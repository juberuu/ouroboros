"""Generate post-task process-memory reflections for non-trivial/error runs."""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros.utils import utc_now_iso, append_jsonl


def _truncate_with_notice(text: Any, limit: int) -> str:
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    marker = f"... [+{len(raw)} chars]"
    available = max(0, limit - len(marker))
    marker = f"... [+{len(raw) - available} chars]"
    available = max(0, limit - len(marker))
    return raw[:available] + marker

log = logging.getLogger(__name__)

# Reflection triggers for non-trivial clean tasks.
NONTRIVIAL_ROUNDS_THRESHOLD: int = 15
NONTRIVIAL_COST_THRESHOLD: float = 5.0

_ERROR_MARKERS = frozenset({
    "REVIEW_BLOCKED",
    "TESTS_FAILED",
    "COMMIT_BLOCKED",
    "REVIEW_MAX_ITERATIONS",
    "TOOL_ERROR",
    "TOOL_TIMEOUT",
    "SHELL_EXIT_ERROR",
    "SHELL_ERROR",
    "CLAUDE_CODE_ERROR",
    "CLAUDE_CODE_TIMEOUT",
    "CLAUDE_CODE_INSTALL_ERROR",
    "CLAUDE_CODE_UNAVAILABLE",
})

REFLECTIONS_FILENAME = "task_reflections.jsonl"

_REFLECTION_PROMPT_ERROR = """\
You are performing a post-task experience review for Ouroboros, a self-modifying AI agent.
The task had errors or blocking events. Write a concise 150-250 word reflection covering:

1. What was the goal?
2. What specific errors/blocks occurred?
3. What was the root cause (if identifiable)?
4. What should be done differently next time?

Be concrete — cite specific file names, tool names, error messages. No platitudes.
If structured review evidence exists, incorporate the critical/advisory findings and
open obligations into the root-cause analysis. Mention them individually with their
severity and item/tag identity rather than collapsing them into a generic "review failed".\
"""

_REFLECTION_PROMPT_NONTRIVIAL = """\
You are performing a post-task experience review for Ouroboros, a self-modifying AI agent.
The task was non-trivial (high round count or high cost) but completed without hard errors.
Write a concise 150-250 word reflection covering:

1. What was the goal?
2. What took the most rounds/cost? Where was the friction?
3. Were there weak assumptions, unnecessary detours, or suboptimal tool choices?
4. What would make a similar task cheaper or faster next time?

Be concrete — cite specific file names, tool names, decision points. No platitudes.\
"""

# Shared tail with {format} fields.
_REFLECTION_PROMPT_TAIL = """

Then, if this task produced durable, reusable self-knowledge worth persisting now,
append a line:
MEMORY_ACTIONS_JSON: [...]
A JSON array of 0-3 objects. Each object must have:
- type: one of "scratchpad_append", "knowledge_write", "identity_update_candidate"
- content: concise, concrete text to persist
Optional field:
- topic: REQUIRED only for knowledge_write (short slug, e.g. "review_process")
Rules for memory actions:
- scratchpad_append: a durable working-memory note useful for near-future tasks.
- knowledge_write: a reusable fact/procedure stored in the knowledge base under `topic`.
- identity_update_candidate: a PROPOSED identity refinement; it is only recorded as a
  review candidate in the scratchpad, never auto-applied to identity.md (avoid drift).
- Persist only genuinely durable, reusable learning, not task-specific trivia.
- If nothing deserves persisting, output MEMORY_ACTIONS_JSON: []

Then, if there is at least one concrete deferred improvement worth tracking, append a final line:
BACKLOG_CANDIDATES_JSON: [...]
Use a JSON array of 0-3 objects. Each object must have:
- summary
- category
- source
- evidence
Optional fields:
- context
- proposed_next_step
- task_id
- requires_plan_review
- priority (high | med | low — how valuable/urgent this is; default med)
- kind (bug | improvement | capability_idea — use capability_idea for a forward-looking NEW ability worth building, not just a fix; default improvement)
Rules for candidates:
- Only include concrete, evidence-backed follow-ups that are OUT OF SCOPE for the current task.
- Prefer recurring process/tool/review friction over one-off noise.
- Capability ideas are welcome (kind=capability_idea), not only bugs — but keep them concrete and evidence-backed.
- Core/code improvements should become explicit Evolution Campaign candidates
  or backlog items; do not assume they were already executed.
- If nothing deserves backlog tracking, output BACKLOG_CANDIDATES_JSON: []
- Tool arguments in logs may show `<TRUNCATED:key:Nch:sha=...>` placeholders.
  That is logging metadata, not the value passed to the tool.

## Task goal

{goal}

## Execution trace

{trace_summary}

## Error details

{error_details}

## Structured review evidence

{review_evidence}

## Related child/subtask evidence

{child_evidence}

Write the reflection now. Plain text, no markdown headers except the exact final
MEMORY_ACTIONS_JSON and BACKLOG_CANDIDATES_JSON lines.
"""

_REFLECTION_PROMPT_ERROR_FULL = _REFLECTION_PROMPT_ERROR + _REFLECTION_PROMPT_TAIL
_REFLECTION_PROMPT_NONTRIVIAL_FULL = _REFLECTION_PROMPT_NONTRIVIAL + _REFLECTION_PROMPT_TAIL


def should_generate_reflection(
    llm_trace: Dict[str, Any],
    *,
    task: Optional[Dict[str, Any]] = None,
    rounds: int = 0,
    cost_usd: float = 0.0,
) -> bool:
    """Return True for tool errors/blocking markers or costly many-round tasks."""
    task = task or {}
    if str(task.get("type") or "") in {"evolution", "deep_self_review"}:
        return True
    if str(task.get("workspace_root") or "").strip() or str(task.get("workspace_mode") or "").strip():
        return True
    if rounds >= NONTRIVIAL_ROUNDS_THRESHOLD:
        return True
    if cost_usd >= NONTRIVIAL_COST_THRESHOLD:
        return True

    tool_calls = llm_trace.get("tool_calls") or []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        if tc.get("is_error") or str(tc.get("status") or "").strip().lower() not in ("", "ok"):
            return True
        result_str = str(tc.get("result", ""))
        for marker in _ERROR_MARKERS:
            if marker in result_str:
                return True

    return False


def _has_error_evidence(llm_trace: Dict[str, Any]) -> bool:
    """Return True when the trace contains tool errors or blocking markers."""
    tool_calls = llm_trace.get("tool_calls") or []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        if tc.get("is_error") or str(tc.get("status") or "").strip().lower() not in ("", "ok"):
            return True
        result_str = str(tc.get("result", ""))
        for marker in _ERROR_MARKERS:
            if marker in result_str:
                return True
    return False

def _collect_error_details(llm_trace: Dict[str, Any], cap: int = 3000) -> str:
    """Extract error tool results from the trace, up to *cap* chars."""
    parts: List[str] = []
    total = 0
    tool_calls = llm_trace.get("tool_calls") or []

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        result_str = str(tc.get("result", ""))
        is_error = tc.get("is_error") or str(tc.get("status") or "").strip().lower() not in ("", "ok")
        is_relevant = is_error or any(m in result_str for m in _ERROR_MARKERS)
        if not is_relevant:
            continue
        tool_name = tc.get("tool", "unknown")
        facts = []
        status = str(tc.get("status") or "").strip()
        if status:
            facts.append(f"status={status}")
        if tc.get("exit_code") not in (None, ""):
            facts.append(f"exit_code={tc.get('exit_code')}")
        if tc.get("signal"):
            facts.append(f"signal={tc.get('signal')}")
        fact_prefix = f" ({', '.join(facts)})" if facts else ""
        snippet = f"[{tool_name}{fact_prefix}]: {result_str}"
        if total + len(snippet) > cap:
            remaining = cap - total
            if remaining > 50:
                parts.append(_truncate_with_notice(snippet, remaining))
            break
        parts.append(snippet)
        total += len(snippet)

    return "\n\n".join(parts) if parts else "(no error details captured)"


def _detect_markers(llm_trace: Dict[str, Any]) -> List[str]:
    """Return list of error marker strings found in the trace."""
    found: set = set()
    for tc in (llm_trace.get("tool_calls") or []):
        result_str = str(tc.get("result", "") if isinstance(tc, dict) else "")
        for marker in _ERROR_MARKERS:
            if marker in result_str:
                found.add(marker)
    return sorted(found)


_ALLOWED_MEMORY_ACTION_TYPES = frozenset({
    "scratchpad_append",
    "knowledge_write",
    "identity_update_candidate",
})


def _extract_trailing_json(text: str, marker: str) -> tuple[str, Optional[list]]:
    """Peel a ``MARKER: [...]`` block out of *text* regardless of its position.

    Removes only the marker and its JSON array (located via a tolerant
    ``raw_decode``), preserving any other marker line so callers can extract
    multiple markers in any order without silently dropping one. Returns
    ``(remaining_text, parsed_list_or_None)``: a present-but-empty payload is
    ``[]``; a malformed payload is ``None`` so the caller can distinguish
    "no items" from "parse failure".
    """
    idx = text.rfind(marker)
    if idx == -1:
        return text, None
    after = text[idx + len(marker):]
    stripped = after.lstrip()
    lead = len(after) - len(stripped)
    if not stripped:
        return text[:idx].rstrip(), []
    try:
        value, end = json.JSONDecoder().raw_decode(stripped)
    except Exception:
        log.warning("Reflection %s JSON parse failed", marker, exc_info=True)
        return text[:idx].rstrip(), None
    remainder = (text[:idx] + after[lead + end:]).rstrip()
    return remainder, value if isinstance(value, list) else None


def _validate_memory_actions(raw: Any, task_id: str) -> List[Dict[str, Any]]:
    """Keep only well-formed, allowed-type memory actions (max 3)."""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:10]:
        if len(out) >= 3:
            break
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("type") or "").strip()
        if action_type not in _ALLOWED_MEMORY_ACTION_TYPES:
            continue
        content = _truncate_with_notice(item.get("content", ""), 1200).strip()
        if not content:
            continue
        action: Dict[str, Any] = {"type": action_type, "content": content, "task_id": task_id}
        if action_type == "knowledge_write":
            topic = _truncate_with_notice(item.get("topic", ""), 80).strip()
            if not topic:
                continue
            action["topic"] = topic
        out.append(action)
    return out

def generate_reflection(
    task: Dict[str, Any],
    llm_trace: Dict[str, Any],
    trace_summary: str,
    llm_client: Any,
    usage_dict: Dict[str, Any],
    review_evidence: Optional[Dict[str, Any]] = None,
    child_evidence: str = "",
) -> Dict[str, Any]:
    """Call the light LLM and return a JSONL-ready reflection entry."""
    from ouroboros.config import get_light_model

    goal = _truncate_with_notice(task.get("text", ""), 200)
    error_details = _collect_error_details(llm_trace)
    markers = _detect_markers(llm_trace)
    error_count = sum(
        1 for tc in (llm_trace.get("tool_calls") or [])
        if isinstance(tc, dict) and (
            tc.get("is_error")
            or str(tc.get("status") or "").strip().lower() not in ("", "ok")
        )
    )
    try:
        from ouroboros.review_evidence import format_review_evidence_for_prompt
        review_evidence_text = format_review_evidence_for_prompt(review_evidence or {}, max_chars=8000)
    except Exception:
        review_evidence_text = "(review evidence unavailable)"

    if _has_error_evidence(llm_trace) or markers:
        prompt_template = _REFLECTION_PROMPT_ERROR_FULL
    else:
        prompt_template = _REFLECTION_PROMPT_NONTRIVIAL_FULL

    prompt = prompt_template.format(
        goal=goal or "(no goal text)",
        trace_summary=_truncate_with_notice(trace_summary, 2000),
        error_details=error_details,
        review_evidence=review_evidence_text,
        child_evidence=child_evidence or "(none)",
    )

    light_model = get_light_model()
    try:
        from ouroboros.llm_observability import chat_observed

        resp_msg, refl_usage = chat_observed(
            llm_client,
            drive_root=pathlib.Path(str(task.get("drive_root") or "../data")),
            task_id=str(task.get("id") or task.get("task_id") or "reflection"),
            call_type="task_reflection",
            messages=[{"role": "user", "content": prompt}],
            model=light_model,
            reasoning_effort="low",
            max_tokens=16384,
        )
        raw_reflection_text = (resp_msg.get("content") or "").strip()
        task_id_str = str(task.get("id", "") or "")

        # Backlog is the last trailing line; peel it first, then memory actions.
        body_after_backlog, raw_candidates = _extract_trailing_json(
            raw_reflection_text, "BACKLOG_CANDIDATES_JSON:"
        )
        reflection_text, raw_memory_actions = _extract_trailing_json(
            body_after_backlog, "MEMORY_ACTIONS_JSON:"
        )
        reflection_text = reflection_text.strip()

        backlog_candidates: List[Dict[str, Any]] = []
        if isinstance(raw_candidates, list):
            for raw in raw_candidates[:3]:
                if not isinstance(raw, dict):
                    continue
                summary = _truncate_with_notice(raw.get("summary", ""), 260).strip()
                category = _truncate_with_notice(raw.get("category", "process"), 80).strip() or "process"
                source = _truncate_with_notice(raw.get("source", "execution_reflection"), 80).strip() or "execution_reflection"
                evidence = _truncate_with_notice(raw.get("evidence", ""), 220).strip()
                if not summary or not evidence:
                    continue
                backlog_candidates.append({
                    "summary": summary,
                    "category": category,
                    "source": source,
                    "evidence": evidence,
                    "context": _truncate_with_notice(raw.get("context", ""), 400).strip(),
                    "proposed_next_step": _truncate_with_notice(raw.get("proposed_next_step", ""), 260).strip(),
                    "task_id": _truncate_with_notice(raw.get("task_id", task_id_str), 80).strip() or task_id_str,
                    "requires_plan_review": bool(raw.get("requires_plan_review", True)),
                    "priority": _truncate_with_notice(raw.get("priority", "med"), 10).strip().lower() or "med",
                    "kind": _truncate_with_notice(raw.get("kind", "improvement"), 40).strip() or "improvement",
                })
        memory_actions = _validate_memory_actions(raw_memory_actions, task_id_str)

        # Reflection runs outside the tool-event loop; update budget directly.
        if refl_usage:
            try:
                from supervisor.state import update_budget_from_usage
                update_budget_from_usage(refl_usage)
            except Exception:
                pass
    except Exception as e:
        log.warning("Reflection LLM call failed: %s", e)
        reflection_text = f"(reflection generation failed: {e})"
        backlog_candidates = []
        memory_actions = []

    return {
        "ts": utc_now_iso(),
        "task_id": task.get("id", ""),
        "task_type": str(task.get("type", "")),
        "goal": goal,
        "rounds": int(usage_dict.get("rounds", 0)),
        "cost_usd": round(float(usage_dict.get("cost", 0)), 4),
        "error_count": error_count,
        "key_markers": markers,
        "review_evidence": review_evidence or {},
        "reflection": reflection_text,
        "backlog_candidates": backlog_candidates,
        "memory_actions": memory_actions,
    }


def apply_memory_actions(env: Any, actions: List[Dict[str, Any]]) -> int:
    """Apply experience-review memory actions to ``env.drive_root``.

    Routes through the existing provenance-preserving memory/knowledge paths.
    Identity is intentionally conservative: an ``identity_update_candidate`` is
    recorded in the scratchpad for review, never auto-written to identity.md, so
    autonomous learning cannot silently drift the personality. Returns the count
    of actions applied.
    """
    applied = 0
    for action in (actions or [])[:3]:
        atype = str(action.get("type") or "")
        content = str(action.get("content") or "").strip()
        if not content:
            continue
        try:
            if atype == "scratchpad_append":
                from ouroboros.memory import Memory

                Memory(env.drive_root, getattr(env, "repo_dir", None)).append_scratchpad_block(
                    content,
                    source="experience_review",
                    metadata={"task_id": str(action.get("task_id") or "")},
                )
                applied += 1
            elif atype == "knowledge_write":
                topic = str(action.get("topic") or "").strip()
                if not topic:
                    continue
                from ouroboros.tools.knowledge import _knowledge_write
                from ouroboros.tools.registry import ToolContext

                ctx = ToolContext(repo_dir=getattr(env, "repo_dir", env.drive_root), drive_root=env.drive_root)
                _knowledge_write(ctx, topic, content, mode="append")
                applied += 1
            elif atype == "identity_update_candidate":
                from ouroboros.memory import Memory

                Memory(env.drive_root, getattr(env, "repo_dir", None)).append_scratchpad_block(
                    "IDENTITY UPDATE CANDIDATE (review before applying to identity.md):\n" + content,
                    source="experience_review_identity_candidate",
                    metadata={"task_id": str(action.get("task_id") or "")},
                )
                applied += 1
        except Exception:
            log.debug("Failed to apply reflection memory action %s", atype, exc_info=True)
    return applied


def append_reflection(drive_root: pathlib.Path, entry: Dict[str, Any]) -> None:
    """Persist a reflection entry to the JSONL file."""
    reflections_path = drive_root / "logs" / REFLECTIONS_FILENAME
    try:
        append_jsonl(reflections_path, entry)
        log.info("Execution reflection saved (task=%s, markers=%s)",
                 entry.get("task_id", "?"), entry.get("key_markers", []))
    except Exception:
        log.warning("Failed to save execution reflection", exc_info=True)

    if entry.get("key_markers"):
        try:
            _update_patterns(drive_root, entry)
        except Exception:
            log.debug("Pattern register update failed (non-critical)", exc_info=True)

_PATTERNS_PROMPT = """\
You maintain a Pattern Register for Ouroboros, a self-modifying AI agent.
Below is the current register and a new error reflection. Update the register.

Rules:
- If this is a NEW error class: add a row.
- If this is a RECURRING class: increment count, update root cause/fix if you have better info.
- Keep the markdown table format.
- Be concrete: cite file names, tool names, error types.
- Max 20 rows. If full, merge least-important entries.

## Current register

{current_patterns}

## New reflection

Task: {goal}
Markers: {markers}
Reflection: {reflection}

Output ONLY the updated markdown table (with header). No extra text.
"""

_PATTERNS_HEADER = (
    "# Pattern Register\n\n"
    "| Error class | Count | Root cause | Structural fix | Status |\n"
    "|-------------|-------|------------|----------------|--------|\n"
)


def _update_patterns(drive_root: pathlib.Path, entry: Dict[str, Any]) -> None:
    """Update the Pattern Register topic via LLM."""
    from ouroboros.config import get_light_model
    from ouroboros.llm import LLMClient

    patterns_path = drive_root / "memory" / "knowledge" / "patterns.md"
    patterns_path.parent.mkdir(parents=True, exist_ok=True)

    if patterns_path.exists():
        current = patterns_path.read_text(encoding="utf-8")
    else:
        current = _PATTERNS_HEADER

    current_truncated = _truncate_with_notice(current, 3000)
    prompt = _PATTERNS_PROMPT.format(
        current_patterns=(
            current_truncated
            + (
                "\n\n[IMPORTANT: The current register was compacted for prompt size. "
                "Preserve existing rows unless you are intentionally merging or updating them.]"
                if len(current) > 3000 else ""
            )
        ),
        goal=_truncate_with_notice(entry.get("goal", "?"), 200),
        markers=", ".join(entry.get("key_markers", [])),
        reflection=_truncate_with_notice(entry.get("reflection", ""), 500),
    )

    light_model = get_light_model()
    client = LLMClient()
    from ouroboros.llm_observability import chat_observed

    resp_msg, patterns_usage = chat_observed(
        client,
        drive_root=drive_root,
        task_id=str(entry.get("task_id") or entry.get("id") or "patterns"),
        call_type="pattern_register_update",
        messages=[{"role": "user", "content": prompt}],
        model=light_model,
        reasoning_effort="low",
        max_tokens=16384,
    )
    # Pattern update also runs outside the tool-event loop.
    if patterns_usage:
        try:
            from supervisor.state import update_budget_from_usage
            update_budget_from_usage(patterns_usage)
        except Exception:
            pass
    updated = (resp_msg.get("content") or "").strip()
    if not updated or "|" not in updated:
        log.warning("Pattern register LLM returned invalid output, skipping update")
        return

    if not updated.startswith("#"):
        updated = "# Pattern Register\n\n" + updated

    append_jsonl(drive_root / "memory" / "knowledge" / "patterns_history.jsonl", {
        "ts": utc_now_iso(),
        "task_id": str(entry.get("task_id") or ""),
        "markers": list(entry.get("key_markers") or []),
        "old_content": current,
        "new_content": updated + "\n",
    })
    patterns_path.write_text(updated + "\n", encoding="utf-8")
    log.info("Pattern register updated (%d chars)", len(updated))

    try:
        from ouroboros.consolidator import _rebuild_knowledge_index
        _rebuild_knowledge_index(patterns_path.parent)
    except Exception:
        log.debug("Failed to rebuild knowledge index after patterns update", exc_info=True)
