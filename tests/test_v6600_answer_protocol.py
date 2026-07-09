"""v6.60.0 (Phase 4) — answer protocol (contract-gated FINAL ANSWER), blocking
widening, and the bytes_equal verification mode.
"""
from __future__ import annotations

import pathlib

from ouroboros.contracts.task_contract import (
    answer_protocol_active,
    build_task_contract,
    normalize_answer_protocol,
)


# --- 4.1 contract field ---------------------------------------------------------

def test_normalize_answer_protocol_closed_enum():
    assert normalize_answer_protocol("final_answer_line") == "final_answer_line"
    assert normalize_answer_protocol("FINAL_ANSWER_LINE") == "final_answer_line"
    assert normalize_answer_protocol("") == ""
    assert normalize_answer_protocol(None) == ""
    assert normalize_answer_protocol("bogus") == ""  # unknown -> no protocol, never an instruction


def test_contract_carries_answer_protocol_and_inherits_via_metadata():
    contract = build_task_contract({"description": "x", "answer_protocol": "final_answer_line"})
    assert contract["answer_protocol"] == "final_answer_line"
    # metadata path (the subagent/CLI --task-metadata-json route)
    contract_meta = build_task_contract({"description": "x", "metadata": {"answer_protocol": "final_answer_line"}})
    assert contract_meta["answer_protocol"] == "final_answer_line"
    # default: no protocol
    assert build_task_contract({"description": "x"})["answer_protocol"] == ""


def test_answer_protocol_active_gate_reads_ctx_and_dicts():
    from types import SimpleNamespace

    assert answer_protocol_active({"answer_protocol": "final_answer_line"}) is True
    assert answer_protocol_active({"answer_protocol": ""}) is False
    ctx = SimpleNamespace(task_contract={"answer_protocol": "final_answer_line"}, task_metadata={})
    assert answer_protocol_active(ctx) is True
    ctx2 = SimpleNamespace(task_contract={}, task_metadata={"task_contract": {"answer_protocol": "final_answer_line"}})
    assert answer_protocol_active(ctx2) is True
    assert answer_protocol_active(SimpleNamespace(task_contract={}, task_metadata={})) is False


def test_context_injects_protocol_rule_only_when_declared(tmp_path):
    from ouroboros.context import build_runtime_section

    class _Env:
        repo_dir = str(tmp_path)
        drive_root = tmp_path

    task_with = {"id": "t1", "task_contract": {"answer_protocol": "final_answer_line"}}
    task_without = {"id": "t2", "task_contract": {}}
    with_rule = build_runtime_section(_Env(), task_with)
    without_rule = build_runtime_section(_Env(), task_without)
    assert "FINAL ANSWER" in with_rule
    assert "answer_protocol" in with_rule
    assert "FINAL ANSWER" not in without_rule


def test_system_prompt_carries_no_marker_doctrine():
    """The SYSTEM.md marker rule moved to the per-task contract: the default prompt
    must NOT instruct every task to emit FINAL ANSWER lines."""
    text = (pathlib.Path(__file__).resolve().parents[1] / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")
    assert "FINAL ANSWER" not in text
    assert "CANDIDATES:" not in text


def test_pacing_marker_phrases_are_protocol_gated():
    from types import SimpleNamespace

    from ouroboros.task_pacing import build_intrinsic_pacing_note
    from ouroboros.deadline_utils import utc_now
    import datetime

    created = utc_now() - datetime.timedelta(minutes=90)

    def _note(ctx):
        return build_intrinsic_pacing_note(
            ctx, created=created, now=utc_now(), round_idx=5, accumulated_usage={"cost": 1.0},
        )

    plain = SimpleNamespace(task_contract={}, task_metadata={})
    note_plain = _note(plain)
    assert note_plain is not None and "FINAL ANSWER" not in note_plain.text

    protocol = SimpleNamespace(task_contract={"answer_protocol": "final_answer_line"}, task_metadata={})
    note_protocol = _note(protocol)
    assert note_protocol is not None and "FINAL ANSWER" in note_protocol.text


# --- 4.2 blocking widening --------------------------------------------------------

def _result(signal, findings, tiers=("solved",)):
    from types import SimpleNamespace

    actors = [
        {"slot_id": f"s{i}", "signal": signal, "parsed": {"outcome_tier": tier}}
        for i, tier in enumerate(tiers)
    ]
    return SimpleNamespace(aggregate_signal=signal, actors=actors, parsed_findings=findings)


def test_obligations_widen_to_high_only_on_failing_aggregate():
    from ouroboros.loop import _collect_acceptance_obligations

    high_finding = {"severity": "high", "slot_id": "s0", "item": "missed requirement", "recommendation": "implement X"}
    critical_finding = {"severity": "critical", "slot_id": "s0", "item": "broken", "recommendation": "fix Y"}

    # PASS aggregate: high findings do NOT become obligations (critical-only bar).
    trace_pass = {}
    _collect_acceptance_obligations(trace_pass, _result("PASS", [high_finding, critical_finding]))
    items = {o["item"] for o in trace_pass["acceptance_obligations"]}
    assert items == {"broken"}

    # FAIL aggregate: high + critical both become obligations.
    trace_fail = {}
    _collect_acceptance_obligations(trace_fail, _result("FAIL", [high_finding, critical_finding]))
    items_fail = {o["item"] for o in trace_fail["acceptance_obligations"]}
    assert items_fail == {"missed requirement", "broken"}

    # blocked_with_evidence tier (even on non-FAIL signal): widened too.
    trace_blocked = {}
    _collect_acceptance_obligations(
        trace_blocked, _result("PASS", [high_finding], tiers=("blocked_with_evidence",))
    )
    assert {o["item"] for o in trace_blocked["acceptance_obligations"]} == {"missed requirement"}


def test_verdict_is_advisory_flag_is_gone():
    """The dead policy KEY is removed from every ReviewRequest (comments may still
    name it historically); enforcement semantics live in OUROBOROS_REVIEW_ENFORCEMENT."""
    hits = []
    for path in (pathlib.Path(__file__).resolve().parents[1] / "ouroboros").rglob("*.py"):
        if '"verdict_is_advisory":' in path.read_text(encoding="utf-8", errors="replace"):
            hits.append(str(path))
    assert hits == [], f"dead policy key still set in: {hits}"


# --- 4.3 bytes_equal ---------------------------------------------------------------

def test_bytes_equal_compare(tmp_path):
    from types import SimpleNamespace

    from ouroboros.tools.verify import _compare_files_bytes_equal

    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"hello world" * 100)
    b.write_bytes(b"hello world" * 100)
    ctx = SimpleNamespace()
    equal, detail = _compare_files_bytes_equal(ctx, [str(a), str(b)], tmp_path, use_executor=False)
    assert equal is True and "==" in detail

    # Introduce a one-byte divergence mid-file; the detail names the offset + hexdump.
    data = bytearray(b"hello world" * 100)
    data[500] = 0x00
    b.write_bytes(bytes(data))
    equal2, detail2 = _compare_files_bytes_equal(ctx, [str(a), str(b)], tmp_path, use_executor=False)
    assert equal2 is False
    assert "offset 500" in detail2 and "@" in detail2

    # Size mismatch (prefix case).
    b.write_bytes(b"hello world")
    equal3, detail3 = _compare_files_bytes_equal(ctx, [str(a), str(b)], tmp_path, use_executor=False)
    assert equal3 is False and "sizes" in detail3

    # Missing file is a fail, not an exception.
    equal4, detail4 = _compare_files_bytes_equal(ctx, [str(a), str(tmp_path / "nope")], tmp_path, use_executor=False)
    assert equal4 is False and "not found" in detail4


def test_verify_and_record_bytes_equal_requires_two_paths(tmp_path):
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools.verify import _verify_and_record

    work = tmp_path / "ws"
    work.mkdir()
    drive = tmp_path / "drive"
    drive.mkdir()
    ctx = ToolContext(repo_dir=work, drive_root=drive, task_id="t")
    out = _verify_and_record(
        ctx, contract_kind="explicit_command", check=["true"],
        expected_match="bytes_equal", artifact_paths=["only-one.txt"],
    )
    assert "TOOL_ARG_ERROR" in out and "exactly two files" in out
