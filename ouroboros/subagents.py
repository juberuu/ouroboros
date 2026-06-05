"""Subagent lane, cap, and metadata helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from ouroboros.config import (
    SETTINGS_DEFAULTS,
    get_light_model,
    get_review_models,
    get_scope_review_models,
)

SUBAGENT_MODEL_LANES: frozenset[str] = frozenset({
    "auto",
    "main",
    "code",
    "light",
    "review",
    "scope",
})


@dataclass(frozen=True)
class SubagentLaneResolution:
    requested_lane: str
    effective_lane: str
    model: str
    use_local_model: bool = False
    slot_index: int = 0
    slot_count: int = 1


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
        "code": ("OUROBOROS_MODEL_CODE", "USE_LOCAL_CODE"),
        "light": ("OUROBOROS_MODEL_LIGHT", "USE_LOCAL_LIGHT"),
    }
    pair = checks.get(lane)
    if not pair:
        return False
    model_key, local_key = pair
    return (
        bool(model)
        and model == str(os.environ.get(model_key, "") or SETTINGS_DEFAULTS.get(model_key, "") or "").strip()
        and str(os.environ.get(local_key, "") or "").strip().lower() in {"1", "true", "yes", "on"}
    )


def _lane_model(lane: str, slot_model: str = "") -> str:
    if lane == "main":
        return _slot_model("OUROBOROS_MODEL")
    if lane == "code":
        return _slot_model("OUROBOROS_MODEL_CODE")
    if lane == "light":
        return get_light_model()
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
) -> SubagentLaneResolution:
    requested = normalize_subagent_model_lane(requested_lane)
    effective = "light" if int(depth or 0) > 1 else ("light" if requested == "auto" else requested)
    model = _lane_model(effective, slot_model=slot_model)
    return SubagentLaneResolution(
        requested_lane=requested,
        effective_lane=effective,
        model=model,
        use_local_model=_use_local_for_lane(effective, model),
        slot_index=int(slot_index or 0),
        slot_count=max(1, int(slot_count or 1)),
    )


def expand_subagent_lane_slots(requested_lane: str, *, depth: int) -> List[SubagentLaneResolution]:
    requested = normalize_subagent_model_lane(requested_lane)
    if int(depth or 0) > 1:
        return [resolve_subagent_lane(requested, depth=depth, slot_count=1)]
    slot_models = _review_or_scope_slots(requested)
    if not slot_models:
        return [resolve_subagent_lane(requested, depth=depth, slot_count=1)]
    total = len(slot_models)
    return [
        resolve_subagent_lane(
            requested,
            depth=depth,
            slot_model=model,
            slot_index=idx,
            slot_count=total,
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
            cost_usd = float(usage_data.get("cost") or usage_data.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost_usd = 0.0
    return {
        "task_id": str(task_id or ""),
        "lineage": {
            "parent_task_id": str(parent_task_id or ""),
            "root_task_id": str(root_task_id or ""),
            "depth": int(depth or 0),
        },
        "task_group_id": str(task_group_id or ""),
        "role": str(role or ""),
        "requested_lane": normalize_subagent_model_lane(requested_lane),
        "effective_lane": normalize_subagent_model_lane(effective_lane if effective_lane in SUBAGENT_MODEL_LANES else "light"),
        "model": str(model or ""),
        "status": str(status or ""),
        "usage": usage_data,
        "cost_usd": round(float(cost_usd or 0.0), 6),
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
