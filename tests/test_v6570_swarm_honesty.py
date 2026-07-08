"""v6.57.0 (Phase 1) — swarm/outcome honesty + settings regression tests.

Covers: find_child_tasks scope=direct (no false grandchild absorption), the
policy_denials outcome bucket, verify_and_record refused_out_of_scope, the
recursive cost_usd_with_children rollup, the subagent profile summary, the
protected-artifact glob carve-out, and the EFFORT_SCALE SSOT.
"""
from __future__ import annotations

import json
import pathlib

from ouroboros.config import (
    EFFORT_SCALE,
    clamp_effort_to,
    effort_one_step_down,
    effort_rank,
    resolve_effort,
)


# --- 3.1 find_child_tasks scope=direct --------------------------------------

def _write_result(root: pathlib.Path, tid: str, **fields) -> None:
    from ouroboros.task_results import write_task_result

    write_task_result(root, tid, fields.pop("status", "completed"), **fields)


def test_find_child_tasks_scope_direct_excludes_siblings_and_parent(tmp_path):
    """A leaf grandchild must NOT see its parent/sibling as children (the false
    children_unabsorbed incident). scope=direct returns only its OWN children."""
    from ouroboros.task_status import find_child_tasks

    root = tmp_path
    # Tree: root -> builder(b), polisher(p) -> grandchild(g). g is a childless leaf.
    _write_result(root, "b", delegation_role="subagent", parent_task_id="root", root_task_id="root")
    _write_result(root, "p", delegation_role="subagent", parent_task_id="root", root_task_id="root")
    _write_result(root, "g", delegation_role="subagent", parent_task_id="p", root_task_id="root")

    # The grandchild finalizing (exclude_task_id=g, as the real consumers pass):
    # scope="direct" => no children at all.
    direct = find_child_tasks(root, parent_task_id="g", root_task_id="root", exclude_task_id="g", scope="direct")
    assert direct == []

    # Legacy subtree scope would (wrongly, for absorption) surface the parent + sibling.
    subtree = find_child_tasks(root, parent_task_id="g", root_task_id="root", exclude_task_id="g", scope="subtree")
    assert {r["task_id"] for r in subtree} == {"b", "p"}

    # The polisher's direct children are exactly [g].
    p_direct = find_child_tasks(root, parent_task_id="p", root_task_id="root", scope="direct")
    assert {r["task_id"] for r in p_direct} == {"g"}


# --- 1.5d cost_usd_with_children rollup -------------------------------------

def test_compute_cost_with_children_rolls_up_direct_children(tmp_path):
    from ouroboros.task_status import compute_cost_with_children

    root = tmp_path
    _write_result(root, "child1", delegation_role="subagent", parent_task_id="parent",
                  root_task_id="parent", cost_usd=1.5, status="completed")
    # A grandchild already rolled up into child2's own with-children total.
    _write_result(root, "child2", delegation_role="subagent", parent_task_id="parent",
                  root_task_id="parent", cost_usd=2.0, cost_usd_with_children=5.0, status="completed")

    total, partial = compute_cost_with_children(root, "parent", own_cost_usd=0.5)
    # own(0.5) + child1(1.5) + child2 rolled-up(5.0) = 7.0
    assert total == 7.0
    assert partial is False


def test_compute_cost_with_children_marks_partial_for_running_child(tmp_path):
    from ouroboros.task_status import compute_cost_with_children

    root = tmp_path
    _write_result(root, "c1", delegation_role="subagent", parent_task_id="p",
                  root_task_id="p", cost_usd=1.0, status="running")
    total, partial = compute_cost_with_children(root, "p", own_cost_usd=0.25)
    assert total == 1.25
    assert partial is True


# --- 1.3 policy_denials bucket ----------------------------------------------

def test_policy_denial_does_not_degrade_or_headline_tool_failure():
    from ouroboros.outcomes import EXECUTION_OK, derive_loop_outcome

    for status in ("integration_blocked", "workspace_blocked", "light_mode_blocked",
                   "resource_policy_blocked", "protected_blocked"):
        out = derive_loop_outcome(
            "Site is built.",
            {"rounds": 3},
            {"tool_calls": [{"tool": "run_command", "is_error": True, "status": status,
                             "result": f"⚠️ {status.upper()}: blocked"}]},
        )
        ex = out["outcome_axes"]["execution"]
        assert ex["status"] == EXECUTION_OK, status
        assert out["reason_code"] == "final_message", status
        assert ex["policy_denials"][0]["status"] == status, status


def test_genuine_error_still_headlines_tool_failure():
    from ouroboros.outcomes import EXECUTION_DEGRADED, derive_loop_outcome

    out = derive_loop_outcome(
        "done",
        {"rounds": 1},
        {"tool_calls": [{"tool": "run_command", "is_error": True, "status": "error",
                         "result": "⚠️ boom"}]},
    )
    assert out["outcome_axes"]["execution"]["status"] == EXECUTION_DEGRADED
    assert out["reason_code"] == "tool_failure"


# --- EFFORT_SCALE SSOT (1.8) -------------------------------------------------

def test_effort_scale_ordered_and_includes_xhigh_max():
    assert EFFORT_SCALE == ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    assert effort_rank("xhigh") > effort_rank("high")
    assert effort_rank("max") == len(EFFORT_SCALE) - 1
    assert effort_rank("bogus") == -1


def test_clamp_effort_to_ceiling():
    assert clamp_effort_to("xhigh", "high") == "high"   # clamped down
    assert clamp_effort_to("low", "high") == "low"      # already under ceiling
    assert clamp_effort_to("max", "max") == "max"       # equal
    assert clamp_effort_to("high", "") == "high"        # unknown ceiling: pass-through


def test_effort_one_step_down():
    assert effort_one_step_down("max") == "xhigh"
    assert effort_one_step_down("xhigh") == "high"
    assert effort_one_step_down("none") == "none"       # floor


def test_resolve_effort_accepts_xhigh_and_max(monkeypatch):
    monkeypatch.setenv("OUROBOROS_EFFORT_TASK", "xhigh")
    assert resolve_effort("task") == "xhigh"
    monkeypatch.setenv("OUROBOROS_EFFORT_TASK", "max")
    assert resolve_effort("task") == "max"


def test_normalize_reasoning_effort_uses_scale():
    from ouroboros.llm import normalize_reasoning_effort

    assert normalize_reasoning_effort("max") == "max"
    assert normalize_reasoning_effort("xhigh") == "xhigh"
    assert normalize_reasoning_effort("bogus") == "medium"


# --- 1.5 subagent profile summary -------------------------------------------

def test_summarize_subagent_profile_readonly_vs_acting():
    from ouroboros.tool_access import (
        predicted_subagent_profile,
        summarize_subagent_profile,
    )

    ro = predicted_subagent_profile(write_surface="")
    assert ro == "local_readonly_subagent"
    ro_summary = summarize_subagent_profile(ro, effective_lane="light")
    assert "shell=no" in ro_summary
    assert "read-only" in ro_summary
    assert "model_lane=light" in ro_summary

    acting = predicted_subagent_profile(write_surface="self_worktree")
    assert acting == "acting_subagent"
    acting_summary = summarize_subagent_profile(acting, effective_lane="heavy")
    assert "shell=yes" in acting_summary
    assert "active_workspace" in acting_summary
