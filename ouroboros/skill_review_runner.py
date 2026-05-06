"""Shared lifecycle-backed runner for expensive skill reviews."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict

from ouroboros.config import get_skills_repo_path, load_settings
from ouroboros.skill_lifecycle_queue import (
    DuplicateLifecycleJobError,
    JobProgressTarget,
    LifecycleJob,
    LifecycleJobOptions,
    run_lifecycle_job,
    run_lifecycle_job_blocking,
)
from ouroboros.skill_loader import SkillPayloadUnreadable, compute_content_hash, find_skill, skill_state_dir
from ouroboros.skill_review import SkillReviewOutcome, review_skill as _default_review_skill
from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_SEC = 30.0
_STALE_REVIEW_JOB_SEC = int(os.environ.get("OUROBOROS_SKILL_REVIEW_JOB_STALE_SEC", "7200"))


ReviewImpl = Callable[[Any, str], SkillReviewOutcome]


def review_job_state_path(drive_root: pathlib.Path, skill_name: str) -> pathlib.Path:
    return skill_state_dir(pathlib.Path(drive_root), skill_name) / "review_job.json"


def _events_path(drive_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(drive_root) / "logs" / "events.jsonl"


def _write_json_atomic(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_review_job(path: pathlib.Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    from ouroboros.platform_layer import pid_is_alive

    return pid_is_alive(pid)


def _iso_age_sec(value: str) -> float:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return 0.0


def mark_stale_review_job_interrupted(
    drive_root: pathlib.Path,
    skill_name: str,
    *,
    current_content_hash: str = "",
    stale_after_sec: int = _STALE_REVIEW_JOB_SEC,
) -> None:
    path = review_job_state_path(drive_root, skill_name)
    data = _read_review_job(path)
    if str(data.get("status") or "") != "running":
        return
    pid = int(data.get("pid") or 0)
    heartbeat_age = _iso_age_sec(str(data.get("last_heartbeat_at") or data.get("started_at") or ""))
    pid_dead = bool(pid and not _pid_alive(pid))
    heartbeat_stale = bool(heartbeat_age and heartbeat_age > stale_after_sec)
    if not (pid_dead or heartbeat_stale):
        return
    now = utc_now_iso()
    payload = {
        **data,
        "status": "interrupted",
        "finished_at": now,
        "interrupted_at": now,
        "interrupt_reason": "owner_process_exited" if pid_dead else "heartbeat_stale",
        "content_hash": data.get("content_hash") or current_content_hash,
    }
    _write_json_atomic(path, payload)
    append_jsonl(
        _events_path(drive_root),
        {
            "ts": now,
            "type": "skill_review_interrupted",
            "skill": skill_name,
            "content_hash": payload.get("content_hash", ""),
            "job_id": payload.get("job_id", ""),
            "reason": payload.get("interrupt_reason", ""),
        },
    )


def reconcile_stale_review_jobs(
    drive_root: pathlib.Path,
    *,
    stale_after_sec: int = _STALE_REVIEW_JOB_SEC,
) -> int:
    """Mark stale running review jobs interrupted across the skill state plane."""

    root = pathlib.Path(drive_root) / "state" / "skills"
    if not root.exists():
        return 0
    count = 0
    for path in root.glob("*/review_job.json"):
        before = _read_review_job(path)
        if str(before.get("status") or "") != "running":
            continue
        skill_name = path.parent.name
        mark_stale_review_job_interrupted(
            pathlib.Path(drive_root),
            skill_name,
            current_content_hash=str(before.get("content_hash") or ""),
            stale_after_sec=stale_after_sec,
        )
        after = _read_review_job(path)
        if str(after.get("status") or "") == "interrupted":
            count += 1
    return count


def _patch_review_job(drive_root: pathlib.Path, skill_name: str, **updates: Any) -> None:
    path = review_job_state_path(drive_root, skill_name)
    data = _read_review_job(path)
    data.update(updates)
    _write_json_atomic(path, data)


@contextlib.contextmanager
def _review_job_heartbeat(drive_root: pathlib.Path, skill_name: str):
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(_HEARTBEAT_INTERVAL_SEC):
            try:
                _patch_review_job(drive_root, skill_name, last_heartbeat_at=utc_now_iso())
            except Exception:
                log.debug("skill review heartbeat update failed", exc_info=True)

    thread = threading.Thread(target=_beat, name=f"skill-review-heartbeat-{skill_name}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)


def _skill_content_hash(drive_root: pathlib.Path, skill_name: str, repo_path: str | None) -> str:
    skill = find_skill(drive_root, skill_name, repo_path=repo_path)
    if skill is None or skill.load_error:
        return ""
    try:
        return compute_content_hash(
            skill.skill_dir,
            manifest_entry=skill.manifest.entry,
            manifest_scripts=skill.manifest.scripts,
        )
    except SkillPayloadUnreadable:
        return ""


def _review_dedupe_key(skill_name: str, content_hash: str) -> str:
    suffix = content_hash or "unknown"
    return f"review:{skill_name}:{suffix}"


async def _to_thread_preserving_result(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking work in a thread and preserve its result across cancellation.

    HTTP clients can disconnect while an expensive review is in flight. The
    underlying reviewer thread cannot be killed, so the lifecycle lane must
    stay occupied until it finishes instead of reporting a terminal job early.
    """

    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            current = asyncio.current_task()
            if current is not None and hasattr(current, "uncancel"):
                while current.cancelling():  # type: ignore[attr-defined]
                    current.uncancel()  # type: ignore[attr-defined]
            await asyncio.sleep(0.01)
            continue


def _reconcile_deps_after_pass_review(
    drive_root: pathlib.Path,
    skill_name: str,
    *,
    repo_path: str | None = None,
) -> tuple[str, str]:
    """Install/reinstall isolated deps after a PASS review."""

    try:
        from ouroboros.marketplace.install_specs import install_specs_hash
        from ouroboros.marketplace.isolated_deps import (
            install_isolated_dependencies,
            read_deps_state,
        )

        loaded = find_skill(drive_root, skill_name, repo_path=repo_path)
        if loaded is None:
            return "failed", "skill not found during dependency reconciliation"
        from ouroboros.skill_dependencies import auto_install_specs_for_skill

        auto_specs = auto_install_specs_for_skill(drive_root, loaded)
        if not auto_specs:
            return "not_required", ""
        deps_state = read_deps_state(drive_root, skill_name)
        expected_hash = install_specs_hash(auto_specs)
        if (
            str(deps_state.get("status") or "") == "installed"
            and deps_state.get("specs_hash") == expected_hash
        ):
            return "installed", ""
        install_isolated_dependencies(drive_root, skill_name, loaded.skill_dir, auto_specs)
        return "installed", ""
    except Exception as exc:
        log.debug("post-review deps reconcile failed", exc_info=True)
        return "failed", f"{type(exc).__name__}: {exc}"


def _heal_mode(ctx: Any) -> bool:
    return any(
        isinstance(message.get("content"), str) and "HEAL_MODE_NO_ENABLE" in message.get("content", "")
        for message in (getattr(ctx, "messages", None) or [])
    )


def _outcome_payload(
    outcome: SkillReviewOutcome,
    *,
    deps_status: str,
    deps_error: str,
    extension_action: Any,
    extension_reason: Any,
    job: LifecycleJob | None = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "skill": outcome.skill_name,
        "status": outcome.status,
        "content_hash": outcome.content_hash,
        "reviewer_models": outcome.reviewer_models,
        "findings": outcome.findings,
        "error": outcome.error,
        "deps_status": deps_status,
        "deps_error": deps_error,
        "extension_action": extension_action,
        "extension_reason": extension_reason,
    }
    if job is not None:
        payload["job_id"] = job.id
        payload["job_status"] = job.status
    return payload


def _reconcile_extension_payload(
    ctx: Any,
    skill_name: str,
    *,
    repo_path: str | None,
    heal_mode: bool,
) -> tuple[Any, Any]:
    if heal_mode:
        try:
            from ouroboros import extension_loader

            if skill_name in extension_loader.snapshot()["extensions"]:
                extension_loader.unload_extension(skill_name)
                return "extension_unloaded", "heal_review_only"
            return "extension_heal_review_only", "heal_review_only"
        except Exception:
            return "extension_heal_review_only", "heal_review_only"
    try:
        from ouroboros import extension_loader

        live_state = extension_loader.reconcile_extension(
            skill_name,
            pathlib.Path(ctx.drive_root),
            load_settings,
            repo_path=repo_path,
            retry_load_error=True,
        )
        return live_state.get("action"), live_state.get("reason")
    except Exception:
        return None, None


def _on_started(
    drive_root: pathlib.Path,
    skill_name: str,
    content_hash: str,
    started_monotonic: Dict[str, float],
) -> Callable[[LifecycleJob], None]:
    def _callback(job: LifecycleJob) -> None:
        now = utc_now_iso()
        started_monotonic["value"] = time.monotonic()
        payload = {
            "status": "running",
            "skill": skill_name,
            "content_hash": content_hash,
            "job_id": job.id,
            "lifecycle_status": job.status,
            "dedupe_key": job.dedupe_key,
            "started_at": job.started_at or now,
            "last_heartbeat_at": now,
            "finished_at": "",
            "duration_sec": None,
            "pid": os.getpid(),
        }
        _write_json_atomic(review_job_state_path(drive_root, skill_name), payload)
        append_jsonl(
            _events_path(drive_root),
            {
                "ts": now,
                "type": "skill_review_started",
                "skill": skill_name,
                "content_hash": content_hash,
                "job_id": job.id,
            },
        )

    return _callback


def _on_finished(
    drive_root: pathlib.Path,
    skill_name: str,
    content_hash: str,
    started_monotonic: Dict[str, float],
) -> Callable[[LifecycleJob, Any, BaseException | None], None]:
    def _callback(job: LifecycleJob, result: Any, exc: BaseException | None) -> None:
        now = utc_now_iso()
        duration = None
        if "value" in started_monotonic:
            duration = round(max(0.0, time.monotonic() - started_monotonic["value"]), 3)
        review_status = getattr(result, "status", "") if result is not None else ""
        error = str(exc) if exc is not None else (getattr(result, "error", "") if result is not None else "")
        deps_error = getattr(result, "deps_error", "") if result is not None else ""
        state_status = "failed" if job.status in {"failed", "cancelled"} else "completed"
        payload = {
            "status": state_status,
            "skill": skill_name,
            "content_hash": getattr(result, "content_hash", "") or content_hash,
            "job_id": job.id,
            "lifecycle_status": job.status,
            "dedupe_key": job.dedupe_key,
            "started_at": job.started_at,
            "last_heartbeat_at": now,
            "finished_at": job.finished_at or now,
            "duration_sec": duration,
            "pid": os.getpid(),
            "review_status": review_status,
            "error": error,
            "deps_error": deps_error,
        }
        _write_json_atomic(review_job_state_path(drive_root, skill_name), payload)
        append_jsonl(
            _events_path(drive_root),
            {
                "ts": now,
                "type": "skill_review_completed" if state_status == "completed" else "skill_review_failed",
                "skill": skill_name,
                "content_hash": payload.get("content_hash", ""),
                "job_id": job.id,
                "duration_sec": duration,
                "status": review_status or state_status,
                "error": error or deps_error,
            },
        )

    return _callback


def _duplicate_payload(skill_name: str, content_hash: str, duplicate: LifecycleJob) -> Dict[str, Any]:
    return {
        "skill": skill_name,
        "status": "pending",
        "content_hash": content_hash,
        "reviewer_models": [],
        "findings": [],
        "error": f"review already {duplicate.status} for this skill/content hash",
        "deps_status": "not_required",
        "deps_error": "",
        "extension_action": None,
        "extension_reason": None,
        "job_id": duplicate.id,
        "job_status": duplicate.status,
    }


def _review_finding_summary(outcome: Any) -> str:
    def _is_pass(item: Dict[str, Any]) -> bool:
        signal = str(item.get("verdict") or item.get("status") or "").strip().lower()
        return signal in {"pass", "passed", "ok"}

    def _chat_headline(text: str, max_chars: int = 180) -> str:
        text = str(text or "").strip()
        if len(text) <= max_chars:
            return text
        marker = "... [omitted {count} chars; full findings in Skills page]"
        budget = max(1, max_chars - len(marker.format(count=0)))
        omitted = max(0, len(text) - budget)
        return text[:budget].rstrip() + marker.format(count=omitted)

    findings = [item for item in (getattr(outcome, "findings", None) or []) if isinstance(item, dict)]
    for item in sorted(findings, key=lambda item: 1 if _is_pass(item) else 0):
        label = str(item.get("item") or item.get("check") or item.get("title") or "finding").strip()
        verdict = str(item.get("verdict") or item.get("severity") or "").strip()
        reason = str(item.get("reason") or item.get("message") or "").strip()
        pieces = [piece for piece in (verdict, label, reason) if piece]
        if pieces:
            summary = ": ".join((" ".join(pieces[:2]), pieces[2])) if len(pieces) > 2 else " ".join(pieces)
            return _chat_headline(summary)
    return ""


def _review_result_message(outcome: Any) -> str:
    status = str(getattr(outcome, "status", "") or "pending")
    summary = _review_finding_summary(outcome)
    return f"Review {status}{f': {summary}' if summary else ''}"


async def run_skill_review_lifecycle(
    ctx: Any,
    skill_name: str,
    *,
    source: str = "skills",
    review_impl: ReviewImpl = _default_review_skill,
    repo_path: str | None = None,
) -> Dict[str, Any]:
    drive_root = pathlib.Path(ctx.drive_root)
    repo_path = repo_path if repo_path is not None else get_skills_repo_path()
    content_hash = _skill_content_hash(drive_root, skill_name, repo_path)
    mark_stale_review_job_interrupted(drive_root, skill_name, current_content_hash=content_hash)
    dedupe_key = _review_dedupe_key(skill_name, content_hash)
    started_monotonic: Dict[str, float] = {}
    progress = JobProgressTarget()

    async def _run_review() -> SkillReviewOutcome:
        with _review_job_heartbeat(drive_root, skill_name):
            progress.set("Running tri-model review…")
            outcome = await _to_thread_preserving_result(review_impl, ctx, skill_name)
        deps_status = "not_required"
        deps_error = ""
        if getattr(outcome, "status", "") == "pass":
            progress.set("Installing dependencies…")
            deps_status, deps_error = await _to_thread_preserving_result(
                _reconcile_deps_after_pass_review,
                drive_root,
                skill_name,
                repo_path=repo_path,
            )
        setattr(outcome, "deps_status", deps_status)
        setattr(outcome, "deps_error", deps_error)
        progress.set("Reloading extension…")
        extension_action, extension_reason = _reconcile_extension_payload(
            ctx,
            skill_name,
            repo_path=repo_path,
            heal_mode=_heal_mode(ctx),
        )
        setattr(outcome, "extension_action", extension_action)
        setattr(outcome, "extension_reason", extension_reason)
        return outcome

    try:
        outcome = await run_lifecycle_job(
            kind="review",
            target=skill_name,
            source=source,
            message=f"Reviewing {skill_name}",
            dedupe_key=dedupe_key,
            runner=_run_review,
            options=LifecycleJobOptions(
                drive_root=drive_root,
                progress_target=progress,
                result_message=_review_result_message,
                result_error=lambda item: getattr(item, "error", "") or getattr(item, "deps_error", "") or "",
                on_started=_on_started(drive_root, skill_name, content_hash, started_monotonic),
                on_finished=_on_finished(drive_root, skill_name, content_hash, started_monotonic),
            ),
        )
    except DuplicateLifecycleJobError as exc:
        return _duplicate_payload(skill_name, content_hash, exc.job)

    return _outcome_payload(
        outcome,
        deps_status=getattr(outcome, "deps_status", "not_required"),
        deps_error=getattr(outcome, "deps_error", ""),
        extension_action=getattr(outcome, "extension_action", None),
        extension_reason=getattr(outcome, "extension_reason", None),
    )


def run_skill_review_lifecycle_blocking(
    ctx: Any,
    skill_name: str,
    *,
    source: str = "tool",
    review_impl: ReviewImpl = _default_review_skill,
    repo_path: str | None = None,
) -> Dict[str, Any]:
    drive_root = pathlib.Path(ctx.drive_root)
    repo_path = repo_path if repo_path is not None else get_skills_repo_path()
    content_hash = _skill_content_hash(drive_root, skill_name, repo_path)
    mark_stale_review_job_interrupted(drive_root, skill_name, current_content_hash=content_hash)
    dedupe_key = _review_dedupe_key(skill_name, content_hash)
    started_monotonic: Dict[str, float] = {}
    progress = JobProgressTarget()

    def _run_review() -> SkillReviewOutcome:
        with _review_job_heartbeat(drive_root, skill_name):
            progress.set("Running tri-model review…")
            outcome = review_impl(ctx, skill_name)
        deps_status = "not_required"
        deps_error = ""
        if getattr(outcome, "status", "") == "pass":
            progress.set("Installing dependencies…")
            deps_status, deps_error = _reconcile_deps_after_pass_review(
                drive_root,
                skill_name,
                repo_path=repo_path,
            )
        setattr(outcome, "deps_status", deps_status)
        setattr(outcome, "deps_error", deps_error)
        progress.set("Reloading extension…")
        extension_action, extension_reason = _reconcile_extension_payload(
            ctx,
            skill_name,
            repo_path=repo_path,
            heal_mode=_heal_mode(ctx),
        )
        setattr(outcome, "extension_action", extension_action)
        setattr(outcome, "extension_reason", extension_reason)
        return outcome

    try:
        outcome = run_lifecycle_job_blocking(
            kind="review",
            target=skill_name,
            source=source,
            message=f"Reviewing {skill_name}",
            dedupe_key=dedupe_key,
            runner=_run_review,
            options=LifecycleJobOptions(
                drive_root=drive_root,
                progress_target=progress,
                result_message=_review_result_message,
                result_error=lambda item: getattr(item, "error", "") or getattr(item, "deps_error", "") or "",
                on_started=_on_started(drive_root, skill_name, content_hash, started_monotonic),
                on_finished=_on_finished(drive_root, skill_name, content_hash, started_monotonic),
            ),
        )
    except DuplicateLifecycleJobError as exc:
        return _duplicate_payload(skill_name, content_hash, exc.job)

    return _outcome_payload(
        outcome,
        deps_status=getattr(outcome, "deps_status", "not_required"),
        deps_error=getattr(outcome, "deps_error", ""),
        extension_action=getattr(outcome, "extension_action", None),
        extension_reason=getattr(outcome, "extension_reason", None),
    )

