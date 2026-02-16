"""
Ouroboros — LLM tool loop.

Core loop: send messages to LLM, execute tool calls, repeat until final response.
Extracted from agent.py to keep the agent thin.
"""

from __future__ import annotations

import json
import pathlib
import queue
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

from ouroboros.llm import LLMClient, normalize_reasoning_effort, reasoning_rank, add_usage
from ouroboros.tools.registry import ToolRegistry
from ouroboros.context import compact_tool_history
from ouroboros.utils import utc_now_iso, append_jsonl, truncate_for_log, sanitize_tool_args_for_log, sanitize_tool_result_for_log

READ_ONLY_PARALLEL_TOOLS = frozenset({
    "repo_read", "repo_list",
    "drive_read", "drive_list",
    "web_search", "codebase_digest", "chat_history",
})


def _truncate_tool_result(result: Any) -> str:
    """
    Hard-cap tool result string to 3000 characters.
    If truncated, append a note with the original length.
    """
    result_str = str(result)
    if len(result_str) <= 3000:
        return result_str
    original_len = len(result_str)
    return result_str[:3000] + f"\n... (truncated from {original_len} chars)"


def _execute_single_tool(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    task_id: str = "",
) -> Dict[str, Any]:
    """
    Execute a single tool call and return all needed info.

    Returns dict with: tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS

    # Parse arguments
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
    except (json.JSONDecodeError, ValueError) as e:
        result = f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{fn_name}': {e}"
        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": result,
            "is_error": True,
            "args_for_log": {},
            "is_code_tool": is_code_tool,
        }

    args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})

    # Execute tool
    tool_ok = True
    try:
        result = tools.execute(fn_name, args)
    except Exception as e:
        tool_ok = False
        result = f"⚠️ TOOL_ERROR ({fn_name}): {type(e).__name__}: {e}"
        append_jsonl(drive_logs / "events.jsonl", {
            "ts": utc_now_iso(), "type": "tool_error", "task_id": task_id,
            "tool": fn_name, "args": args_for_log, "error": repr(e),
        })

    # Log tool execution (sanitize secrets from result before persisting)
    append_jsonl(drive_logs / "tools.jsonl", {
        "ts": utc_now_iso(), "tool": fn_name, "task_id": task_id,
        "args": args_for_log,
        "result_preview": sanitize_tool_result_for_log(truncate_for_log(result, 2000)),
    })

    is_error = (not tool_ok) or str(result).startswith("⚠️")

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": is_error,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
    }


def _execute_with_timeout(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
) -> Dict[str, Any]:
    """
    Execute a tool call with a hard timeout.

    On timeout: returns TOOL_TIMEOUT error so the LLM regains control.
    The hung worker thread leaks as daemon — watchdog handles recovery.
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_execute_single_tool, tools, tc, drive_logs, task_id)
        try:
            return future.result(timeout=timeout_sec)
        except TimeoutError:
            args_for_log = {}
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})
            except Exception:
                pass
            result = (
                f"⚠️ TOOL_TIMEOUT ({fn_name}): exceeded {timeout_sec}s limit. "
                f"The tool is still running in background but control is returned to you. "
                f"Try a different approach or inform the owner about the issue."
            )
            append_jsonl(drive_logs / "events.jsonl", {
                "ts": utc_now_iso(), "type": "tool_timeout",
                "tool": fn_name, "args": args_for_log,
                "timeout_sec": timeout_sec,
            })
            append_jsonl(drive_logs / "tools.jsonl", {
                "ts": utc_now_iso(), "tool": fn_name,
                "args": args_for_log, "result_preview": result,
            })
            return {
                "tool_call_id": tool_call_id,
                "fn_name": fn_name,
                "result": result,
                "is_error": True,
                "args_for_log": args_for_log,
                "is_code_tool": is_code_tool,
            }


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
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Core LLM-with-tools loop.

    Sends messages to LLM, executes tool calls, retries on errors,
    escalates reasoning effort for long tasks.

    Args:
        budget_remaining_usd: If set, forces completion when task cost exceeds 50% of this budget

    Returns: (final_text, accumulated_usage, llm_trace)
    """
    profile_name = llm.select_task_profile(task_type)
    profile_cfg = llm.model_profile(profile_name)
    active_model = profile_cfg["model"]
    active_effort = profile_cfg["effort"]

    llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}
    accumulated_usage: Dict[str, Any] = {}
    max_retries = 3
    soft_check_interval = 20

    tool_schemas = tools.schemas()

    def _maybe_raise_effort(target: str) -> None:
        nonlocal active_effort
        t = normalize_reasoning_effort(target, default=active_effort)
        if reasoning_rank(t) > reasoning_rank(active_effort):
            active_effort = t

    def _switch_to_code_profile() -> None:
        nonlocal active_model, active_effort
        code_cfg = llm.model_profile("code_task")
        if code_cfg["model"] != active_model or reasoning_rank(code_cfg["effort"]) > reasoning_rank(active_effort):
            active_model = code_cfg["model"]
            active_effort = max(active_effort, code_cfg["effort"], key=reasoning_rank)

    round_idx = 0
    while True:
        round_idx += 1

        # Inject owner messages received during task execution
        while not incoming_messages.empty():
            try:
                injected = incoming_messages.get_nowait()
                messages.append({"role": "user", "content": injected})
            except queue.Empty:
                break

        # Self-check: soft cost-awareness, LLM decides whether to continue
        if round_idx > 1 and round_idx % soft_check_interval == 0:
            task_cost = accumulated_usage.get("cost", 0)
            task_tokens = accumulated_usage.get("prompt_tokens", 0)
            cached = accumulated_usage.get("cached_tokens", 0)
            cache_pct = int(cached * 100 / max(task_tokens, 1)) if cached > 0 else 0
            cache_str = f" ({cache_pct}% cached)" if cache_pct > 0 else ""
            self_check_msg = (
                f"[Self-check] {round_idx} rounds, ${task_cost:.2f} spent, "
                f"{task_tokens:,} prompt tokens{cache_str}. Assess progress. "
                f"If stuck, change approach."
            )
            messages.append({"role": "system", "content": self_check_msg})
            # Log self-check injection to supervisor
            append_jsonl(drive_logs / "events.jsonl", {
                "ts": utc_now_iso(), "type": "self_check",
                "task_id": task_id, "round": round_idx,
                "message": self_check_msg,
                "cost_usd": task_cost, "prompt_tokens": task_tokens,
                "cache_pct": cache_pct,
            })

        # Escalate reasoning effort for long tasks
        if round_idx >= 5:
            _maybe_raise_effort("high")
        if round_idx >= 10:
            _maybe_raise_effort("xhigh")

        # Compact old tool history to save tokens on long conversations
        if round_idx > 1:
            messages = compact_tool_history(messages, keep_recent=4)

        # --- LLM call with retry ---
        msg = None
        last_error: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                resp_msg, usage = llm.chat(
                    messages=messages, model=active_model, tools=tool_schemas,
                    reasoning_effort=active_effort,
                )
                msg = resp_msg
                add_usage(accumulated_usage, usage)
                accumulated_usage["rounds"] = accumulated_usage.get("rounds", 0) + 1
                # Log per-round metrics
                _round_event = {
                    "ts": utc_now_iso(), "type": "llm_round",
                    "task_id": task_id,
                    "round": round_idx, "model": active_model,
                    "reasoning_effort": active_effort,
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "cached_tokens": int(usage.get("cached_tokens") or 0),
                    "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
                    "cost_usd": float(usage.get("cost") or 0),
                }
                append_jsonl(drive_logs / "events.jsonl", _round_event)
                break
            except Exception as e:
                last_error = e
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(), "type": "llm_api_error",
                    "task_id": task_id,
                    "round": round_idx, "attempt": attempt + 1,
                    "model": active_model, "error": repr(e),
                })
                if attempt < max_retries - 1:
                    time.sleep(min(2 ** attempt * 2, 30))

        if msg is None:
            return (
                f"⚠️ Не удалось получить ответ от модели после {max_retries} попыток.\n"
                f"Ошибка: {last_error}"
            ), accumulated_usage, llm_trace

        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content")

        # No tool calls — final response
        if not tool_calls:
            if content and content.strip():
                llm_trace["assistant_notes"].append(content.strip()[:320])
            return (content or ""), accumulated_usage, llm_trace

        # Process tool calls
        messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})

        if content and content.strip():
            emit_progress(content.strip())
            llm_trace["assistant_notes"].append(content.strip()[:320])

        saw_code_tool = False
        error_count = 0

        # Parallelize only for a strict read-only whitelist; all calls wrapped with timeout.
        can_parallel = (
            len(tool_calls) > 1 and
            all(
                tc.get("function", {}).get("name") in READ_ONLY_PARALLEL_TOOLS
                for tc in tool_calls
            )
        )

        if not can_parallel:
            results = [
                _execute_with_timeout(tools, tc, drive_logs,
                                      tools.get_timeout(tc["function"]["name"]), task_id)
                for tc in tool_calls
            ]
        else:
            max_workers = min(len(tool_calls), 8)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_index = {
                    executor.submit(
                        _execute_with_timeout, tools, tc, drive_logs,
                        tools.get_timeout(tc["function"]["name"]), task_id,
                    ): idx
                    for idx, tc in enumerate(tool_calls)
                }
                results = [None] * len(tool_calls)
                for future in as_completed(future_to_index):
                    idx = future_to_index[future]
                    results[idx] = future.result()

        # Process results in original order
        for exec_result in results:
            fn_name = exec_result["fn_name"]
            is_code_tool = exec_result["is_code_tool"]
            is_error = exec_result["is_error"]

            if is_code_tool:
                saw_code_tool = True
            if is_error:
                error_count += 1

            # Truncate tool result before appending to messages
            truncated_result = _truncate_tool_result(exec_result["result"])

            # Append tool result message
            messages.append({
                "role": "tool",
                "tool_call_id": exec_result["tool_call_id"],
                "content": truncated_result
            })

            # Append to LLM trace
            llm_trace["tool_calls"].append({
                "tool": fn_name,
                "args": _safe_args(exec_result["args_for_log"]),
                "result": truncate_for_log(exec_result["result"], 700),
                "is_error": is_error,
            })

        if saw_code_tool:
            _switch_to_code_profile()
        if error_count >= 2:
            _maybe_raise_effort("high")
        if error_count >= 4:
            _maybe_raise_effort("xhigh")

        # --- Budget guard ---
        # LLM decides when to stop (Bible П0, П3). We only enforce hard budget limit.
        if budget_remaining_usd is not None:
            task_cost = accumulated_usage.get("cost", 0)
            budget_pct = task_cost / budget_remaining_usd if budget_remaining_usd > 0 else 1.0

            if budget_pct > 0.5:
                # Hard stop — protect the budget
                finish_reason = f"Задача потратила ${task_cost:.3f} (>50% от остатка ${budget_remaining_usd:.2f}). Бюджет исчерпан."
                messages.append({"role": "system", "content": f"[BUDGET LIMIT] {finish_reason} Дай финальный ответ сейчас."})
                try:
                    resp_msg, usage = llm.chat(
                        messages=messages, model=active_model, tools=None,
                        reasoning_effort=active_effort,
                    )
                    add_usage(accumulated_usage, usage)
                    return (resp_msg.get("content") or finish_reason), accumulated_usage, llm_trace
                except Exception:
                    return finish_reason, accumulated_usage, llm_trace
            elif budget_pct > 0.3 and round_idx % 10 == 0:
                # Soft nudge every 10 rounds when spending is significant
                messages.append({"role": "system", "content": f"[INFO] Задача потратила ${task_cost:.3f} из ${budget_remaining_usd:.2f}. Если можешь — завершай."})

    # Unreachable but keeps type checkers happy
    return "", accumulated_usage, llm_trace


def _safe_args(v: Any) -> Any:
    """Ensure args are JSON-serializable for trace logging."""
    try:
        return json.loads(json.dumps(v, ensure_ascii=False, default=str))
    except Exception:
        return {"_repr": repr(v)}
