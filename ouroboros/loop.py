"""LLM tool loop: call model, execute tools, repeat until final response."""

from __future__ import annotations

import json
import os
import queue
import pathlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import logging

from ouroboros.llm import LLMClient, normalize_reasoning_effort, add_usage
from ouroboros.config import get_context_mode, get_light_model, get_task_review_mode, resolve_effort
from ouroboros.outcomes import turn_has_reviewable_effects
from ouroboros.observability import new_call_id, persist_call
from ouroboros.tool_policy import initial_tool_schemas, list_non_core_tools
from ouroboros.tools.registry import ToolRegistry
from ouroboros.context import build_user_content
from ouroboros.context_budget import EMERGENCY_COMPACTION_CHARS, LOW_EMERGENCY_COMPACTION_CHARS
from ouroboros.context_compaction import _tool_round_spans, compact_tool_history_llm
from ouroboros.deadline_utils import parse_deadline_ts, utc_now
from ouroboros.utils import estimate_tokens

from ouroboros.loop_tool_execution import (
    StatefulToolExecutor,
    handle_tool_calls,
)
from ouroboros.loop_llm_call import call_llm_with_retry, emit_llm_usage_event, estimate_cost

# Backward-compat alias for source-inspecting/monkeypatched tests.
_call_llm_with_retry = call_llm_with_retry

log = logging.getLogger(__name__)


@dataclass
class _CompactionRoundContext:
    tools: ToolRegistry
    drive_root: Optional[pathlib.Path]
    drive_logs: pathlib.Path
    task_id: str
    round_idx: int
    event_queue: Optional[queue.Queue]
    active_use_local: bool
    active_context_mode: str
    checkpoint_injected: bool
    emit_progress: Callable[[str], None]
    active_model: str = ""


def _estimate_messages_chars(messages: List[Dict[str, Any]]) -> int:
    """Estimate transcript size over the FULL message list (the system block,
    when present in ``messages``, is counted too — conservative for the
    window-derived emergency trigger)."""
    from ouroboros.context_budget import IMAGE_BLOCK_CHAR_EQUIVALENT

    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if str(block.get("type") or "") in ("image_url", "image"):
                        # Vision tokens are billed per tile, not per base64
                        # char: counting the raw payload made ONE image look
                        # like ~300K tokens and permanently wedged emergency
                        # compaction.
                        total += IMAGE_BLOCK_CHAR_EQUIVALENT
                        continue
                    # Count whole multipart blocks, including cache markers.
                    try:
                        import json as _json2
                        total += len(_json2.dumps(block, ensure_ascii=False))
                    except (TypeError, ValueError):
                        total += len(str(block))
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            try:
                import json as _json
                total += len(_json.dumps(tool_calls, ensure_ascii=False))
            except (TypeError, ValueError):
                total += sum(len(str(tc)) for tc in tool_calls)
        tc_id = msg.get("tool_call_id")
        if tc_id:
            total += len(str(tc_id))
    return total


def _provider_failure_hint(accumulated_usage: Dict[str, Any]) -> str:
    detail = " ".join(str(accumulated_usage.get("_last_llm_error") or "").split()).strip()
    if not detail:
        return ""
    return f" Last provider error: {detail}"


def _provider_recovery_hint(accumulated_usage: Dict[str, Any]) -> str:
    """Explain whether retrying later is likely to help."""
    if accumulated_usage.get("context_overflow_suggest_low"):
        return (
            " ⚠️ The context overflowed the model window. Switching to low context "
            "mode (Settings → Behavior, or the chat toggle) fits ~200K / local "
            "models by serving ARCHITECTURE as a navigation map and compacting "
            "memory sooner — without changing the model or reasoning effort."
        )
    kind = str(accumulated_usage.get("_last_llm_error_kind") or "").strip()
    if kind in {"quota_exhausted", "auth_error", "request_too_large", "bad_request", "context_overflow"}:
        guidance = {
            "quota_exhausted": "The provider rejected the request for quota/billing reasons; retrying the same request will not help until the key/account limit changes.",
            "auth_error": "The provider rejected authentication/authorization; retrying the same request will not help until the configured key or provider access is fixed.",
            "request_too_large": "The provider rejected the request size/output-token shape; retrying the same request will not help without reducing context/output demand or changing model capacity.",
            "bad_request": "The provider rejected the request shape; retrying the same request will not help until the transcript/tool payload is fixed.",
            "context_overflow": "The context overflowed the model window; retrying the same request will not help without reducing context or changing model capacity.",
        }.get(kind, "Retrying the same provider request will not help until the underlying request/account issue changes.")
        return f" {guidance}"
    detail = str(accumulated_usage.get("_last_llm_error") or "").lower()
    if "prefill" in detail or "conversation must end with a user message" in detail:
        return (
            " This looks like a client-side transcript-shape error, not a "
            "provider outage; retrying the same input will not help."
        )
    if "provider returned incomplete response" in detail or "finish_reason=null" in detail:
        return (
            " The provider returned incomplete responses repeatedly; this may "
            "be transient, but it can also indicate malformed client input."
        )
    return " If background consciousness is running, it will retry when the provider recovers."


def _handle_text_response(
    content: Optional[str],
    llm_trace: Dict[str, Any],
    accumulated_usage: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Handle LLM response without tool calls (final response)."""
    if content and content.strip():
        llm_trace["reasoning_notes"].append(content.strip())
    return (content or ""), accumulated_usage, llm_trace


def _final_text_acknowledges_incomplete_children(content: Any, children: List[Dict[str, Any]]) -> bool:
    text = str(content or "").lower()
    if not text.strip():
        return False
    incomplete_words = ("incomplete", "pending", "running", "scheduled", "not complete", "still")
    if not any(word in text for word in incomplete_words):
        return False
    for child in children:
        task_id = str(child.get("task_id") or child.get("id") or "").strip().lower()
        status = str(child.get("status") or "").strip().lower()
        if task_id and task_id not in text:
            return False
        if status and status not in text:
            return False
    return True


def _skill_names_touched_by_trace(llm_trace: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for call in llm_trace.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool") or "")
        if tool not in {"write_file", "edit_text", "claude_code_edit"}:
            continue
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        bucket = str(args.get("bucket") or "").strip().lower()
        skill_name = str(args.get("skill_name") or "").strip()
        if bucket in {"external", "clawhub", "ouroboroshub"} and skill_name:
            if skill_name not in names:
                names.append(skill_name)
            continue
        candidates = [str(args.get("cwd") or "")] if tool == "claude_code_edit" else [str(args.get("path") or "")]
        for raw in candidates:
            norm = raw.replace("\\", "/").strip().lstrip("/")
            if norm.startswith("data/"):
                norm = norm[len("data/"):]
            parts = pathlib.PurePosixPath(norm).parts
            if len(parts) >= 3 and parts[0] == "skills" and parts[1] in {"external", "clawhub", "ouroboroshub", "native"}:
                name = parts[2]
                if name and name not in names:
                    names.append(name)
    return names


def _skill_finalization_message(drive_root: pathlib.Path, llm_trace: Dict[str, Any]) -> str:
    names = _skill_names_touched_by_trace(llm_trace)
    if not names:
        return ""
    try:
        from ouroboros.skill_loader import find_skill
        from ouroboros.skill_readiness import skill_readiness_for_execution
    except Exception:
        return ""
    blockers: List[str] = []
    for name in names:
        try:
            skill = find_skill(pathlib.Path(drive_root), name)
            if skill is None or not getattr(skill, "is_self_authored", False):
                continue
            readiness = skill_readiness_for_execution(pathlib.Path(drive_root), skill)
            ready = readiness.ready
        except Exception:
            continue
        if not ready:
            blockers.append(
                f"{skill.name}: status={skill.review.status!r}, "
                f"blockers={readiness.blockers}"
            )
    if not blockers:
        return ""
    return (
        "⚠️ SKILL_NOT_FINALIZED: You edited self-authored skill payloads but "
        "they are not ready yet. Call skill_review for each skill before "
        "declaring the task done. Current blockers: " + "; ".join(blockers)
    )


def _force_plan_completed(llm_trace: Dict[str, Any]) -> bool:
    """True when a reviewed plan_task completed in this trace.

    Reads the structured ``plan_review_aggregate`` flag captured from the FULL
    tool result at execution time (loop_tool_execution); the old substring
    check against the 700-char trace preview could never see the aggregate
    marker at the end of a long plan output, wedging Consilium tasks in the
    force-plan reminder loop.
    """
    for call in llm_trace.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        if (
            str(call.get("tool") or "") == "plan_task"
            and not bool(call.get("is_error"))
            and bool(call.get("plan_review_aggregate"))
        ):
            return True
    return False


def _force_plan_required(ctx: Any, llm_trace: Dict[str, Any]) -> bool:
    metadata = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
    return bool(metadata.get("force_plan")) and not _force_plan_completed(llm_trace)


def _check_budget_limits(
    budget_remaining_usd: Optional[float],
    accumulated_usage: Dict[str, Any],
    round_idx: int,
    messages: List[Dict[str, Any]],
    llm: LLMClient,
    active_model: str,
    active_effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: Optional[queue.Queue],
    llm_trace: Dict[str, Any],
    task_type: str = "task",
    use_local: bool = False,
    deadline_ts: Optional[float] = None,
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """Return a final-response tuple when budget limits require stopping."""
    if budget_remaining_usd is None:
        return None

    task_cost = accumulated_usage.get("cost", 0)

    if budget_remaining_usd <= 0:
        finish_reason = "🚫 Task rejected. Total budget exhausted. Please increase TOTAL_BUDGET in settings."
        accumulated_usage["execution_status"] = "failed"
        accumulated_usage["reason_code"] = "budget_exhausted"
        # One bounded tool-less best-effort extraction before rejecting: if the
        # task already produced verified work, salvage it instead of returning
        # nothing (the typed best_effort outcome gate reads this reason code).
        if round_idx > 1:
            try:
                _append_or_merge_user_message(
                    messages,
                    "[BUDGET LIMIT] Total budget exhausted. Produce your best final answer NOW "
                    "from the verified work so far; clearly mark anything unverified or "
                    "incomplete. An honest best-effort result is the expected outcome here.",
                )
                final_msg, _cost = _call_llm_with_retry(
                    llm, messages, active_model, None, active_effort,
                    1, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type,
                    use_local=use_local,
                    deadline_ts=deadline_ts,
                )
                accumulated_usage["execution_status"] = "failed"
                accumulated_usage["reason_code"] = "budget_exhausted"
                final_text = str((final_msg or {}).get("content") or "").strip()
                if final_text:
                    accumulated_usage["_best_effort_extracted"] = True
                    return final_text, accumulated_usage, llm_trace
            except Exception:
                log.warning("Failed to extract best-effort answer after budget exhaustion", exc_info=True)
        return finish_reason, accumulated_usage, llm_trace

    budget_pct = task_cost / budget_remaining_usd if budget_remaining_usd > 0 else 1.0

    from ouroboros.config import SETTINGS_DEFAULTS as _DEFAULTS
    _per_task_default = str(_DEFAULTS["OUROBOROS_PER_TASK_COST_USD"])
    per_task_limit = float(os.environ.get("OUROBOROS_PER_TASK_COST_USD", _per_task_default) or _per_task_default)
    if task_cost >= per_task_limit and round_idx % 10 == 0:
        _append_or_merge_user_message(
            messages,
            f"[COST NOTE] Task spent ${task_cost:.3f}, which is at or above the per-task soft threshold of ${per_task_limit:.2f}. Continue only if the expected value still justifies the cost.",
        )

    if budget_pct > 0.5:
        finish_reason = f"Task spent ${task_cost:.3f} (>50% of remaining ${budget_remaining_usd:.2f}). Budget exhausted."
        _append_or_merge_user_message(
            messages,
            f"[BUDGET LIMIT] {finish_reason} Produce your best final answer now from the "
            "verified work so far; clearly mark anything unverified or incomplete. An honest "
            "best-effort result is the expected outcome here, not a failure.",
        )
        try:
            final_msg, final_cost = _call_llm_with_retry(
                llm, messages, active_model, None, active_effort,
                max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type,
                use_local=use_local,
                deadline_ts=deadline_ts,
            )
            accumulated_usage["execution_status"] = "failed"
            accumulated_usage["reason_code"] = "budget_exhausted"
            extracted = str((final_msg or {}).get("content") or "").strip()
            if extracted:
                accumulated_usage["_best_effort_extracted"] = True
                return extracted, accumulated_usage, llm_trace
            return finish_reason, accumulated_usage, llm_trace
        except Exception:
            log.warning("Failed to get final response after budget limit", exc_info=True)
            accumulated_usage["execution_status"] = "failed"
            accumulated_usage["reason_code"] = "budget_exhausted"
            return finish_reason, accumulated_usage, llm_trace
    elif budget_pct > 0.3 and round_idx % 10 == 0:
        _append_or_merge_user_message(messages, f"[INFO] Task spent ${task_cost:.3f} of ${budget_remaining_usd:.2f}. Wrap up if possible.")

    return None


def _build_recent_tool_trace(messages: List[Dict[str, Any]], window: int = 15) -> str:
    """Build a compact recent-tool trace for the self-check prompt."""
    all_calls: List[str] = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", "")
                if isinstance(args, dict):
                    args = json.dumps(args, sort_keys=True)
                args_str = str(args)
                summary = f"{name}({args_str[:80]})" if len(args_str) > 80 else f"{name}({args_str})"
                all_calls.append(summary)
    recent = all_calls[-window:] if all_calls else []
    if not recent:
        return ""
    return "Recent tool calls (oldest first):\n" + "\n".join(f"  {i+1}. {c}" for i, c in enumerate(recent))


def _emit_checkpoint_event(
    event_queue: Optional[queue.Queue],
    task_id: str,
    drive_logs: Optional[pathlib.Path],
    data: Dict[str, Any],
) -> bool:
    """Emit a task_checkpoint via event queue or direct events.jsonl append."""
    from ouroboros.loop_llm_call import _emit_live_log
    payload = {"type": "task_checkpoint", "task_id": task_id, **data}
    if event_queue is not None:
        _emit_live_log(event_queue, payload)
    elif drive_logs:
        try:
            from ouroboros.utils import append_jsonl, utc_now_iso
            append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), **payload})
        except Exception:
            pass


def _persist_compaction_checkpoint(
    messages: List[Dict[str, Any]],
    *,
    drive_root: Optional[pathlib.Path],
    drive_logs: pathlib.Path,
    task_id: str,
    reason: str,
    keep_recent: int,
    round_idx: int,
    event_queue: Optional[queue.Queue],
) -> None:
    """Persist the pre-compaction transcript so compaction is only a view."""
    root = pathlib.Path(drive_root) if drive_root is not None else pathlib.Path(drive_logs).parent
    call_id = new_call_id("compaction_checkpoint")
    try:
        ref = persist_call(
            root,
            task_id=task_id,
            call_id=call_id,
            call_type="compaction_checkpoint",
            payload={
                "reason": reason,
                "keep_recent": keep_recent,
                "round": round_idx,
                "messages": messages,
            },
            manifest={
                "round": round_idx,
                "reason": reason,
                "keep_recent": keep_recent,
            },
        )
        _emit_checkpoint_event(event_queue, task_id, drive_logs, {
            "checkpoint_kind": "pre_compaction_transcript",
            "round": round_idx,
            "reason": reason,
            "keep_recent": keep_recent,
            "checkpoint_ref": ref.get("manifest_ref"),
        })
        return True
    except Exception:
        log.debug("Failed to persist pre-compaction transcript checkpoint", exc_info=True)
        _emit_checkpoint_event(event_queue, task_id, drive_logs, {
            "checkpoint_kind": "pre_compaction_transcript",
            "round": round_idx,
            "reason": reason,
            "keep_recent": keep_recent,
            "checkpoint_status": "failed",
        })
        return False


def _extract_plain_text_from_content(content: Any) -> str:
    """Extract text from strings or multipart content for transcript sealing."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content) if content is not None else ""


def _append_or_merge_user_message(messages: List[Dict[str, Any]], text: str) -> None:
    """Append a user message without creating consecutive user turns."""
    _append_or_merge_user_content(messages, text)


def _evict_stale_image_blocks(messages: List[Dict[str, Any]], *, incoming: int = 0) -> None:
    """Keep only the newest MAX_LIVE_IMAGE_BLOCKS image blocks in the transcript.

    Single counter across ALL image sources (owner uploads, browser
    screenshots, transport injections). Evicted blocks become a text
    placeholder carrying the caption and the re-view path, so the dialogue
    HORIZON is preserved while the heavy payload is dropped (P1: granularity
    varies, history does not silently vanish). ``incoming`` reserves room for
    blocks about to be appended.
    """
    from ouroboros.context_budget import MAX_LIVE_IMAGE_BLOCKS

    image_refs: List[tuple] = []  # (message_idx, block_idx)
    for m_idx, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b_idx, block in enumerate(content):
            if isinstance(block, dict) and str(block.get("type") or "") in ("image_url", "image"):
                image_refs.append((m_idx, b_idx))
    excess = len(image_refs) + max(0, int(incoming)) - MAX_LIVE_IMAGE_BLOCKS
    if excess <= 0:
        return
    for m_idx, b_idx in image_refs[:excess]:
        content = messages[m_idx]["content"]
        block = content[b_idx]
        caption = str(block.get("_caption") or "").strip()
        source_path = str(block.get("_source_path") or "").strip()
        placeholder = "[image evicted"
        if caption:
            placeholder += f": {caption}"
        if source_path:
            placeholder += f"; re-view: vlm_query file_path={source_path}"
        placeholder += "]"
        content[b_idx] = {"type": "text", "text": placeholder}


def _append_or_merge_user_content(messages: List[Dict[str, Any]], content: Any) -> None:
    """Append user content without flattening multipart blocks."""
    if isinstance(content, list):
        incoming_images = sum(
            1 for b in content
            if isinstance(b, dict) and str(b.get("type") or "") in ("image_url", "image")
        )
        if incoming_images:
            _evict_stale_image_blocks(messages, incoming=incoming_images)
    if messages and messages[-1].get("role") == "user":
        prior = messages[-1].get("content")
        if isinstance(content, list):
            new_blocks = list(content)
            if isinstance(prior, list):
                messages[-1] = {"role": "user", "content": list(prior) + new_blocks}
                return
            prior_text = prior if isinstance(prior, str) else str(prior or "")
            prefix_block = [{"type": "text", "text": prior_text.rstrip() + "\n\n---\n\n"}] if prior_text else []
            messages[-1] = {"role": "user", "content": prefix_block + new_blocks}
            return
        text = str(content or "")
        if isinstance(prior, list):
            messages[-1] = {
                "role": "user",
                "content": list(prior) + [{"type": "text", "text": "\n\n---\n\n" + text}],
            }
            return
        prior_text = prior if isinstance(prior, str) else str(prior or "")
        messages[-1] = {
            "role": "user",
            "content": (prior_text.rstrip() + "\n\n---\n\n" + text) if prior_text else text,
        }
        return
    messages.append({"role": "user", "content": content})


def _owner_marked_content(content: Any) -> Any:
    """Mark direct owner injections with the same priority tag as mailbox messages."""
    prefix = "[Message from my human]: "
    if isinstance(content, list):
        blocks = [dict(block) if isinstance(block, dict) else block for block in content]
        for block in blocks:
            if isinstance(block, dict) and str(block.get("type") or "") in {"text", "input_text"}:
                block["text"] = prefix + str(block.get("text") or "")
                return blocks
        return [{"type": "text", "text": prefix.rstrip()}] + blocks
    return prefix + str(content or "")


def _task_acceptance_eligible(mode: str, llm_trace: Dict[str, Any], is_direct_chat: bool) -> tuple[bool, str]:
    """Return ``(host_should_review, trigger_reason)``.

    ``required`` is effect-gated: the host enforces review only when the turn
    produced reviewable work (commit / deliverable / repo / workspace / skill
    write) or the task is not a direct-chat turn (queued / headless / scheduled).
    Pure conversation with no reviewable effect is not reviewed even in
    ``required``. ``auto`` stays LLM-first (the agent elects via the visible
    task_acceptance_review tool); ``off`` never reviews. This gates on observable
    runtime effects (P3 immune gate), not on message content (no P5 violation).
    """
    if mode == "off":
        return False, "off"
    if mode == "required":
        if turn_has_reviewable_effects(llm_trace):
            return True, "required_effect"
        if not is_direct_chat:
            return True, "required_nondirect"
        return False, "skipped_conversation"
    return False, "skipped_auto"


def _run_task_acceptance_review_once(
    *,
    tools: ToolRegistry,
    content: str,
    task_id: str,
    task_type: str,
    llm_trace: Dict[str, Any],
    drive_root: Optional[pathlib.Path],
    messages: List[Dict[str, Any]],
    emit_progress: Callable[[str], None],
) -> bool:
    mode = get_task_review_mode()
    if getattr(tools._ctx, "_task_acceptance_reviewed", False):
        return False
    is_direct_chat = bool(getattr(tools._ctx, "is_direct_chat", False))
    eligible, trigger = _task_acceptance_eligible(mode, llm_trace, is_direct_chat)
    agent_called = any(
        isinstance(c, dict) and str(c.get("tool") or "") == "task_acceptance_review"
        for c in (llm_trace.get("tool_calls") or [])
    )
    agent_review_run = any(
        isinstance(run, dict)
        and str(((run.get("request") or {}) if isinstance(run.get("request"), dict) else {}).get("surface") or "") == "task_acceptance"
        and str(run.get("aggregate_signal") or "").strip()
        for run in (llm_trace.get("review_runs") or [])
    )
    if agent_called and agent_review_run:
        tools._ctx._task_acceptance_reviewed = True
        llm_trace["review_decision"] = {"eligibility": "already_reviewed", "trigger": "agent_called_tool_result"}
        return False
    if agent_called:
        llm_trace["review_decision"] = {"eligibility": "eligible", "trigger": "agent_called_tool"}
    else:
        llm_trace["review_decision"] = {
            "eligibility": "eligible" if eligible else "not_eligible",
            "trigger": trigger,
        }
    if not eligible:
        return False
    try:
        from ouroboros.review_substrate import ReviewRequest, reviewer_slots, run_review_request

        tools._ctx._task_acceptance_reviewed = True
        evidence = {
            "task_id": task_id,
            "task_type": task_type,
            "tool_calls": llm_trace.get("tool_calls") or [],
            "reasoning_notes": llm_trace.get("reasoning_notes") or [],
        }
        slots = reviewer_slots(effort=resolve_effort("review"), role_hint="task acceptance")
        min_successful = 2 if len(slots) >= 3 else max(1, len(slots))
        request = ReviewRequest(
            surface="task_acceptance",
            goal=_extract_plain_text_from_content(messages[1].get("content")) if len(messages) > 1 else "",
            subject=str(content or ""),
            evidence=evidence,
            checklist=(
                "Check whether the claimed result follows from the tool trace, "
                "whether errors/timeouts/artifacts were handled honestly, and "
                "whether each explicit original requirement was verified through "
                "the interface/surface the task itself names (not a weaker "
                "surrogate self-test), and "
                "whether the final response should be changed before release. "
                "Classify the deliverable tier (solved / best_effort / "
                "blocked_with_evidence) and name the single highest-value change "
                "that would move it one tier up. If the task asks for a specific "
                "value or short answer, check the FINAL ANSWER line matches the "
                "requested format exactly."
            ),
            policy={
                "verdict_is_advisory": True,
                "full_output_enters_context": True,
                "min_successful_slots": min_successful,
                "fail_closed_on_errors": True,
                "classify_outcome_tier": True,
            },
            task_id=task_id,
        )
        result = run_review_request(
            request,
            slots=slots,
            drive_root=pathlib.Path(drive_root) if drive_root is not None else pathlib.Path(tools._ctx.drive_root),
            usage_ctx=tools._ctx,
        )
        payload = json.dumps(result.__dict__, ensure_ascii=False, indent=2, default=str)
        messages.append({"role": "assistant", "content": content or ""})
        _append_or_merge_user_message(
            messages,
            "[TASK ACCEPTANCE REVIEW]\n"
            "The following full reviewer output is advisory. Deliver your actual answer/result to the "
            "user, revised only if a finding is valid; if a finding is wrong, reject it with evidence. "
            "Do NOT replace your user-facing answer with a status report about this review unless the "
            "user explicitly asked for one.\n\n"
            f"{payload}",
        )
        llm_trace.setdefault("review_runs", []).append(result.__dict__)
        emit_progress("Task acceptance review completed; reviewer output injected before final response.")
        return True
    except Exception as exc:
        if mode == "required":
            tools._ctx._task_acceptance_reviewed = True
            safe_error = _extract_plain_text_from_content(str(exc))[:2000]
            degraded_result = {
                "request": {"surface": "task_acceptance", "task_id": task_id},
                "actors": [],
                "parsed_findings": [{
                    "severity": "critical",
                    "item": "task_acceptance_infra_failure",
                    "evidence": f"{type(exc).__name__}: {safe_error}",
                    "recommendation": "Do not report semantic success unless the failure is explicitly accounted for.",
                }],
                "aggregate_signal": "DEGRADED",
                "degraded": True,
                "degraded_reasons": [f"{type(exc).__name__}: {safe_error}"],
            }
            llm_trace.setdefault("review_runs", []).append(degraded_result)
            messages.append({"role": "assistant", "content": content or ""})
            _append_or_merge_user_message(
                messages,
                "[TASK ACCEPTANCE REVIEW DEGRADED]\n"
                "Required task acceptance review failed before reviewers returned. "
                "This degraded review record is part of the task evidence; account for it "
                "explicitly in your final response (it does not by itself make the work a "
                "failure — deliver your actual result with the review gap noted).\n\n"
                f"{json.dumps(degraded_result, ensure_ascii=False, indent=2)}",
            )
            return True
        log.debug("Task acceptance review skipped after failure", exc_info=True)
        return False


def _maybe_inject_self_check(
    round_idx: int,
    max_rounds: int,
    messages: List[Dict[str, Any]],
    accumulated_usage: Dict[str, Any],
    emit_progress: Callable[[str], None],
    *,
    event_queue: Optional[queue.Queue] = None,
    task_id: str = "",
    drive_logs: Optional[pathlib.Path] = None,
) -> bool:
    """Inject a normal user-turn self-check and emit one checkpoint event."""
    REMINDER_INTERVAL = 15
    if round_idx <= 1 or round_idx % REMINDER_INTERVAL != 0 or round_idx >= max_rounds:
        return False

    ctx_tokens = sum(
        estimate_tokens(_extract_plain_text_from_content(m.get("content")))
        for m in messages
    )
    task_cost = accumulated_usage.get("cost", 0)
    checkpoint_num = round_idx // REMINDER_INTERVAL

    tool_trace = _build_recent_tool_trace(messages)

    reminder = (
        f"[CHECKPOINT {checkpoint_num} — round {round_idx}/{max_rounds}]\n"
        f"Context: ~{ctx_tokens} tokens | Cost so far: ${task_cost:.2f} | "
        f"Rounds remaining: {max_rounds - round_idx}\n"
    )
    if tool_trace:
        reminder += f"\n{tool_trace}\n"
    reminder += (
        "\nThis is a periodic self-check, not a command to stop. "
        "Glance at your recent tool-call trace above and briefly consider:\n"
        "- Are you still making progress toward the task, or repeating the same actions?\n"
        "- Is the current approach still the right one, or should you narrow scope / try a different angle?\n"
        "- If you are waiting on a long build/download/training run or have independent branches of investigation, consider schedule_subagent for a focused parallel handoff.\n"
        "- If the task is effectively done, first re-check the literal original requirements one by one "
        "against the specified interface/path/format/service, then wrap up by replying with your final answer in plain text (no tool call). "
        "Otherwise continue with the most valuable next step.\n"
        "\nNo special format required — just think, then act."
    )

    # Merge into a prior user turn to avoid Anthropic consecutive-role 400s,
    # preserving multipart blocks so images/cache markers survive.
    _append_or_merge_user_message(messages, reminder)
    emit_progress(
        f"Checkpoint {checkpoint_num} at round {round_idx}: "
        f"~{ctx_tokens} tokens, ${task_cost:.2f} spent"
    )

    _emit_checkpoint_event(event_queue, task_id, drive_logs, {
        "checkpoint_number": checkpoint_num,
        "round": round_idx,
        "max_rounds": max_rounds,
        "context_tokens": ctx_tokens,
        "task_cost": task_cost,
    })

    return True


def _maybe_inject_time_budget_milestone(
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    *,
    event_queue: Optional[queue.Queue] = None,
    task_id: str = "",
    drive_logs: Optional[pathlib.Path] = None,
) -> bool:
    """Inject deadline-awareness at 50/25/10% remaining, never per-round."""
    meta = getattr(tools._ctx, "task_metadata", {})
    if not isinstance(meta, dict):
        return False
    deadline = parse_deadline_ts(meta.get("deadline_at"))
    if deadline is None:
        return False
    created = parse_deadline_ts(meta.get("created_at") or meta.get("started_at"))
    if created is None:
        created = getattr(tools._ctx, "_time_budget_started_at", None)
        if created is None:
            created = utc_now()
            tools._ctx._time_budget_started_at = created
    now = utc_now()
    total = max(1.0, (deadline - created).total_seconds())
    remaining = (deadline - now).total_seconds()
    fraction_remaining = 0.0 if remaining <= 0 else remaining / total
    thresholds = ((0.50, "50%"), (0.25, "25%"), (0.10, "10%"))
    seen = getattr(tools._ctx, "_time_budget_milestones_seen", None)
    if not isinstance(seen, set):
        seen = set()
        tools._ctx._time_budget_milestones_seen = seen
    # Fire the TIGHTEST crossed milestone, not the coarsest. Starting a task
    # already past 50% (or 25%/10%) remaining must announce the real urgency
    # immediately instead of labelling it "50%" and cascading one threshold per
    # round (which lags reality and can pass the deadline before "10%" fires).
    # Mark every crossed label seen so coarser ones never fire redundantly.
    crossed = [(value, label) for value, label in thresholds if fraction_remaining <= value]
    unseen_crossed = [(value, label) for value, label in crossed if label not in seen]
    if not unseen_crossed:
        return False
    selected_label = unseen_crossed[-1][1]  # thresholds are coarse→fine
    for _value, label in crossed:
        seen.add(label)
    elapsed = max(0.0, (now - created).total_seconds())
    remaining_clamped = max(0.0, remaining)
    deadline_text = deadline.isoformat().replace("+00:00", "Z")
    _append_or_merge_user_message(
        messages,
        (
            f"[TIME BUDGET — {selected_label} remaining crossed]\n"
            f"Elapsed: ~{elapsed/60:.1f} min | Remaining: ~{remaining_clamped/60:.1f} min | "
            f"Deadline: {deadline_text}\n"
            "Use this as planning context, not as a command to stop. If a passing artifact "
            "or service already exists, prefer preserving and verifying it over speculative "
            "improvements. If not, focus on the shortest path to a verifiable result."
        ),
    )
    _emit_checkpoint_event(event_queue, task_id, drive_logs, {
        "checkpoint_kind": "time_budget_milestone",
        "milestone": selected_label,
        "elapsed_sec": round(elapsed, 3),
        "remaining_sec": round(remaining_clamped, 3),
        "deadline_at": deadline_text,
    })
    return True


def _no_response_failure_text(
    active_model: str,
    active_use_local: bool,
    max_retries: int,
    accumulated_usage: Dict[str, Any],
) -> str:
    """Final failure text when the model gave no response and no distinct fallback exists."""
    attempts_used = int(accumulated_usage.get("_llm_attempts_used") or max_retries)
    local_tag = " (local)" if active_use_local else ""
    return (
        f"⚠️ Failed to get a response from model {active_model}{local_tag} after {attempts_used} attempts. "
        f"No viable fallback model configured.{_provider_failure_hint(accumulated_usage)} "
        f"{_provider_recovery_hint(accumulated_usage)}"
    )


def _task_deadline_epoch(tools: ToolRegistry) -> Optional[float]:
    """Task deadline as epoch seconds, for deadline-bounded LLM retry backoff."""
    meta = getattr(tools._ctx, "task_metadata", {})
    if not isinstance(meta, dict):
        return None
    deadline = parse_deadline_ts(meta.get("deadline_at"))
    return deadline.timestamp() if deadline is not None else None


def seal_task_transcript(
    messages: List[Dict[str, Any]],
    keep_active: int = 5,
    min_prefix_tokens: int = 2048,
) -> None:
    """Mark one stable old tool-result boundary for provider prompt caching."""
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            # Flatten the old sealed boundary before choosing a new one.
            msg["content"] = _extract_plain_text_from_content(content)

    tool_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool"
    ]
    if len(tool_indices) <= keep_active:
        return

    seal_candidate_idx = tool_indices[-(keep_active + 1)]

    prefix_text_len = sum(
        len(_extract_plain_text_from_content(m.get("content", "")))
        for m in messages[: seal_candidate_idx + 1]
    )
    prefix_tokens = prefix_text_len // 4  # rough 4-chars-per-token estimate

    if prefix_tokens < min_prefix_tokens:
        return

    candidate = messages[seal_candidate_idx]
    plain_text = str(candidate.get("content", ""))
    candidate["content"] = [
        {
            "type": "text",
            "text": plain_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _setup_dynamic_tools(tools_registry, tool_schemas, messages):
    """Attach list/enable tool handlers and mutate the active schema list."""
    enabled_extra: set = set()
    active_tool_names = {
        str(schema.get("function", {}).get("name") or "").strip()
        for schema in tool_schemas
        if str(schema.get("function", {}).get("name") or "").strip()
    }

    def _handle_list_tools(ctx=None, **kwargs):
        omissions = (
            tools_registry.capability_omissions()
            if hasattr(tools_registry, "capability_omissions")
            else []
        )
        non_core = [
            t for t in list_non_core_tools(tools_registry)
            if t["name"] not in active_tool_names
        ]
        if not non_core:
            if not omissions:
                return "All tools are already in your active set."
            lines = ["All currently discovered tools are already in your active set.", "", "[CAPABILITY_OMISSION_MANIFEST]"]
            for item in omissions:
                lines.append(
                    f"- {item.get('surface', 'unknown')}: {item.get('reason', 'unknown')} "
                    f"({item.get('error', 'no detail')})"
                )
            return "\n".join(lines)
        lines = [f"**{len(non_core)} additional tools available** (use `enable_tools` to activate):\n"]
        for t in non_core:
            lines.append(f"- **{t['name']}**: {t['description'][:120]}")
        if omissions:
            lines.append("\n[CAPABILITY_OMISSION_MANIFEST]")
            for item in omissions:
                lines.append(
                    f"- {item.get('surface', 'unknown')}: {item.get('reason', 'unknown')} "
                    f"({item.get('error', 'no detail')})"
                )
        return "\n".join(lines)

    def _handle_enable_tools(ctx=None, tools: str = "", **kwargs):
        names = [n.strip() for n in tools.split(",") if n.strip()]
        enabled, not_found = [], []
        for name in names:
            schema = tools_registry.get_schema_by_name(name)
            if schema and name not in active_tool_names:
                tool_schemas.append(schema)
                enabled_extra.add(name)
                active_tool_names.add(name)
                enabled.append(f"{name} (registered late)")
            elif name in active_tool_names:
                enabled.append(f"{name} (already active)")
            else:
                not_found.append(name)
        parts = []
        if enabled:
            parts.append(
                "✅ Tools are registered in the active capability envelope: "
                + ", ".join(enabled)
            )
        if not_found:
            parts.append(f"❌ Not found: {', '.join(not_found)}")
        return "\n".join(parts) if parts else "No tools specified."

    tools_registry.override_handler("list_available_tools", _handle_list_tools)
    tools_registry.override_handler("enable_tools", _handle_enable_tools)

    non_core_count = len(list_non_core_tools(tools_registry))
    if non_core_count > 0:
        _append_or_merge_user_message(
            messages,
            (
                "[SYSTEM NOTICE]\n"
                f"You have {len(tool_schemas)} core tools loaded. "
                f"There are {non_core_count} additional tools available "
                f"(use `list_available_tools` to see them, `enable_tools` to activate). "
                f"Core tools cover most tasks. Enable extras only when needed."
            ),
        )
    omissions = (
        tools_registry.capability_omissions()
        if hasattr(tools_registry, "capability_omissions")
        else []
    )
    if omissions:
        lines = ["[CAPABILITY_OMISSION_MANIFEST]"]
        for item in omissions:
            lines.append(
                f"- {item.get('surface', 'unknown')}: {item.get('reason', 'unknown')} "
                f"({item.get('error') or item.get('resource') or 'no detail'})"
            )
        _append_or_merge_user_message(messages, "[SYSTEM NOTICE]\n" + "\n".join(lines))

    return tool_schemas, enabled_extra


def _drain_incoming_messages(
    messages: List[Dict[str, Any]],
    incoming_messages: queue.Queue,
    drive_root: Optional[pathlib.Path],
    task_id: str,
    event_queue: Optional[queue.Queue],
    _owner_msg_seen: set,
) -> Dict[str, Any]:
    """Inject owner messages received during task execution.

    Returns typed control signals drained from the mailbox (currently
    ``{"finalize_now": reason}`` when the supervisor opened a finalization
    grace window); control entries are routed structurally, never injected
    as owner prose.
    """
    controls: Dict[str, Any] = {}
    while not incoming_messages.empty():
        try:
            injected = incoming_messages.get_nowait()
            if isinstance(injected, dict):
                _append_or_merge_user_content(messages, _owner_marked_content(build_user_content(injected)))
            else:
                _append_or_merge_user_message(messages, _owner_marked_content(injected))
        except queue.Empty:
            break

    if drive_root is not None and task_id:
        from ouroboros.owner_mailbox import KIND_FINALIZE_NOW, KIND_OWNER_TEXT, drain_owner_entries
        for entry in drain_owner_entries(drive_root, task_id=task_id, seen_ids=_owner_msg_seen):
            kind = entry.get("kind") or KIND_OWNER_TEXT
            if kind == KIND_FINALIZE_NOW:
                controls["finalize_now"] = str(entry.get("text") or "deadline")
                continue
            dmsg = entry.get("text") or ""
            _append_or_merge_user_message(messages, _owner_marked_content(dmsg))
            if event_queue is not None:
                try:
                    event_queue.put_nowait({
                        "type": "owner_message_injected",
                        "task_id": task_id,
                        "text": dmsg,
                    })
                except Exception:
                    pass
    return controls


def _run_round_compaction(
    messages: List[Dict[str, Any]],
    ctx: _CompactionRoundContext,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Run at most one transcript compaction for this round.

    Manual (pending) and emergency compaction always run; routine compaction
    covers local/low-context lanes plus known small-window (<=260K) remote
    models, and is skipped on self-check checkpoint rounds to avoid a
    duplicate summarizer call. Each branch persists a forensic checkpoint
    before compacting (P1: no silent truncation). Returns the possibly-rebound
    message list and any compaction usage record.
    """
    pending_compaction = getattr(ctx.tools._ctx, "_pending_compaction", None)
    if pending_compaction is not None:
        if _persist_compaction_checkpoint(
            messages, drive_root=ctx.drive_root, drive_logs=ctx.drive_logs, task_id=ctx.task_id,
            reason="manual", keep_recent=int(pending_compaction),
            round_idx=ctx.round_idx, event_queue=ctx.event_queue,
        ):
            messages, usage = compact_tool_history_llm(
                messages,
                keep_recent=pending_compaction,
                drive_root=ctx.drive_root,
                task_id=ctx.task_id,
            )
            ctx.tools._ctx._pending_compaction = None
            return messages, usage
        ctx.emit_progress("⚠️ Context compaction skipped: forensic checkpoint could not be persisted.")
        return messages, None

    emergency_chars = LOW_EMERGENCY_COMPACTION_CHARS if ctx.active_context_mode == "low" else EMERGENCY_COMPACTION_CHARS
    # Window-fit: when the active remote model's window is known, tighten the
    # trigger to fit it (profile constant stays the ceiling — never raised).
    # A 200K-window model in max mode previously kept the 1.2M-char trigger and
    # overflowed the provider long before emergency compaction ever fired.
    window_tokens = 0
    if not ctx.active_use_local:
        from ouroboros.context_budget import WINDOW_EMERGENCY_COMPACTION_FRACTION
        from ouroboros.provider_models import context_window_tokens
        window_tokens = context_window_tokens(ctx.active_model)
        if window_tokens > 0:
            emergency_chars = min(emergency_chars, int(window_tokens * 4 * WINDOW_EMERGENCY_COMPACTION_FRACTION))
    if _estimate_messages_chars(messages) > emergency_chars:
        # keep_recent must stay BELOW the current span count or the compactor
        # no-ops (len(spans) <= keep_recent returns as-is): a transcript over
        # the emergency byte threshold with only ~50 huge rounds previously
        # never compacted at all. Halve the history (floor 6), but ALWAYS
        # clamp below the span count so even 2-6 huge rounds compact; with a
        # single round there is nothing older to summarize.
        span_count = len(_tool_round_spans(messages))
        emergency_keep_recent = min(50, max(6, span_count // 2), max(1, span_count - 1))
        if _persist_compaction_checkpoint(
            messages, drive_root=ctx.drive_root, drive_logs=ctx.drive_logs, task_id=ctx.task_id,
            reason="emergency_context_size", keep_recent=emergency_keep_recent,
            round_idx=ctx.round_idx, event_queue=ctx.event_queue,
        ):
            return compact_tool_history_llm(
                messages,
                keep_recent=emergency_keep_recent,
                drive_root=ctx.drive_root,
                task_id=ctx.task_id,
            )
        ctx.emit_progress("⚠️ Emergency compaction skipped: forensic checkpoint could not be persisted.")
        return messages, None

    # Routine remote compaction runs only when local, in low context mode, or on
    # a small-window remote model; never on checkpoint rounds. Max with a big
    # window relies on emergency compaction to preserve prompt-cache hits.
    if ctx.active_context_mode != "low" and not ctx.active_use_local and window_tokens > 0:
        from ouroboros.context_budget import SMALL_WINDOW_ROUTINE_COMPACTION_TOKENS
        small_window_remote = window_tokens <= SMALL_WINDOW_ROUTINE_COMPACTION_TOKENS
    else:
        small_window_remote = False
    if not ctx.checkpoint_injected and (ctx.active_use_local or ctx.active_context_mode == "low" or small_window_remote):
        if ctx.round_idx > 6 and len(messages) > 40:
            if _persist_compaction_checkpoint(
                messages, drive_root=ctx.drive_root, drive_logs=ctx.drive_logs, task_id=ctx.task_id,
                reason="routine", keep_recent=20,
                round_idx=ctx.round_idx, event_queue=ctx.event_queue,
            ):
                return compact_tool_history_llm(
                    messages,
                    keep_recent=20,
                    drive_root=ctx.drive_root,
                    task_id=ctx.task_id,
                )
    return messages, None


@dataclass
class _RoundLimitContext:
    messages: List[Dict[str, Any]]
    llm: LLMClient
    active_model: str
    active_effort: str
    max_retries: int
    drive_logs: pathlib.Path
    task_id: str
    round_idx: int
    event_queue: Optional[queue.Queue]
    accumulated_usage: Dict[str, Any]
    task_type: str
    active_use_local: bool
    max_rounds: int
    deadline_ts: Optional[float] = None


def _handle_round_limit(ctx: _RoundLimitContext) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    finish_reason = f"⚠️ Task exceeded MAX_ROUNDS ({ctx.max_rounds}). Consider decomposing into subtasks via schedule_subagent."
    prompt = (
        f"[ROUND_LIMIT] {finish_reason} Produce your best final answer now from the "
        "verified work so far; clearly mark anything unverified or incomplete. An honest "
        "best-effort result is the expected outcome here, not a failure."
    )
    return _forced_final_answer(ctx, prompt=prompt, fallback_text=finish_reason, reason_code="round_limit")


def _handle_forced_finalization(ctx: _RoundLimitContext, reason: str) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Cooperative finalize-and-exit when the supervisor opens a grace window.

    The supervisor sends a typed finalize_now control through the owner
    mailbox when the task deadline/hard-timeout is reached; this extracts one
    tool-less best final answer inside the grace window so a deadline NEVER
    returns emptiness.
    """
    fallback = f"⚠️ Task reached {reason or 'deadline'}; finalization grace produced no answer."
    prompt = (
        f"[FINALIZE_NOW] The supervisor opened a finalization grace window (reason: {reason or 'deadline'}). "
        "The task will be stopped shortly. Produce your best final answer NOW from the verified "
        "work so far; clearly mark anything unverified or incomplete. An honest best-effort "
        "result is the expected outcome here, not a failure."
    )
    return _forced_final_answer(ctx, prompt=prompt, fallback_text=fallback, reason_code="finalization_grace")


def _forced_final_answer(
    ctx: _RoundLimitContext,
    *,
    prompt: str,
    fallback_text: str,
    reason_code: str,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Force one tool-less final answer; stamp the typed forced-finalization
    reason code (the best_effort outcome gate reads it downstream)."""
    llm_trace: Dict[str, Any] = {}
    _append_or_merge_user_message(ctx.messages, prompt)
    try:
        final_msg, _final_cost = call_llm_with_retry(
            ctx.llm, ctx.messages, ctx.active_model, None, ctx.active_effort,
            ctx.max_retries, ctx.drive_logs, ctx.task_id, ctx.round_idx, ctx.event_queue, ctx.accumulated_usage, ctx.task_type,
            use_local=ctx.active_use_local,
            deadline_ts=ctx.deadline_ts,
        )
        ctx.accumulated_usage["execution_status"] = "failed"
        ctx.accumulated_usage["reason_code"] = reason_code
        extracted = str((final_msg or {}).get("content") or "").strip()
        if extracted:
            # Typed fact for the best_effort outcome gate: a REAL model answer
            # was extracted (host fallback strings never set this).
            ctx.accumulated_usage["_best_effort_extracted"] = True
            return extracted, ctx.accumulated_usage, llm_trace
        return fallback_text, ctx.accumulated_usage, llm_trace
    except Exception:
        log.warning("Failed to get final response after %s", reason_code, exc_info=True)
        ctx.accumulated_usage["execution_status"] = "failed"
        ctx.accumulated_usage["reason_code"] = reason_code
        return fallback_text, ctx.accumulated_usage, llm_trace


def _apply_runtime_overrides(
    ctx: Any,
    active_model: str,
    active_use_local: bool,
    active_effort: str,
) -> Tuple[str, bool, str]:
    """Apply one-shot per-round model/locality/effort overrides from tool ctx."""
    if ctx.active_model_override:
        active_model = ctx.active_model_override
        ctx.active_model_override = None
    if getattr(ctx, "active_use_local_override", None) is not None:
        active_use_local = ctx.active_use_local_override
        ctx.active_use_local_override = None
    if ctx.active_effort_override:
        active_effort = normalize_reasoning_effort(ctx.active_effort_override, default=active_effort)
        ctx.active_effort_override = None
    return active_model, active_use_local, active_effort


def run_llm_loop(
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    incoming_messages: queue.Queue,
    task_type: str = "",
    task_id: str = "",
    budget_remaining_usd: Optional[float] = None,
    event_queue: Optional[queue.Queue] = None,
    initial_effort: str = "medium",
    drive_root: Optional[pathlib.Path] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Run the LLM-with-tools loop and return final text, usage, and trace."""
    ctx = tools._ctx
    task_model_override = str(getattr(ctx, "task_model_override", "") or "").strip()
    active_model = task_model_override or llm.default_model()
    active_effort = initial_effort
    if getattr(ctx, "task_use_local_override", None) is not None:
        active_use_local = bool(ctx.task_use_local_override)
    else:
        active_use_local = os.environ.get("USE_LOCAL_MAIN", "").lower() in ("true", "1")
    # Low context mode compacts the transcript sooner and enables remote routine compaction.
    active_context_mode = get_context_mode()

    llm_trace: Dict[str, Any] = {"reasoning_notes": [], "tool_calls": []}
    accumulated_usage: Dict[str, Any] = {}
    max_retries = 3
    from ouroboros.tools import tool_discovery as _td
    _td.set_registry(tools)

    tool_schemas = initial_tool_schemas(tools)
    tool_schemas, _enabled_extra_tools = _setup_dynamic_tools(tools, tool_schemas, messages)

    tools._ctx.event_queue = event_queue
    tools._ctx.task_id = task_id
    tools._ctx.messages = messages
    stateful_executor = StatefulToolExecutor()
    _owner_msg_seen: set = set()
    from ouroboros.config import SETTINGS_DEFAULTS as _DEFAULTS
    _max_rounds_default = int(_DEFAULTS["OUROBOROS_MAX_ROUNDS"])
    try:
        MAX_ROUNDS = max(1, int(os.environ.get("OUROBOROS_MAX_ROUNDS", str(_max_rounds_default))))
    except (ValueError, TypeError):
        MAX_ROUNDS = _max_rounds_default
        log.warning("Invalid OUROBOROS_MAX_ROUNDS, defaulting to %s", _max_rounds_default)
    round_idx = 0
    try:
        while True:
            round_idx += 1

            ctx = tools._ctx
            active_model, active_use_local, active_effort = _apply_runtime_overrides(
                ctx, active_model, active_use_local, active_effort,
            )

            # One forced-wrap-up context per round: consumed by the round-limit
            # path and the supervisor finalize_now control path below.
            limit_ctx = _RoundLimitContext(
                messages, llm, active_model, active_effort, max_retries,
                drive_logs, task_id, round_idx, event_queue,
                accumulated_usage, task_type, active_use_local, MAX_ROUNDS,
                deadline_ts=_task_deadline_epoch(tools),
            )
            if round_idx > MAX_ROUNDS:
                text, accumulated_usage, _ = _handle_round_limit(limit_ctx)
                return text, accumulated_usage, llm_trace

            _controls = _drain_incoming_messages(messages, incoming_messages, drive_root, task_id, event_queue, _owner_msg_seen)
            if _controls.get("finalize_now"):
                text, accumulated_usage, _ = _handle_forced_finalization(limit_ctx, str(_controls["finalize_now"]))
                return text, accumulated_usage, llm_trace

            # Inject after owner messages so the checkpoint is the LLM-call tail.
            # It is a normal user turn; only routine compaction is skipped below.
            _checkpoint_injected = _maybe_inject_self_check(
                round_idx, MAX_ROUNDS, messages, accumulated_usage, emit_progress,
                event_queue=event_queue, task_id=task_id, drive_logs=drive_logs,
            )
            _time_budget_injected = _maybe_inject_time_budget_milestone(
                messages, tools, event_queue=event_queue, task_id=task_id, drive_logs=drive_logs,
            )
            _checkpoint_injected = bool(_checkpoint_injected or _time_budget_injected)

            messages, _compaction_usage = _run_round_compaction(
                messages,
                _CompactionRoundContext(
                    tools=tools,
                    drive_root=drive_root,
                    drive_logs=drive_logs,
                    task_id=task_id,
                    round_idx=round_idx,
                    event_queue=event_queue,
                    active_use_local=active_use_local,
                    active_context_mode=active_context_mode,
                    checkpoint_injected=_checkpoint_injected,
                    emit_progress=emit_progress,
                    active_model=active_model,
                ),
            )
            if tools._ctx.messages is not messages:
                tools._ctx.messages = messages
            if _compaction_usage:
                add_usage(accumulated_usage, _compaction_usage)
                _cm = get_light_model()
                _cc = float(_compaction_usage.get("cost") or 0) or estimate_cost(
                    _cm, int(_compaction_usage.get("prompt_tokens") or 0),
                    int(_compaction_usage.get("completion_tokens") or 0),
                    int(_compaction_usage.get("cached_tokens") or 0),
                    int(_compaction_usage.get("cache_write_tokens") or 0),
                    _compaction_usage.get("prompt_cache_ttl"))
                emit_llm_usage_event(event_queue, task_id, _cm, _compaction_usage, _cc, "compaction")

            # Provider cache boundary; unsupported providers strip cache_control in llm.py.
            seal_task_transcript(messages)

            msg, cost = call_llm_with_retry(
                llm, messages, active_model, tool_schemas, active_effort,
                max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type,
                use_local=active_use_local,
                deadline_ts=_task_deadline_epoch(tools),
            )
            tools._ctx._current_llm_call_meta = dict(accumulated_usage.get("_last_llm_call_meta") or {})

            if msg is None:
                fallback_model = os.environ.get("OUROBOROS_MODEL_FALLBACK", "").strip()
                if not fallback_model or fallback_model == active_model:
                    failure = _no_response_failure_text(active_model, active_use_local, max_retries, accumulated_usage)
                    return failure, accumulated_usage, llm_trace

                fallback_use_local = os.environ.get("USE_LOCAL_FALLBACK", "").lower() in ("true", "1")
                primary_tag = " (local)" if active_use_local else ""
                fallback_tag = " (local)" if fallback_use_local else ""
                emit_progress(f"⚡ Fallback: {active_model}{primary_tag} → {fallback_model}{fallback_tag} after empty response")
                msg, fallback_cost = call_llm_with_retry(
                    llm, messages, fallback_model, tool_schemas, active_effort,
                    max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type,
                    use_local=fallback_use_local,
                    deadline_ts=_task_deadline_epoch(tools),
                )

                if msg is None:
                    return (
                        f"⚠️ All models are down. Primary ({active_model}{primary_tag}) and fallback ({fallback_model}{fallback_tag}) "
                        f"both returned no response. Stopping.{_provider_failure_hint(accumulated_usage)} "
                        f"{_provider_recovery_hint(accumulated_usage)}"
                    ), accumulated_usage, llm_trace

            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")
            if not tool_calls:
                if _force_plan_required(tools._ctx, llm_trace):
                    attempts = int(getattr(tools._ctx, "_force_plan_reminder_count", 0) or 0)
                    if attempts >= 2:
                        accumulated_usage["execution_status"] = "failed"
                        accumulated_usage["reason_code"] = "consilium_force_plan_not_called"
                        return (
                            "⚠️ CONSILIUM_FORCE_PLAN_BLOCKED: plan_task was required for this Consilium task but was not called.",
                            accumulated_usage,
                            llm_trace,
                        )
                    tools._ctx._force_plan_reminder_count = attempts + 1
                    if content and content.strip():
                        messages.append({"role": "assistant", "content": content})
                    _append_or_merge_user_message(
                        messages,
                        "[CONSILIUM_FORCE_PLAN] plan_task is required before finalizing this task. "
                        "Call plan_task now with an appropriate context_level, then continue.",
                    )
                    emit_progress("Consilium force-plan reminder injected before final response.")
                    llm_trace["reasoning_notes"].append("Consilium force-plan reminder injected before final response.")
                    continue
                handoff_msg = ""
                if drive_root is not None and task_id:
                    try:
                        from ouroboros.task_status import FINAL_STATUSES, find_child_tasks, format_handoff_message

                        metadata = getattr(tools._ctx, "task_metadata", {}) if isinstance(getattr(tools._ctx, "task_metadata", {}), dict) else {}
                        children = find_child_tasks(
                            drive_root,
                            parent_task_id=task_id,
                            root_task_id=str(metadata.get("root_task_id") or task_id),
                            exclude_task_id=task_id,
                        )
                        signature = "|".join(
                            f"{child.get('task_id') or child.get('id')}:{child.get('status')}:{len(str(child.get('result') or ''))}"
                            for child in children
                        )
                        previous = getattr(tools._ctx, "_subagent_handoff_signature", "")
                        nonterminal_children = [
                            child for child in children
                            if str(child.get("status") or "").strip().lower() not in FINAL_STATUSES
                        ]
                        needs_incomplete_ack = bool(nonterminal_children) and not _final_text_acknowledges_incomplete_children(content, nonterminal_children)
                        if children and signature and (signature != previous or needs_incomplete_ack):
                            tools._ctx._subagent_handoff_signature = signature
                            handoff_msg = format_handoff_message(children)
                    except Exception:
                        log.debug("Failed to build subagent handoff reminder", exc_info=True)
                if handoff_msg:
                    if content and content.strip():
                        messages.append({"role": "assistant", "content": content})
                    _append_or_merge_user_message(messages, f"[SYSTEM REMINDER]\n{handoff_msg}")
                    emit_progress("Subagent handoff status refreshed before final response.")
                    llm_trace["reasoning_notes"].append("Subagent handoff status refreshed before final response.")
                    continue
                finalization_msg = _skill_finalization_message(drive_root, llm_trace) if drive_root is not None else ""
                if finalization_msg and not getattr(tools._ctx, "_skill_finalization_injected", False):
                    tools._ctx._skill_finalization_injected = True
                    if content and content.strip():
                        messages.append({"role": "assistant", "content": content})
                    _append_or_merge_user_message(messages, f"[SYSTEM REMINDER]\n{finalization_msg}")
                    emit_progress(finalization_msg)
                    llm_trace["reasoning_notes"].append(finalization_msg)
                    continue
                if _run_task_acceptance_review_once(
                    tools=tools,
                    content=content or "",
                    task_id=task_id,
                    task_type=task_type,
                    llm_trace=llm_trace,
                    drive_root=drive_root,
                    messages=messages,
                    emit_progress=emit_progress,
                ):
                    continue
                return _handle_text_response(content, llm_trace, accumulated_usage)

            if getattr(tools._ctx, "_skill_finalization_injected", False):
                tools._ctx._skill_finalization_injected = False
            assistant_msg = dict(msg)
            assistant_msg.setdefault("role", "assistant")
            messages.append(assistant_msg)

            progress_text = str(content or "").strip()
            if progress_text:
                emit_progress(progress_text.strip())
                llm_trace["reasoning_notes"].append(progress_text.strip())

            handle_tool_calls(
                tool_calls, tools, drive_logs, task_id, stateful_executor,
                messages, llm_trace, emit_progress
            )

            budget_result = _check_budget_limits(
                budget_remaining_usd, accumulated_usage, round_idx, messages,
                llm, active_model, active_effort, max_retries, drive_logs,
                task_id, event_queue, llm_trace, task_type, active_use_local,
                deadline_ts=_task_deadline_epoch(tools),
            )
            if budget_result is not None:
                return budget_result

    finally:
        if stateful_executor:
            try:
                from ouroboros.tools.browser import cleanup_browser
                stateful_executor.submit(cleanup_browser, tools._ctx).result(timeout=5)
            except Exception:
                log.debug("Browser cleanup on executor thread failed or timed out", exc_info=True)
            try:
                stateful_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                log.warning("Failed to shutdown stateful executor", exc_info=True)
        if drive_root is not None and task_id:
            try:
                from ouroboros.tools.services import stop_task_services

                finalized_services = stop_task_services(tools._ctx)
                stopped_services = [s for s in finalized_services if s.get("lifecycle") != "kept"]
                kept_services = [s for s in finalized_services if s.get("lifecycle") == "kept"]
                if stopped_services:
                    _emit_checkpoint_event(event_queue, task_id, drive_logs, {
                        "checkpoint_kind": "services_stopped",
                        "services": stopped_services,
                    })
                    llm_trace.setdefault("verification_events", []).append({
                        "kind": "services_stopped",
                        "services": stopped_services,
                    })
                if kept_services:
                    # Survivors are deliberate (keep_alive / service_teardown=keep):
                    # record pid/port metadata so the external party that asked for
                    # them (verifier, owner) knows what it now owns.
                    _emit_checkpoint_event(event_queue, task_id, drive_logs, {
                        "checkpoint_kind": "services_kept",
                        "services": kept_services,
                    })
                    llm_trace.setdefault("verification_events", []).append({
                        "kind": "services_kept",
                        "services": kept_services,
                    })
            except Exception:
                log.debug("Failed to stop task services", exc_info=True)
            try:
                from ouroboros.owner_mailbox import cleanup_task_mailbox
                cleanup_task_mailbox(drive_root, task_id)
            except Exception:
                log.debug("Failed to cleanup task mailbox", exc_info=True)
