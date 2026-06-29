"""C: verify_and_record after-only artifact-lifecycle FLAG (a check that built then deleted a
declared deliverable). Flag-only: status stays pass; the structural fact reaches the ledger and
the advisory acceptance reviewer."""
from __future__ import annotations

from ouroboros.tools.registry import ToolContext
from ouroboros.tools.verify import _probe_artifact_lifecycle


def _ctx(tmp_path):
    work = tmp_path / "ws"
    work.mkdir()
    drive = tmp_path / "drive"
    drive.mkdir()
    return ToolContext(repo_dir=work, drive_root=drive, task_id="t"), work


def test_probe_flags_deleted_artifact_host(tmp_path):
    ctx, work = _ctx(tmp_path)
    (work / "kept.txt").write_text("x")  # present after the check
    # "out.so" was built then deleted by the check -> absent now
    lifecycle, missing = _probe_artifact_lifecycle(ctx, ["kept.txt", "out.so"], work, use_executor=False)
    by = {e["path"]: e for e in lifecycle}
    assert by["kept.txt"]["exists_after"] is True
    assert by["out.so"]["exists_after"] is False
    assert missing == ["out.so"]
    assert all(e["check_surface"] == "host" for e in lifecycle)


def test_probe_traversal_is_unavailable_not_probed(tmp_path):
    ctx, work = _ctx(tmp_path)
    lifecycle, missing = _probe_artifact_lifecycle(ctx, ["../../etc/passwd"], work, use_executor=False)
    assert lifecycle and lifecycle[0]["check_surface"] == "unavailable"
    assert lifecycle[0]["exists_after"] is None
    assert missing == []  # refused path is never reported as "missing" (no arbitrary host probe)


def test_ledger_carries_artifact_lifecycle_flag_only():
    from ouroboros.outcomes import build_verification_ledger

    led = build_verification_ledger(
        task={"id": "t", "task_contract": {}},
        loop_outcome={"outcome_axes": {"execution": {"status": "ok"}, "objective": {"status": "not_evaluated"}}},
        llm_trace={"tool_calls": [], "verification_receipts": [{
            "status": "pass", "contract_kind": "explicit_command", "check": "build.sh",
            "artifact_lifecycle": [{"path": "out.so", "exists_after": False, "check_surface": "host"}],
            "artifacts_missing_after": ["out.so"],
        }]},
        artifact_bundle={},
    )
    entry = next(e for e in led["entries"] if e.get("kind") == "verification_receipt")
    assert entry["status"] == "pass"  # FLAG-ONLY: a missing artifact does NOT flip the status
    assert entry["artifacts_missing_after"] == ["out.so"]
    assert entry["artifact_lifecycle"][0]["exists_after"] is False


def test_acceptance_summary_surfaces_and_redacts_missing_after():
    from ouroboros.review_evidence import _accept_verification_summary

    summary = _accept_verification_summary([
        {"status": "pass", "check": "a", "artifacts_missing_after": ["/work/out.so"]},
        {"status": "pass", "check": "b"},
    ])
    assert summary["artifacts_missing_after_any"] is True
    assert any("out.so" in p for p in summary["artifacts_missing_after"])
    assert summary["count"] == 2
