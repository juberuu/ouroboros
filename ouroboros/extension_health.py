"""Durable per-extension health vector for live->broken regression detection.

A small immune-system instrument (BIBLE P1 "discrepancy between expected and actual
state — immediate alert"; P3 health invariants): if an extension that was live at a
prior code version stops loading after a self-modification + restart, record the
regression and surface it to the owner/agent. This is warning-only — it never
disables a skill and never mutates git. The record lives next to the other per-skill
owner-state files at ``data/state/skills/<name>/health.json``.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros.contracts.schema_versions import with_schema_version
from ouroboros.skill_loader import skill_state_dir
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

HEALTH_FILENAME = "health.json"
_SCHEMA = 1

# Health statuses derived from the extension runtime state.
LIVE = "live"        # desired_live and loaded successfully
BROKEN = "broken"    # desired_live but failed to load
INACTIVE = "inactive"  # disabled, deps pending, review-gated, or not an extension


def health_path(drive_root: pathlib.Path, skill_name: str) -> pathlib.Path:
    return skill_state_dir(pathlib.Path(drive_root), skill_name) / HEALTH_FILENAME


def read_extension_health(drive_root: pathlib.Path, skill_name: str) -> Optional[Dict[str, Any]]:
    return read_json_dict(health_path(drive_root, skill_name))


def record_extension_health(
    drive_root: pathlib.Path,
    skill_name: str,
    *,
    status: str,
    version: str = "",
    sha: str = "",
    reason: str = "",
    load_error: str = "",
) -> Dict[str, Any]:
    """Persist the current health observation and flag a live->broken regression.

    ``regressed`` (persisted) stays true while a once-live extension is broken, so
    the UI and health invariants keep surfacing it until it loads again.
    ``newly_regressed`` (returned, not persisted) marks the live->broken transition
    so callers can log/alert once per transition rather than every restart.
    """
    prior = read_extension_health(drive_root, skill_name) or {}
    prior_status = str((prior.get("last_observed") or {}).get("status") or "")
    last_known_good = prior.get("last_known_good")
    if not isinstance(last_known_good, dict):
        last_known_good = None

    now = utc_now_iso()
    observed = {
        "version": version,
        "sha": sha,
        "status": status,
        "reason": reason,
        "load_error": (load_error or "")[:2000],
        "ts": now,
    }
    regressed = False
    newly_regressed = False
    if status == LIVE:
        last_known_good = {"version": version, "sha": sha, "ts": now}
    elif status == BROKEN and last_known_good is not None:
        # Only a break at a DIFFERENT code version/commit is a regression. A same-sha
        # break is environmental (revoked grant, transient catalog/spawn failure), not a
        # code regression, so it must not raise the "broken after a code update" alarm.
        if str(last_known_good.get("sha") or "") != str(sha or ""):
            regressed = True
            newly_regressed = prior_status == LIVE

    record = with_schema_version(
        {
            "skill": skill_name,
            "status": status,
            "regressed": regressed,
            "last_known_good": last_known_good,
            "last_observed": observed,
        },
        _SCHEMA,
    )
    try:
        atomic_write_json(health_path(drive_root, skill_name), record)
    except Exception:
        log.debug("Failed to persist extension health for %s", skill_name, exc_info=True)
    result = dict(record)
    result["newly_regressed"] = newly_regressed
    return result


def regressed_extensions(drive_root: pathlib.Path) -> List[Dict[str, Any]]:
    """Return health records for extensions currently flagged as regressed."""
    root = pathlib.Path(drive_root) / "state" / "skills"
    out: List[Dict[str, Any]] = []
    if not root.is_dir():
        return out
    for skill_dir in sorted(root.iterdir()):
        if not skill_dir.is_dir():
            continue
        record = read_json_dict(skill_dir / HEALTH_FILENAME)
        if not (record and record.get("regressed")):
            continue
        # A regression alarm only matters while the skill still exists and is still
        # enabled. An uninstalled or owner-disabled skill must not raise a permanent
        # false CRITICAL (its health.json can outlive the payload).
        name = str(record.get("skill") or skill_dir.name)
        try:
            from ouroboros.skill_loader import find_skill, load_enabled

            if find_skill(pathlib.Path(drive_root), name) is None or not load_enabled(pathlib.Path(drive_root), name):
                continue
            # If it is currently live it has recovered — not a regression, even if a
            # prior reload_all left health.json.regressed=true. Health is recorded in
            # reload_all, so a repair/reconcile outside reload_all (UI/tool/review) would
            # otherwise keep alarming until the next restart; the live check clears it.
            from ouroboros.extension_loader import runtime_state_for_skill_name

            if runtime_state_for_skill_name(name, pathlib.Path(drive_root)).get("live_loaded"):
                continue
        except Exception:
            pass
        out.append(record)
    return out


def status_for_runtime_state(state: Dict[str, Any]) -> str:
    """Map an extension runtime-state dict to a health status."""
    if state.get("live_loaded"):
        return LIVE
    if state.get("desired_live") and (
        state.get("action") == "extension_load_error" or state.get("reason") == "load_error"
    ):
        return BROKEN
    return INACTIVE


__all__ = [
    "HEALTH_FILENAME",
    "LIVE",
    "BROKEN",
    "INACTIVE",
    "health_path",
    "read_extension_health",
    "record_extension_health",
    "regressed_extensions",
    "status_for_runtime_state",
]
