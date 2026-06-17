"""v6.37.0 guard (C3.1/C3.3): the parent's 'you may delegate / mutate / fan out
further' intent must propagate STRUCTURALLY via a typed delegation_budget on the
task contract and be surfaced in the child's prompt — instead of being lost in
freeform objective prose (the cyber-racing 'maximum subagents' request that
collapsed into 3 flat research leaves)."""


def test_delegation_budget_defaults_and_normalization():
    from ouroboros.contracts.task_contract import build_task_contract, normalize_delegation_budget

    c = build_task_contract({"objective": "x"})
    b = c["delegation_budget"]
    assert b["may_delegate"] is True
    assert b["may_mutate"] is False  # mutation is opt-in
    assert b["may_fan_out"] is True
    assert b["depth_remaining"] is None and b["max_children"] is None
    assert b["intent_note"] == ""

    c2 = build_task_contract({
        "objective": "x",
        "delegation_budget": {"may_mutate": "yes", "depth_remaining": "2", "intent_note": "  go deep  "},
    })
    b2 = c2["delegation_budget"]
    assert b2["may_mutate"] is True
    assert b2["depth_remaining"] == 2
    assert b2["intent_note"] == "go deep"

    # junk is coerced safely
    assert normalize_delegation_budget(None)["may_delegate"] is True
    assert normalize_delegation_budget({"depth_remaining": "junk"})["depth_remaining"] is None
    assert normalize_delegation_budget({"depth_remaining": -5})["depth_remaining"] == 0


def test_compose_subagent_text_surfaces_budget():
    from supervisor.events import _compose_subagent_text

    txt = _compose_subagent_text(
        "obj", role="builder", expected_output="out", constraints="", context="",
        delegation_budget={
            "may_delegate": True, "may_mutate": True, "may_fan_out": True,
            "depth_remaining": 2, "max_children": None,
            "intent_note": "build the whole game, delegate per subsystem",
        },
    )
    assert "[DELEGATION BUDGET]" in txt
    assert "depth_remaining=2" in txt
    assert "mutating descendants permitted" in txt
    assert "build the whole game, delegate per subsystem" in txt

    # no budget -> no section (back-compat with callers that don't pass one)
    txt2 = _compose_subagent_text("obj", role="r", expected_output="out", constraints="", context="")
    assert "[DELEGATION BUDGET]" not in txt2


def test_absorption_full_then_whole_pointer_and_grandchild_rollup():
    from ouroboros.task_status import format_subagent_absorption_message
    children = [
        {"task_id": "d1", "parent_task_id": "P", "status": "completed", "role": "a", "result": "A" * 50, "cost_usd": 0.1},
        {"task_id": "d2", "parent_task_id": "P", "status": "completed", "role": "b", "result": "B" * 5000, "cost_usd": 0.2},
        {"task_id": "gc1", "parent_task_id": "d1", "status": "completed", "role": "c", "result": "grandchild-secret"},
        {"task_id": "d3", "parent_task_id": "P", "status": "running", "role": "d", "result": ""},
    ]
    msg = format_subagent_absorption_message(children, parent_task_id="P", budget_chars=100)
    assert "[SUBAGENT_RESULTS" in msg
    assert "A" * 50 in msg                      # d1 injected in FULL (fits)
    assert "B" * 5000 not in msg                # d2 over budget -> NOT injected
    assert 'get_task_result("d2")' in msg       # d2 replaced WHOLE by a pointer
    assert "grandchild-secret" not in msg       # grandchild raw output rolled up, not in root
    assert "DEEPER DESCENDANTS" in msg and "gc1" in msg
    assert "STILL RUNNING" in msg and "d3" in msg

    msg2 = format_subagent_absorption_message(children, parent_task_id="P", budget_chars=1_000_000)
    assert "B" * 5000 in msg2                    # generous budget -> both full
