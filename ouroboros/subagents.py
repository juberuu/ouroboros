"""Subagent lane, cap, and metadata helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from ouroboros.config import (
    SETTINGS_DEFAULTS,
    get_heavy_model,
    get_light_model,
    get_review_models,
    get_scope_review_models,
)

SUBAGENT_MODEL_LANES: frozenset[str] = frozenset({
    "auto",
    "main",
    "heavy",
    "light",
    "review",
    "scope",
})


def _capability_depth_limit() -> int:
    """Subagent depth at/below which an EXPLICIT main/heavy lane is honored; deeper
    descendants fall to light (cost guard). SSOT for the resolver, the tool-schema
    description, and the onboarding note. Owner-configurable (advanced)."""
    try:
        return max(0, int(os.environ.get("OUROBOROS_SUBAGENT_CAPABILITY_DEPTH_LIMIT", "")
                          or SETTINGS_DEFAULTS.get("OUROBOROS_SUBAGENT_CAPABILITY_DEPTH_LIMIT", 1)))
    except (TypeError, ValueError):
        return 1


@dataclass(frozen=True)
class SubagentLaneResolution:
    requested_lane: str
    effective_lane: str
    model: str
    use_local_model: bool = False
    slot_index: int = 0
    slot_count: int = 1
    downgrade_note: str = ""


def normalize_subagent_model_lane(value: Any) -> str:
    lane = str(value or "auto").strip().lower()
    if lane not in SUBAGENT_MODEL_LANES:
        allowed = ", ".join(sorted(SUBAGENT_MODEL_LANES))
        raise ValueError(f"model_lane must be one of: {allowed}")
    return lane


def _slot_model(key: str) -> str:
    return str(os.environ.get(key, "") or SETTINGS_DEFAULTS.get(key, "") or "").strip()


def _use_local_for_lane(lane: str, model: str) -> bool:
    checks = {
        "main": ("OUROBOROS_MODEL", "USE_LOCAL_MAIN"),
        "heavy": ("OUROBOROS_MODEL_HEAVY", "USE_LOCAL_HEAVY"),
        "light": ("OUROBOROS_MODEL_LIGHT", "USE_LOCAL_LIGHT"),
    }
    pair = checks.get(lane)
    if not pair:
        return False
    model_key, local_key = pair
    slot_value = str(os.environ.get(model_key, "") or SETTINGS_DEFAULTS.get(model_key, "") or "").strip()
    if not slot_value and lane in {"heavy", "light"}:
        # Empty Heavy/Light resolves to the Main model -> follow Main's local flag, so
        # USE_LOCAL_MAIN governs the effective model rather than being silently ignored.
        return _use_local_for_lane("main", model)
    return (
        bool(model)
        and model == slot_value
        and str(os.environ.get(local_key, "") or "").strip().lower() in {"1", "true", "yes", "on"}
    )


def _lane_model(lane: str, slot_model: str = "") -> str:
    if lane == "main":
        return _slot_model("OUROBOROS_MODEL")
    if lane == "heavy":
        return get_heavy_model()  # empty heavy slot -> main
    if lane == "light":
        return get_light_model()  # empty light slot -> main
    if lane in {"review", "scope"} and slot_model:
        return str(slot_model).strip()
    return get_light_model()


def _review_or_scope_slots(lane: str) -> List[str]:
    if lane == "review":
        return [str(model).strip() for model in get_review_models() if str(model).strip()]
    if lane == "scope":
        return [str(model).strip() for model in get_scope_review_models() if str(model).strip()]
    return []


def resolve_subagent_lane(
    requested_lane: str,
    *,
    depth: int,
    slot_model: str = "",
    slot_index: int = 0,
    slot_count: int = 1,
    mutating: bool = False,
) -> SubagentLaneResolution:
    """Resolve a subagent's effective lane + model.

    - depth > capability limit: fall to light (cost guard for deep descendants). If the
      lane was EXPLICITLY requested as main/heavy, surface a visible downgrade note
      (P1: never a SILENT cognitive-horizon cut).
    - depth <= limit, ``auto``: a MUTATING first-level child (writes code/files) gets the
      strong ``heavy`` lane (->main by default); a read-only child stays ``light``.
    - depth <= limit, explicit lane: honored.
    """
    requested = normalize_subagent_model_lane(requested_lane)
    cap = _capability_depth_limit()
    d = int(depth or 0)
    downgrade_note = ""
    if d > cap:
        if requested in {"main", "heavy"}:
            downgrade_note = (
                f"requested '{requested}' lane at subagent depth {d} (> capability "
                f"depth limit {cap}); capped to 'light' to bound deep-swarm cost"
            )
        effective = "light"
    elif requested == "auto":
        effective = "heavy" if mutating else "light"
    else:
        effective = requested
    model = _lane_model(effective, slot_model=slot_model)
    return SubagentLaneResolution(
        requested_lane=requested,
        effective_lane=effective,
        model=model,
        use_local_model=_use_local_for_lane(effective, model),
        slot_index=int(slot_index or 0),
        slot_count=max(1, int(slot_count or 1)),
        downgrade_note=downgrade_note,
    )


def expand_subagent_lane_slots(
    requested_lane: str, *, depth: int, mutating: bool = False
) -> List[SubagentLaneResolution]:
    requested = normalize_subagent_model_lane(requested_lane)
    if int(depth or 0) > _capability_depth_limit():
        return [resolve_subagent_lane(requested, depth=depth, slot_count=1, mutating=mutating)]
    slot_models = _review_or_scope_slots(requested)
    if not slot_models:
        return [resolve_subagent_lane(requested, depth=depth, slot_count=1, mutating=mutating)]
    total = len(slot_models)
    return [
        resolve_subagent_lane(
            requested,
            depth=depth,
            slot_model=model,
            slot_index=idx,
            slot_count=total,
            mutating=mutating,
        )
        for idx, model in enumerate(slot_models)
    ]


def build_subagent_envelope(
    *,
    task_id: str,
    parent_task_id: str = "",
    root_task_id: str = "",
    task_group_id: str = "",
    depth: int = 0,
    role: str = "",
    requested_lane: str = "auto",
    effective_lane: str = "light",
    model: str = "",
    status: str = "",
    usage: Dict[str, Any] | None = None,
    cost_usd: float | None = None,
) -> Dict[str, Any]:
    usage_data = dict(usage or {})
    if cost_usd is None:
        try:
            raw_cost = usage_data.get("cost")
            if raw_cost is None:
                raw_cost = usage_data.get("cost_usd")
            cost_usd = float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            cost_usd = None
    return {
        "task_id": str(task_id or ""),
        "lineage": {
            "parent_task_id": str(parent_task_id or ""),
            "root_task_id": str(root_task_id or ""),
            "depth": int(depth or 0),
        },
        "task_group_id": str(task_group_id or ""),
        "role": str(role or ""),
        # Durable-data tolerance: this envelope is built for an ALREADY-RAN task from its
        # stored record, which may carry a legacy/unknown lane (e.g. a pre-v6.39 "code")
        # that the public schema now rejects. Coerce an unknown stored lane to a safe
        # default instead of raising (the public schedule_subagent schema stays strict —
        # this is NOT a "code"->"heavy" alias, just metadata robustness, symmetric with
        # the effective_lane guard that already existed).
        "requested_lane": normalize_subagent_model_lane(requested_lane if requested_lane in SUBAGENT_MODEL_LANES else "auto"),
        "effective_lane": normalize_subagent_model_lane(effective_lane if effective_lane in SUBAGENT_MODEL_LANES else "light"),
        "model": str(model or ""),
        "status": str(status or ""),
        "usage": usage_data,
        "cost_usd": round(float(cost_usd), 6) if cost_usd is not None else None,
    }


def compact_task_group(
    *,
    group_id: str,
    task_ids: Iterable[str],
    requested_lane: str,
    parent_task_id: str = "",
    root_task_id: str = "",
    role: str = "",
) -> Dict[str, Any]:
    ids = [str(task_id) for task_id in task_ids if str(task_id).strip()]
    return {
        "id": str(group_id or ""),
        "kind": "subagent_group",
        "task_ids": ids,
        "size": len(ids),
        "requested_lane": normalize_subagent_model_lane(requested_lane),
        "parent_task_id": str(parent_task_id or ""),
        "root_task_id": str(root_task_id or ""),
        "role": str(role or ""),
    }
