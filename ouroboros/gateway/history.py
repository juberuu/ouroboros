"""History/cost endpoints extracted from server.py."""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
from typing import Any, Callable, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.contracts.chat_id_policy import is_a2a_chat_id
from ouroboros.gateway._helpers import iter_jsonl_objects
from ouroboros.outcomes import normalize_outcome_axes
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

_ACCOUNTING_SUMMARY_FIELDS = (
    "settled_usd",
    "confirmed_usd",
    "estimated_usd",
    "reserved_usd",
    "unresolved_upper_bound_usd",
    "accounted_usd",
    "unknown_unmetered",
    "cost_final",
    "attempt_counts",
)

_PROGRESS_META_FIELDS = (
    "ephemeral_decision",
    "subagent_event",
    "subagent_task_id",
    "root_task_id",
    "parent_task_id",
    "delegation_role",
    "subagent_role",
    "accepted",
    "active_subagent_count",
    "max_active_subagents",
    "queued_behind_active_cap",
    "required_capabilities",
    "write_surface",
    "status",
    "cost_usd",
    "cost_accounting_status",
    "cost_accounting_error",
    "cost_final",
    "cost_usd_with_children",
    "cost_with_children_partial",
    "reserved_usd",
    "unresolved_upper_bound_usd",
    "unknown_unmetered",
    "result",
    "result_truncated",
    "trace_summary",
    "trace_summary_truncated",
    "error",
    "artifact_status",
    "worker_saturation_warning",
    "model_lane",
    "requested_model_lane",
    "effective_model_lane",
    "model",
    "task_group_id",
)


def _compat_cost_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "cost": round(float(bucket.get("settled_usd") or 0.0), 6),
        "calls": int(bucket.get("physical_calls") or 0),
        "prompt_tokens": int(bucket.get("prompt_tokens") or 0),
        "completion_tokens": int(bucket.get("completion_tokens") or 0),
        "cached_tokens": int(bucket.get("cached_tokens") or 0),
        "cache_write_tokens": int(bucket.get("cache_write_tokens") or 0),
        "prompt_cache_ttls": dict(bucket.get("prompt_cache_ttls") or {}),
    }


def _compat_cost_groups(
    groups: Dict[str, Dict[str, Any]],
    unattributed: Dict[str, Any],
    *,
    group_key: Optional[Callable[[str], str]] = None,
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for name, raw_bucket in groups.items():
        if not (
            int(raw_bucket.get("physical_calls") or 0)
            or int(raw_bucket.get("unknown_unmetered") or 0)
            or float(raw_bucket.get("accounted_usd") or 0.0)
        ):
            continue
        key = group_key(str(name)) if group_key else str(name)
        source = _compat_cost_bucket(raw_bucket)
        if key not in result:
            result[key] = source
            continue
        target = result[key]
        for field in (
            "cost", "calls", "prompt_tokens", "completion_tokens",
            "cached_tokens", "cache_write_tokens",
        ):
            target[field] += source[field]
        for ttl, count in source["prompt_cache_ttls"].items():
            target["prompt_cache_ttls"][ttl] = int(target["prompt_cache_ttls"].get(ttl, 0)) + int(count)
    if (
        int(unattributed.get("physical_calls") or 0)
        or int(unattributed.get("unknown_unmetered") or 0)
        or float(unattributed.get("accounted_usd") or 0.0)
    ):
        result["unattributed"] = _compat_cost_bucket(unattributed)
    for bucket in result.values():
        bucket["cost"] = round(float(bucket["cost"]), 6)
    return dict(sorted(result.items(), key=lambda item: item[1]["cost"], reverse=True))


async def _project_history_context(
    data_dir: pathlib.Path,
    thread_id: int,
) -> tuple[set[int], list[dict], Dict[str, Any]]:
    """Load the three read-only Project history lenses off the event loop."""
    try:
        from ouroboros.projects_registry import reserved_project_chat_ids

        project_chat_ids = reserved_project_chat_ids(data_dir)
    except Exception:
        project_chat_ids = set()
    source_refs: list[dict] = []
    if thread_id in project_chat_ids:
        try:
            from ouroboros.project_dialogue import source_refs_for_project

            source_refs = await asyncio.to_thread(source_refs_for_project, data_dir, thread_id)
        except Exception:
            log.debug("Failed to load canonical Project source refs", exc_info=True)
    try:
        from ouroboros.project_dialogue import latest_chat_annotations

        annotations = await asyncio.to_thread(latest_chat_annotations, data_dir)
    except Exception:
        annotations = {}
    return project_chat_ids, source_refs, annotations


def _matches_project_source(entry: Dict[str, Any], source_refs: list[dict]) -> bool:
    if not source_refs:
        return False
    try:
        from ouroboros.project_dialogue import entry_matches_source_ref

        return entry_matches_source_ref(entry, source_refs)
    except Exception:
        log.debug("Project source-ref classification failed", exc_info=True)
        return False


def _user_annotation(
    role: str,
    client_message_id: str,
    annotations: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    annotation = annotations.get(client_message_id)
    if role != "user" or not isinstance(annotation, dict):
        return None
    return {key: annotation.get(key) for key in ("action", "target", "status")}


def make_cost_breakdown_endpoint(data_dir: pathlib.Path):
    async def api_cost_breakdown(_request: Request) -> JSONResponse:
        """Return ledger-derived cost and physical-attempt breakdowns."""
        try:
            from ouroboros.pricing import infer_model_category
            from ouroboros.usage_accounting import ensure_legacy_imported, usage_breakdown

            ensure_legacy_imported(data_dir)
            breakdown = usage_breakdown(data_dir)
            unattributed = dict(breakdown.get("unattributed") or {})
            by_model_raw = dict(breakdown.get("by_model") or {})
            try:
                from supervisor.state import TOTAL_BUDGET_LIMIT

                limit = float(TOTAL_BUDGET_LIMIT or 0.0)
            except (ImportError, TypeError, ValueError):
                limit = 0.0
            if limit <= 0 and "TOTAL_BUDGET" in os.environ:
                try:
                    limit = max(0.0, float(os.environ.get("TOTAL_BUDGET") or 0.0))
                except (TypeError, ValueError):
                    limit = 0.0
            accounting = {field: breakdown.get(field) for field in _ACCOUNTING_SUMMARY_FIELDS}
            accounting.update({
                "available": True,
                "authority": "physical_attempt_ledger",
                "limit_usd": round(limit, 6),
                "remaining_known_usd": (
                    round(max(0.0, limit - float(breakdown.get("accounted_usd") or 0.0)), 6)
                    if limit > 0
                    else None
                ),
            })
            return JSONResponse({
                # Compatibility fields now project the physical-attempt ledger;
                # events.jsonl is import evidence, never a second cost authority.
                "total_cost": round(float(breakdown.get("settled_usd") or 0.0), 6),
                "total_calls": int(breakdown.get("physical_calls") or 0),
                "total_prompt_tokens": int(breakdown.get("prompt_tokens") or 0),
                "total_completion_tokens": int(breakdown.get("completion_tokens") or 0),
                "total_cached_tokens": int(breakdown.get("cached_tokens") or 0),
                "total_cache_write_tokens": int(breakdown.get("cache_write_tokens") or 0),
                "prompt_cache_ttls": dict(breakdown.get("prompt_cache_ttls") or {}),
                "by_model": _compat_cost_groups(by_model_raw, dict(unattributed.get("model") or {})),
                "by_api_key": _compat_cost_groups(
                    dict(breakdown.get("by_provider") or {}),
                    dict(unattributed.get("provider") or {}),
                ),
                "by_model_category": _compat_cost_groups(
                    by_model_raw,
                    dict(unattributed.get("model") or {}),
                    group_key=infer_model_category,
                ),
                "by_task_category": _compat_cost_groups(
                    dict(breakdown.get("by_category") or {}),
                    dict(unattributed.get("category") or {}),
                ),
                "accounting": accounting,
                "unattributed": unattributed,
            })
        except Exception:
            log.exception("Physical-attempt accounting unavailable")
            return JSONResponse({
                "error": "Physical-attempt accounting unavailable",
                "accounting": {
                    "available": False,
                    "authority": "physical_attempt_ledger",
                    "cost_final": False,
                    "error_code": "ledger_unavailable",
                },
            }, status_code=503)

    return api_cost_breakdown


def _read_chat_history_entries(live, adir, want, row_matches_thread):
    """Read the live chat.jsonl plus a bounded, newest-first archive backfill.

    The live chat.jsonl is rotated to ``archive/chat_<ts>.jsonl`` once it crosses
    ~800KB. Reading only the live file would erase the visible conversation right
    after a rotation (and any file bubble delivered before it). Backfill from the
    most recent archives — newest first, until we have enough human rows to satisfy
    ``want``, bounded to a few files — then reassemble chronologically (oldest
    archive -> live). ``row_matches_thread`` is the endpoint's A2A + chat_id/
    project-thread filter, threaded in so a human row counts toward the backfill
    quota only if it would survive the same filter applied in the render loop —
    otherwise a project-thread request whose live file already holds ``want``
    unrelated main-chat rows would skip the archives and still lose the rotated
    project messages/documents this backfill exists to recover.
    """
    live_entries = list(iter_jsonl_objects(live))

    def _counts_toward_thread(e):
        if not isinstance(e, dict):
            return False
        if str(e.get("direction", "")).lower() not in ("in", "out"):
            return False
        if is_a2a_chat_id(e.get("chat_id", 1)):
            return False
        try:
            ec = int(e.get("chat_id", 1) or 1)
        except (TypeError, ValueError):
            ec = 1
        return row_matches_thread(ec, e)

    def _human_count(entries):
        return sum(1 for e in entries if _counts_toward_thread(e))

    collected = _human_count(live_entries)
    try:
        archives = sorted(
            adir.glob("chat_*.jsonl"), key=lambda p: p.name, reverse=True
        )
    except Exception:
        archives = []
    chosen: list = []
    for ap in archives:
        if collected >= want or len(chosen) >= 3:
            break
        try:
            aents = list(iter_jsonl_objects(ap))
        except Exception:
            continue
        chosen.append(aents)
        collected += _human_count(aents)
    ordered: list = []
    for aents in reversed(chosen):  # oldest archive first
        ordered.extend(aents)
    ordered.extend(live_entries)
    return ordered


def make_chat_history_endpoint(data_dir: pathlib.Path):
    async def api_chat_history(request: Request) -> JSONResponse:
        """Return recent chat, system, and progress messages merged chronologically."""
        def _int_param(name: str, default: int, cap: int) -> int:
            try:
                return max(0, min(int(request.query_params.get(name, default)), cap))
            except (ValueError, TypeError):
                return default

        # Separate per-type quotas so a burst of progress/telemetry can never evict
        # the user's real conversation from a single combined tail. (`limit` is still
        # accepted for backward-compat but no longer governs the slice.)
        n_human = _int_param("n_human", 750, 1500)
        n_progress = _int_param("n_progress", 300, 600)
        # Multi-project thread filter (v6.32.0): each chat fetches its own
        # history. Default 1 = main chat (legacy rows without chat_id are main).
        # The filter only PARTITIONS when the requested thread is a registered
        # project chat; for the main chat (and any non-project chat_id, e.g. an
        # external-transport mirror) it keeps the historic behavior of showing
        # every non-project, non-A2A row so transport conversations stay visible.
        thread_id = _int_param("chat_id", 1, 2**31 - 1) or 1
        project_chat_ids, project_source_refs, chat_annotations = await _project_history_context(
            data_dir, thread_id,
        )
        bound_chat_cache: Dict[tuple, int] = {}

        def _bound_project_chat(task_id: str, parent_task_id: str = "", root_task_id: str = "") -> int:
            # Resolve by LINEAGE (own binding -> parent -> root) so a subagent's rows
            # classify into its root's project thread (only the root is bound).
            tid = str(task_id or "").strip()
            if not tid:
                return 0
            key = (tid, str(parent_task_id or ""), str(root_task_id or ""))
            if key in bound_chat_cache:
                return bound_chat_cache[key]
            try:
                from ouroboros.projects_registry import project_chat_for_task_tree

                bound_chat_cache[key] = int(project_chat_for_task_tree(data_dir, tid, parent_task_id, root_task_id) or 0)
            except Exception:
                bound_chat_cache[key] = 0
            return bound_chat_cache[key]

        def _row_matches_thread(entry_chat: int, entry: Optional[dict] = None) -> bool:
            # A post-hoc bound task keeps its original (main) chat_id on its rows
            # but belongs to a project — classify by the durable LINEAGE binding too.
            bound_chat = (
                _bound_project_chat(
                    str(entry.get("task_id") or ""),
                    str(entry.get("parent_task_id") or ""),
                    str(entry.get("root_task_id") or ""),
                ) if isinstance(entry, dict) else 0
            )
            if thread_id in project_chat_ids:
                if bound_chat == thread_id:
                    return True
                if isinstance(entry, dict) and _matches_project_source(entry, project_source_refs):
                    return True
                return entry_chat == thread_id
            # Main / non-project view: everything that is NOT another project. A
            # bound task's rows are project-owned, so mirror only its sanitized
            # progress/task_summary and exclude its raw chat (same as a native
            # project row), never leak raw project chat into the штаб.
            if entry_chat in project_chat_ids or bound_chat > 0:
                if not isinstance(entry, dict):
                    return False
                return bool(entry.get("is_progress")) or str(entry.get("type") or "") == "task_summary"
            return entry_chat not in project_chat_ids

        combined: list = []

        chat_path = data_dir / "logs" / "chat.jsonl"
        archive_dir = data_dir / "archive"
        try:
            # WS4: parse the jsonl off the event loop so a large history can't block
            # the loop. Rotation-aware archive backfill lives in the module-level
            # _read_chat_history_entries helper (endpoint's thread filter threaded in).
            _chat_entries = await asyncio.to_thread(
                _read_chat_history_entries, chat_path, archive_dir, n_human, _row_matches_thread
            )
            for entry in _chat_entries:
                # Skip A2A virtual chat_ids so A2A task traffic does not appear in human chat history.
                if is_a2a_chat_id(entry.get("chat_id", 1)):
                    continue
                try:
                    entry_chat = int(entry.get("chat_id", 1) or 1)
                except (TypeError, ValueError):
                    entry_chat = 1
                if not _row_matches_thread(entry_chat, entry):
                    continue
                direction = str(entry.get("direction", "")).lower()
                role = {"in": "user", "out": "assistant", "system": "system"}.get(direction)
                if role is None:
                    continue
                rec = {
                    "text": str(entry.get("text", "")),
                    "role": role,
                    "ts": str(entry.get("ts", "")),
                    "is_progress": False,
                    "system_type": str(entry.get("type", "")),
                    "markdown": str(entry.get("format", "")).lower() == "markdown",
                    "source": str(entry.get("source", "")),
                    "sender_label": str(entry.get("sender_label", "")),
                    "sender_session_id": str(entry.get("sender_session_id", "")),
                    "client_message_id": str(entry.get("client_message_id", "")),
                    "task_id": str(entry.get("task_id", "")),
                    "telegram_chat_id": int(entry.get("telegram_chat_id") or 0),
                }
                annotation = _user_annotation(role, rec["client_message_id"], chat_annotations)
                if annotation is not None:
                    rec["chat_annotation"] = annotation
                # Delivered document rows carry lightweight media metadata (no
                # base64); surface a msg_type + download_url so the frontend
                # rebuilds the file bubble on reload instead of a bare text line.
                if entry.get("type") == "document":
                    rec["msg_type"] = "document"
                    rec["filename"] = str(entry.get("filename") or "file")
                    rec["mime"] = str(entry.get("mime") or "application/octet-stream")
                    rec["download_url"] = str(entry.get("download_url") or "")
                    rec["caption"] = str(entry.get("caption") or "")
                # Pass task metadata for task_summary entries so the frontend can decide whether to show a live card.
                if entry.get("type") == "task_summary":
                    if "tool_calls" in entry:
                        rec["tool_calls"] = int(entry["tool_calls"])
                    if "rounds" in entry:
                        rec["rounds"] = int(entry["rounds"])
                    rec["outcome_axes"] = normalize_outcome_axes(entry)
                    if "reason_code" in entry:
                        rec["reason_code"] = str(entry.get("reason_code") or "")
                combined.append(rec)
        except Exception as exc:
            log.warning("Failed to read chat history: %s", exc)

        progress_path = data_dir / "logs" / "progress.jsonl"
        try:
            _progress_entries = await asyncio.to_thread(lambda p=progress_path: list(iter_jsonl_objects(p)))
            for entry in _progress_entries:
                # Skip A2A virtual chat_ids.
                if is_a2a_chat_id(entry.get("chat_id", 1)):
                    continue
                try:
                    entry_chat = int(entry.get("chat_id", 1) or 1)
                except (TypeError, ValueError):
                    entry_chat = 1
                if not _row_matches_thread(entry_chat, {"is_progress": True, **entry}):
                    continue
                text = str(entry.get("content", entry.get("text", "")))
                if not text:
                    continue
                rec = {
                    "text": text,
                    "role": "assistant",
                    "ts": str(entry.get("ts", "")),
                    "is_progress": True,
                    "markdown": str(entry.get("format", "")).lower() == "markdown",
                    "task_id": str(entry.get("task_id", "")),
                }
                if isinstance(entry.get("lifecycle"), dict):
                    rec["lifecycle"] = dict(entry.get("lifecycle") or {})
                for field in _PROGRESS_META_FIELDS:
                    if field in entry:
                        rec[field] = entry[field]
                combined.append(rec)
        except Exception as exc:
            log.warning("Failed to read progress log: %s", exc)

        try:
            from ouroboros.skill_lifecycle_queue import queue_snapshot

            active = queue_snapshot().get("active")
            if isinstance(active, dict) and active.get("status") == "running":
                label = "stale" if active.get("stale") else "running"
                detail = active.get("error") or active.get("message") or active.get("status") or ""
                text = (
                    f"Skill {active.get('kind') or 'operation'}: `{active.get('target') or 'skill'}`"
                    f" — {label}{f' — {detail}' if detail else ''}"
                )
                lifecycle = dict(active)
                lifecycle["phase"] = label
                combined.append({
                    "text": text,
                    "role": "assistant",
                    "ts": utc_now_iso(),
                    "is_progress": True,
                    "markdown": False,
                    "task_id": str(active.get("chat_task_id") or ""),
                    "lifecycle": lifecycle,
                    "lifecycle_virtual": True,
                })
        except Exception as exc:
            log.debug("Failed to synthesize active lifecycle history: %s", exc)

        # Annotate progress messages whose task already reached a terminal (or
        # cancel-intent) status on disk. Tasks torn down by crash storm, hard
        # timeout, or cancellation emit a live task_done but never write a
        # task_summary, so on reload/reconnect the client would otherwise replay
        # their progress and re-inflate a "Working" spinner that never resolves.
        try:
            from ouroboros.task_status import FINAL_STATUSES, load_effective_task_result

            progress_task_ids = {
                str(m.get("task_id") or "")
                for m in combined
                if m.get("is_progress") and m.get("task_id")
            }
            # Cluster B: a card can also be (re)built from a task_summary row (a finished
            # task with no retained progress row), so include those task ids — else their
            # suggested_name would be lost on reload despite the persisted-title contract.
            summary_task_ids = {
                str(m.get("task_id") or "")
                for m in combined
                if str(m.get("system_type") or "") == "task_summary" and m.get("task_id")
            }
            card_task_ids = progress_task_ids | summary_task_ids
            terminal_status_by_task: Dict[str, str] = {}
            suggested_name_by_task: Dict[str, str] = {}
            for tid in card_task_ids:
                try:
                    # Effective (not raw) status: applies the stale-orphan guard so a
                    # task whose worker was SIGKILLed (/panic, crash) and never wrote a
                    # terminal result is treated as failed → its card finalizes instead
                    # of replaying "Working" forever.
                    res = load_effective_task_result(data_dir, tid)
                except Exception:
                    res = None
                status = str((res or {}).get("status") or "")
                if status in FINAL_STATUSES:
                    terminal_status_by_task[tid] = status
                # The proactively-coined project name (rendered as the card title), reusing
                # the result we already loaded — no extra file read.
                nm = str((res or {}).get("suggested_name") or "").strip()
                if nm:
                    suggested_name_by_task[tid] = nm
            if terminal_status_by_task or suggested_name_by_task:
                for m in combined:
                    tid = str(m.get("task_id") or "")
                    if not tid:
                        continue
                    if m.get("is_progress"):
                        status = terminal_status_by_task.get(tid)
                        if status:
                            m["task_terminal_status"] = status
                    nm = suggested_name_by_task.get(tid)
                    # Attach to progress AND task_summary rows (both can build a card).
                    if nm and (m.get("is_progress") or str(m.get("system_type") or "") == "task_summary"):
                        m["suggested_name"] = nm
        except Exception as exc:
            log.debug("Failed to annotate terminal task status in history: %s", exc)

        # Background consciousness writes no task_result, so its progress would
        # otherwise replay as a perpetual "thinking" card after reload. Mark its
        # most recent progress entry terminal; a fresh live event re-activates the
        # card if a new cycle starts. (Structured signal, consumed by log_events.js.)
        try:
            bg_msgs = [
                m for m in combined
                if m.get("is_progress") and str(m.get("task_id") or "") == "bg-consciousness"
            ]
            if bg_msgs:
                latest = max(bg_msgs, key=lambda m: str(m.get("ts") or ""))
                latest["task_terminal_status"] = "done"
        except Exception as exc:
            log.debug("Failed to annotate bg-consciousness terminal status: %s", exc)

        # Tail human conversation and progress telemetry with SEPARATE quotas so a
        # burst of progress messages can never push the user's real conversation out
        # (the previous single combined[-limit:] tail). Subagent lineage is kept on
        # top of the progress quota so a flood can't evict a RECENT child's lifecycle
        # events (the client rebuilds child-card lineage from them) — but only WITHIN
        # the recent telemetry window: resurrecting an old finished swarm's child
        # events would recreate an orphaned "Working" parent card whose own terminal
        # row has already aged out of the window.
        def _is_subagent_lineage(m: dict) -> bool:
            # Only true SUBAGENT lifecycle (delegation_role 'subagent' or any
            # subagent_event) is lineage-critical. delegation_role can also be
            # 'root', which must NOT bypass the progress quota.
            return str(m.get("delegation_role") or "").lower() == "subagent" or bool(m.get("subagent_event"))

        # NOTE: guard 0 explicitly — Python's list[-0:] is list[0:] (the WHOLE list),
        # so a `[-quota:]` slice with quota==0 would leak everything, not nothing.
        lineage_cap = 1000  # bound lineage so a huge swarm fan-out can't balloon the response
        human = sorted((m for m in combined if not m.get("is_progress")), key=lambda m: m.get("ts", ""))
        progress = sorted((m for m in combined if m.get("is_progress")), key=lambda m: m.get("ts", ""))
        human_tail = human[-n_human:] if n_human > 0 else []
        other = [m for m in progress if not _is_subagent_lineage(m)]
        other_tail = other[-n_progress:] if n_progress > 0 else []
        # Recency floor = oldest retained telemetry row. Drop lineage older than it so
        # long-finished swarms don't re-materialise as stuck "Working" parent cards.
        floor = str(other_tail[0].get("ts") or "") if other_tail else ""
        lineage = [
            m for m in progress
            if _is_subagent_lineage(m) and (not floor or str(m.get("ts") or "") >= floor)
        ]
        if len(lineage) > lineage_cap:
            lineage = lineage[-lineage_cap:]  # keep the most recent lineage events
        progress_tail = lineage + other_tail
        messages = sorted(human_tail + progress_tail, key=lambda m: m.get("ts", ""))
        return JSONResponse({"messages": messages})

    return api_chat_history
