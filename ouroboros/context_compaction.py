"""Tool-history compaction with safe structural fallback on LLM failure."""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_COMPACTION_PROTECTED_TOOLS = frozenset({
    "commit_reviewed",
    "vcs_commit_reviewed",
    "advisory_review",
    "task_acceptance_review",
    "skill_review",
    "review_status",
    "knowledge_read",
})

_SUMMARY_INPUT_LIMIT = 2500
_BLOCKS_PER_BATCH = 8

_SHELL_EXIT_ERROR_MARKER = "⚠️ SHELL_EXIT_ERROR"

# Structured summary protocol: the summarizer returns one entry per round via
# a pinned tool call instead of the fragile "[round:N]" text framing that weak
# models drift away from (mis-numbered/merged blocks made whole compaction
# passes fail on coding transcripts). The text protocol remains the fallback
# for local light models without reliable tool calling.
_ROUND_SUMMARIES_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_round_summaries",
        "description": "Emit one summary entry per reasoning round block, keyed by round_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "summaries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "round_id": {"type": "integer", "description": "The [round:id] number of the block."},
                            "summary": {"type": "string", "description": "3-6 sentence first-person summary."},
                        },
                        "required": ["round_id", "summary"],
                    },
                }
            },
            "required": ["summaries"],
        },
    },
}


def _find_tool_name_for_result(msg: dict, messages: list) -> str:
    """Look up which tool produced a given tool-result message."""
    target_id = msg.get("tool_call_id", "")
    if not target_id:
        return ""
    msg_idx = None
    for idx, item in enumerate(messages):
        if item is msg:
            msg_idx = idx
            break
    if msg_idx is None:
        return ""
    for j in range(msg_idx - 1, -1, -1):
        prev = messages[j]
        if prev.get("role") != "assistant":
            continue
        for tc in (prev.get("tool_calls") or []):
            if tc.get("id") == target_id:
                return tc.get("function", {}).get("name", "")
        break
    return ""


def _tool_round_starts(messages: list) -> list[int]:
    return [
        idx for idx, msg in enumerate(messages)
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]


def _tool_round_spans(messages: list) -> list[Tuple[int, int]]:
    starts = _tool_round_starts(messages)
    spans: list[Tuple[int, int]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] - 1 if idx + 1 < len(starts) else len(messages) - 1
        spans.append((start, end))
    return spans


def _protected_warning_in_head(content: str) -> bool:
    """Whether a tool result carries a compaction-protected ⚠️ marker.

    The marker is not always at character 0: shell prepends autocorrect notes
    before the ⚠️ line and some results lead with whitespace, which made the
    old ``startswith`` check silently unprotect prefixed warnings. Scan the
    first two non-empty lines instead. SHELL_EXIT_ERROR markers are exempt:
    failed-command rounds are exactly the trial-and-error history that MUST
    compact (the summarizer is instructed to keep the first error line), while
    every other ⚠️ marker keeps full protection.
    """
    seen = 0
    for line in str(content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("⚠️"):
            return not line.startswith(_SHELL_EXIT_ERROR_MARKER)
        seen += 1
        if seen >= 2:
            break
    return False


def _round_has_protected_content(messages: list, start: int, end: int) -> bool:
    for idx in range(start, end + 1):
        msg = messages[idx]
        role = msg.get("role", "")
        content = str(msg.get("content") or "")
        # Protect critical tool results and error markers from compaction.
        if role == "tool":
            tool_name = _find_tool_name_for_result(msg, messages)
            if tool_name in _COMPACTION_PROTECTED_TOOLS or _protected_warning_in_head(content):
                return True
    return False


def _excerpt_for_summary(text: str, limit: int = _SUMMARY_INPUT_LIMIT) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n<<EXCERPT_FOR_COMPACTION len={len(text)} chars>>"


def _compact_argument_value(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return {"_depth_limit": True, "_type": type(value).__name__}
    if isinstance(value, str):
        if len(value) <= 160:
            return value
        return f"<<LONG_STRING len={len(value)}>>"
    if isinstance(value, dict):
        return {k: _compact_argument_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > 20:
            return {"_list_len": len(value), "_type": "list"}
        return [_compact_argument_value(v, depth + 1) for v in value]
    return value


def _compact_tool_call_arguments(tool_name: str, args_json: str) -> Dict[str, Any]:
    """Compact tool call arguments for old rounds without silent truncation."""
    large_content_tools = {
        "write_file": "content",
        "update_scratchpad": "content",
        "update_identity": "content",
    }

    try:
        args = json.loads(args_json)
        if not isinstance(args, dict):
            return {"name": tool_name, "arguments": f"<<NON_DICT_ARGS type={type(args).__name__}>>"}

        compacted = dict(args)
        large_field = large_content_tools.get(tool_name)
        if large_field and large_field in compacted and compacted[large_field]:
            raw = compacted[large_field]
            raw_str = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            compacted[large_field] = f"<<CONTENT_OMITTED len={len(raw_str)}>>"

        compacted = {k: _compact_argument_value(v) for k, v in compacted.items()}
        return {"name": tool_name, "arguments": json.dumps(compacted, ensure_ascii=False)}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"name": tool_name, "arguments": f"<<UNPARSEABLE_ARGS_JSON len={len(args_json)}>>"}


def _render_round_block(messages: list, start: int, end: int) -> str:
    lines: list[str] = []
    for idx in range(start, end + 1):
        msg = messages[idx]
        role = msg.get("role")
        if role == "assistant":
            content = str(msg.get("content") or "").strip()
            if content:
                lines.append("REASONING:")
                lines.append(_excerpt_for_summary(content))
            for tc in msg.get("tool_calls") or []:
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                args_json = func.get("arguments", "")
                compacted = _compact_tool_call_arguments(tool_name, args_json) if args_json else {"name": tool_name, "arguments": "{}"}
                lines.append(f"TOOL_CALL {compacted['name']}: {compacted['arguments']}")
        elif role == "tool":
            tool_name = _find_tool_name_for_result(msg, messages) or "unknown_tool"
            content = str(msg.get("content") or "")
            lines.append(f"TOOL_RESULT {tool_name}:")
            lines.append(_excerpt_for_summary(content))
        elif role == "user":
            content = msg.get("content")
            if isinstance(content, list):
                # Multipart user content: never str() an image block (the
                # base64 payload would flood the light summarizer's prompt).
                parts = []
                for block in content:
                    if not isinstance(block, dict):
                        parts.append(str(block))
                    elif str(block.get("type") or "") in ("image_url", "image"):
                        caption = str(block.get("_caption") or "").strip()
                        parts.append(f"[image: {caption or 'omitted'}]")
                    else:
                        parts.append(str(block.get("text", "")))
                content = "\n".join(part for part in parts if part)
            else:
                content = str(content or "")
            lines.append("USER_INPUT:")
            lines.append(_excerpt_for_summary(content))
    return "\n".join(lines).strip()


def compact_tool_history(messages: list, keep_recent: int = 6) -> list:
    """Safe fallback: preserve full content, compact only oversized tool-call payloads."""
    spans = _tool_round_spans(messages)
    if len(spans) <= keep_recent:
        return messages

    compactable_starts = {start for start, _ in spans[:-keep_recent]}
    result = []
    for idx, msg in enumerate(messages):
        if idx in compactable_starts and msg.get("role") == "assistant" and msg.get("tool_calls"):
            compacted = dict(msg)
            compacted_calls = []
            for tc in msg.get("tool_calls") or []:
                tc_copy = dict(tc)
                if "function" in tc_copy:
                    func = dict(tc_copy["function"])
                    args_str = func.get("arguments", "")
                    tc_copy["function"] = _compact_tool_call_arguments(func.get("name", ""), args_str) if args_str else func
                compacted_calls.append(tc_copy)
            compacted["tool_calls"] = compacted_calls
            result.append(compacted)
            continue
        result.append(msg)
    return result


_SUMMARY_GUIDANCE = (
    "Summarize each reasoning round block below. Preserve: user steering, "
    "key hypotheses, tools used only when relevant, outcomes, what changed, "
    "and the next step or open question. If a command or tool failed, keep "
    "the exact first error line verbatim. Write as Ouroboros in first person. "
    "Keep each summary to 3-6 sentences."
)


def _parse_structured_summaries(resp_msg: Dict[str, Any]) -> Dict[int, str]:
    """Extract round summaries from an emit_round_summaries tool call."""
    for tc in resp_msg.get("tool_calls") or []:
        func = tc.get("function") or {}
        if str(func.get("name") or "") != "emit_round_summaries":
            continue
        try:
            args = json.loads(func.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        parsed: Dict[int, str] = {}
        for entry in args.get("summaries") or []:
            if not isinstance(entry, dict):
                continue
            try:
                round_id = int(entry.get("round_id"))
            except (TypeError, ValueError):
                continue
            summary = str(entry.get("summary") or "").strip()
            if summary:
                parsed[round_id] = summary
        if parsed:
            return parsed
    return {}


def _parse_text_protocol_summaries(summary_text: str) -> Dict[int, str]:
    """Parse the legacy ``[round:N]``-framed text protocol (local fallback)."""
    summary_map: Dict[int, str] = {}
    current_round: Optional[int] = None
    current_lines: list[str] = []
    for line in summary_text.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("[round:") and stripped.endswith("]"):
            if current_round is not None:
                summary_map[current_round] = " ".join(current_lines).strip()
            current_lines = []
            try:
                current_round = int(stripped[len("[round:"):-1])
            except ValueError:
                current_round = None
            continue
        if current_round is not None:
            current_lines.append(stripped)
    if current_round is not None:
        summary_map[current_round] = " ".join(current_lines).strip()
    return {k: v for k, v in summary_map.items() if v}


def _merge_compaction_usage(
    total: Optional[Dict[str, Any]], usage: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    if not usage:
        return total
    if total is None:
        return dict(usage)
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value
        else:
            total[key] = value
    return total


def _summarize_round_batch(
    rendered_blocks: List[Tuple[int, str]],
    *,
    drive_root: pathlib.Path,
    task_id: str,
) -> Tuple[Dict[int, str], Optional[Dict[str, Any]]]:
    """Summarize one batch of rounds; returns (summary_map, usage).

    Tries the structured tool protocol first (reliable round_id keying), then
    falls back to the legacy text protocol within the same response or — for
    local light models without dependable tool calling — as the primary path.
    Usage is returned even when parsing yields nothing so the caller can
    account spend for failed batches.
    """
    batch_text = "\n\n---\n\n".join(
        f"[round:{start}]\n{content}" for start, content in rendered_blocks
    )

    from ouroboros.config import get_light_model
    from ouroboros.llm import LLMClient
    from ouroboros.llm_observability import chat_observed

    light_model = get_light_model()
    client = LLMClient()
    use_local_light = os.environ.get("USE_LOCAL_LIGHT", "").lower() in ("true", "1")
    total_usage: Optional[Dict[str, Any]] = None

    if not use_local_light:
        structured_prompt = (
            _SUMMARY_GUIDANCE
            + " Call emit_round_summaries exactly once with one entry per [round:id] block.\n\n"
            + batch_text
        )
        try:
            resp_msg, usage = chat_observed(
                client,
                drive_root=drive_root,
                task_id=task_id,
                call_type="context_compaction",
                messages=[{"role": "user", "content": structured_prompt}],
                model=light_model,
                tools=[_ROUND_SUMMARIES_TOOL],
                tool_choice="required",
                reasoning_effort="low",
                max_tokens=32768,
                use_local=False,
            )
            total_usage = _merge_compaction_usage(total_usage, usage)
            summary_map = _parse_structured_summaries(resp_msg)
            if not summary_map:
                # Some models answer in prose despite the pin; salvage it.
                summary_map = _parse_text_protocol_summaries(str(resp_msg.get("content") or ""))
            if summary_map:
                return summary_map, total_usage
        except Exception:
            log.warning(
                "Structured compaction protocol failed; retrying with text protocol",
                exc_info=True,
            )

    text_prompt = (
        _SUMMARY_GUIDANCE
        + " Output one block per [round:id] in the same order.\n\n"
        + batch_text
    )
    try:
        resp_msg, usage = chat_observed(
            client,
            drive_root=drive_root,
            task_id=task_id,
            call_type="context_compaction",
            messages=[{"role": "user", "content": text_prompt}],
            model=light_model,
            reasoning_effort="low",
            max_tokens=32768,
            use_local=use_local_light,
        )
    except Exception as exc:
        # Spend already incurred by the structured attempt must survive the
        # text-protocol failure so the caller can account it.
        raise _BatchSummaryError(
            f"text-protocol call failed: {type(exc).__name__}: {exc}", usage=total_usage
        ) from exc
    total_usage = _merge_compaction_usage(total_usage, usage)
    summary_text = resp_msg.get("content") or ""
    if not summary_text.strip():
        raise _BatchSummaryError("empty summary response", usage=total_usage)
    return _parse_text_protocol_summaries(summary_text), total_usage


class _BatchSummaryError(RuntimeError):
    """Batch-level summarization failure that still carries spend to account."""

    def __init__(self, message: str, *, usage: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.usage = usage


def compact_tool_history_llm(
    messages: list,
    keep_recent: int = 6,
    *,
    drive_root: Optional[pathlib.Path] = None,
    task_id: str = "context_compaction",
) -> Tuple[list, Optional[Dict[str, Any]]]:
    """LLM-driven compaction of old reasoning rounds, with safe non-destructive fallback."""
    spans = _tool_round_spans(messages)
    if len(spans) <= keep_recent:
        return messages, None

    spans_to_keep = spans[-keep_recent:]
    {start for start, _ in spans_to_keep}
    compactable_spans = []
    protected_starts = set()
    for start, end in spans[:-keep_recent]:
        if _round_has_protected_content(messages, start, end):
            protected_starts.add(start)
            continue
        compactable_spans.append((start, end))

    if not compactable_spans:
        return messages, None

    rendered_blocks = [(start, _render_round_block(messages, start, end)) for start, end in compactable_spans]

    total_usage: Optional[Dict[str, Any]] = None
    summary_map: Dict[int, str] = {}
    observed_drive_root = pathlib.Path(drive_root) if drive_root is not None else pathlib.Path("../data").resolve(strict=False)
    observed_task_id = str(task_id or "context_compaction")
    # Per-batch isolation: one failed batch leaves ONLY its own rounds raw
    # (the old whole-pass try/except threw away every successful summary, so
    # a single bad batch made large transcripts permanently uncompactable).
    # Rounds whose summary is missing within a successful batch likewise stay
    # raw individually. Spend from failed batches is still accounted.
    for idx in range(0, len(rendered_blocks), _BLOCKS_PER_BATCH):
        batch = rendered_blocks[idx:idx + _BLOCKS_PER_BATCH]
        try:
            batch_map, usage = _summarize_round_batch(
                batch,
                drive_root=observed_drive_root,
                task_id=observed_task_id,
            )
        except _BatchSummaryError as exc:
            log.warning("Compaction batch failed (%s); leaving its rounds raw", exc)
            total_usage = _merge_compaction_usage(total_usage, exc.usage)
            continue
        except Exception:
            log.warning("Compaction batch failed; leaving its rounds raw", exc_info=True)
            continue
        total_usage = _merge_compaction_usage(total_usage, usage)
        for start, _ in batch:
            if batch_map.get(start):
                summary_map[start] = batch_map[start]

    if not summary_map:
        log.warning("LLM compaction produced no summaries, preserving original rounds")
        return compact_tool_history(messages, keep_recent=keep_recent), total_usage

    compacted_by_start = {
        start: (end, summary_map[start])
        for start, end in compactable_spans
        if start in summary_map
    }

    result = []
    idx = 0
    while idx < len(messages):
        block = compacted_by_start.get(idx)
        if block:
            end, summary = block
            result.append({
                "role": "assistant",
                "content": f"[Compacted reasoning block]\n{summary}",
            })
            idx = end + 1
            continue
        result.append(messages[idx])
        idx += 1

    return result, total_usage
