"""v6.52.2 — ephemeral `scratch=[...]` (Fix #1) + verify_and_record exit-masking sensor (Fix #2).

Both are GENERAL, leak-free, advisory-only. Additive: with no scratch / no masking, behavior is
unchanged. Includes the false-completion adversarial coverage DEVELOPMENT.md §651 mandates for the
loop nudge (one-shot, fires only on a masked unreconciled PASS, suppressed by a later clean pass,
ordered after the red nudge, advisory).
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ouroboros.tools.registry import ToolRegistry

# Spawns real OS subprocesses (git, the `sys.executable` Python, and a POSIX `sh` in one guarded
# case) via _tracked_subprocess_run — not parallel-safe under `pytest -n auto` (DEVELOPMENT.md
# "Parallel CI and the serial marker"). Python invocations use `sys.executable` (cross-platform);
# the only POSIX-shell-dependent test is skipped when `sh` is unavailable (e.g. Windows).
pytestmark = pytest.mark.serial


# ----------------------------------------------------------------------------- helpers
def _git_ws(parent, name="proj"):
    """A git worktree under user_files (scratch requires a git-worktree cwd)."""
    ws = parent / name
    ws.mkdir()
    _git(ws, "init", "-q")
    return ws


def _reg(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    home = tmp_path / "home"
    repo = home / "Ouroboros" / "repo"
    data = home / "Ouroboros" / "data"
    desktop = home / "Desktop"
    for d in (repo, data, desktop):
        d.mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", lambda: home)
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    registry._ctx.task_id = "task1"
    return registry, repo, data, desktop


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=T", *args], cwd=repo, check=True, capture_output=True)


# ============================================================ Fix #1: ephemeral scratch
def test_scratch_exempts_output_guard_and_is_not_an_artifact(tmp_path, monkeypatch):
    from ouroboros.artifacts import collect_task_artifact_records, read_task_scratch_fingerprints

    registry, _repo, data, desktop = _reg(tmp_path, monkeypatch)
    ws = _git_ws(desktop)  # scratch requires a git-worktree cwd
    target = ws / "scratch_check.py"
    result = registry.execute(
        "run_command",
        {
            "cmd": [sys.executable, "-c", f"open({str(target)!r}, 'w').write('x')"],
            "cwd": str(ws),
            "scratch": [str(target)],
        },
    )
    # Declared scratch is exempt from the undeclared-output guard...
    assert "ARTIFACT_OUTPUT_ERROR" not in result, result
    assert "exit_code=0" in result
    # ...and is never registered as a task artifact (the manifest itself is excluded too).
    assert collect_task_artifact_records(data, "task1") == []
    # ...but its FINGERPRINT is recorded (so patch capture can exclude it while it still matches).
    assert str(target.resolve()) in read_task_scratch_fingerprints(data, "task1")
    # ...and the agent is reminded to delete it (it still exists on disk).
    assert "SCRATCH_REMAINS" in result


def test_undeclared_write_still_blocks_without_scratch(tmp_path, monkeypatch):
    """0-regression: the SAME write WITHOUT scratch still trips the output guard."""
    registry, _repo, _data, desktop = _reg(tmp_path, monkeypatch)
    target = desktop / "undeclared.py"
    result = registry.execute(
        "run_command",
        {"cmd": [sys.executable, "-c", f"open({str(target)!r}, 'w').write('x')"], "cwd": str(desktop)},
    )
    assert result.startswith("⚠️ ARTIFACT_OUTPUT_ERROR"), result


def test_scratch_preexisting_path_is_blocked(tmp_path, monkeypatch):
    registry, _repo, _data, desktop = _reg(tmp_path, monkeypatch)
    ws = _git_ws(desktop)
    existing = ws / "already.py"
    existing.write_text("real\n")  # pre-exists -> not a throwaway
    result = registry.execute(
        "run_command",
        {"cmd": [sys.executable, "-c", "print('noop')"], "cwd": str(ws), "scratch": [str(existing)]},
    )
    assert result.startswith("⚠️ SCRATCH_BLOCKED"), result
    assert "already exists" in result


def test_scratch_traversal_outside_cwd_is_blocked(tmp_path, monkeypatch):
    registry, _repo, _data, desktop = _reg(tmp_path, monkeypatch)
    ws = _git_ws(desktop)
    result = registry.execute(
        "run_command",
        {"cmd": [sys.executable, "-c", "print('noop')"], "cwd": str(ws), "scratch": ["../escape.py"]},
    )
    assert result.startswith("⚠️ SCRATCH_BLOCKED"), result
    assert "escapes the command cwd" in result


def test_scratch_refused_outside_git_worktree(tmp_path, monkeypatch):
    """round-2 review CRITICAL: scratch must be refused when cwd is NOT a git worktree (so a new
    user_files deliverable cannot bypass the output guard by mislabeling itself scratch)."""
    registry, _repo, _data, desktop = _reg(tmp_path, monkeypatch)  # desktop is NOT a git repo
    target = desktop / "not_in_repo.py"
    result = registry.execute(
        "run_command",
        {"cmd": [sys.executable, "-c", f"open({str(target)!r}, 'w').write('x')"], "cwd": str(desktop), "scratch": [str(target)]},
    )
    assert result.startswith("⚠️ SCRATCH_BLOCKED"), result
    assert "git-worktree" in result


def test_scratch_fingerprint_recorded_even_on_timeout(tmp_path, monkeypatch):
    """round-2 review CRITICAL: a command that creates scratch then TIMES OUT must still record the
    fingerprint (else headless would leak the throwaway into the patch)."""
    from ouroboros.artifacts import read_task_scratch_fingerprints

    registry, _repo, data, desktop = _reg(tmp_path, monkeypatch)
    ws = _git_ws(desktop)
    target = ws / "scratch_timeout.txt"
    # Portable (no sh/touch/sleep): create the scratch file, then block past the 1s timeout.
    result = registry.execute(
        "run_command",
        {
            "cmd": [sys.executable, "-c", f"import pathlib,time; pathlib.Path({str(target)!r}).write_text('x'); time.sleep(5)"],
            "cwd": str(ws),
            "scratch": [str(target)],
            "timeout_sec": 1,
        },
    )
    assert "TOOL_TIMEOUT" in result, result
    assert str(target.resolve()) in read_task_scratch_fingerprints(data, "task1")


def test_audit_gap_audits_nonscratch_deliverable_despite_scratch(tmp_path, monkeypatch):
    """round-4 review CRITICAL: declaring scratch must NOT globally suppress the user_files audit —
    a SEPARATE real deliverable created in the same command must still trip ARTIFACT_AUDIT_GAP."""
    registry, _repo, _data, desktop = _reg(tmp_path, monkeypatch)
    ws = _git_ws(desktop)
    # `.touch()` carries no write-marker, so the hard output guard does not pre-empt; both files
    # become untracked effects. probe.py is declared scratch; deliv.txt is a real undeclared deliverable.
    code = (
        "import pathlib; "
        f"pathlib.Path({str(ws / 'probe.py')!r}).touch(); "
        f"pathlib.Path({str(ws / 'deliv.txt')!r}).touch()"
    )
    result = registry.execute(
        "run_command",
        {"cmd": [sys.executable, "-c", code], "cwd": str(ws), "scratch": [str(ws / "probe.py")]},
    )
    assert "ARTIFACT_OUTPUT_ERROR" not in result, result
    assert "ARTIFACT_AUDIT_GAP" in result, result  # the non-scratch deliverable is still audited


def test_headless_excludes_declared_scratch_from_workspace_patch(tmp_path):
    from hashlib import sha256
    from ouroboros.headless import SCRATCH_MANIFEST_NAME, write_workspace_patch_artifacts

    repo = tmp_path / "ws"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "--allow-empty", "-m", "base", "-q")
    (repo / "real_deliverable.txt").write_text("keep\n")          # genuine new file -> included
    scratch_body = "// throwaway\n"
    (repo / "scratch_probe_test.go").write_text(scratch_body)      # declared scratch -> excluded (sha match)

    art = tmp_path / "artifacts"
    art.mkdir()
    (art / SCRATCH_MANIFEST_NAME).write_text(json.dumps({"schema_version": 2, "scratch": {
        str((repo / "scratch_probe_test.go").resolve()): sha256(scratch_body.encode()).hexdigest()
    }}))
    _arts, manifest = write_workspace_patch_artifacts(repo, art, task={"id": "t", "workspace_root": str(repo)})
    included = manifest.get("untracked_included") or []
    excluded = [e.get("path") for e in (manifest.get("untracked_excluded") or [])]
    assert "real_deliverable.txt" in included
    assert "scratch_probe_test.go" not in included
    assert "scratch_probe_test.go" in excluded

    # CRITICAL FIX (round-1 review): a LATER real file at the SAME path (different content) must NOT
    # be dropped — the manifest is fingerprint-gated, not path-authoritative.
    (repo / "scratch_probe_test.go").write_text("// a REAL later edit, different content\n")
    art2 = tmp_path / "artifacts2"
    art2.mkdir()
    (art2 / SCRATCH_MANIFEST_NAME).write_text(json.dumps({"schema_version": 2, "scratch": {
        str((repo / "scratch_probe_test.go").resolve()): sha256(scratch_body.encode()).hexdigest()  # STALE sha
    }}))
    _a2, manifest2 = write_workspace_patch_artifacts(repo, art2, task={"id": "t", "workspace_root": str(repo)})
    assert "scratch_probe_test.go" in (manifest2.get("untracked_included") or [])  # sha mismatch -> included

    # 0-regression: with NO scratch manifest, the file IS included.
    art3 = tmp_path / "artifacts3"
    art3.mkdir()
    _a3, manifest3 = write_workspace_patch_artifacts(repo, art3, task={"id": "t", "workspace_root": str(repo)})
    assert "scratch_probe_test.go" in (manifest3.get("untracked_included") or [])


# ============================================================ Fix #2: exit-masking sensor
def test_check_has_exit_masking_detection():
    from ouroboros.tools.verify import _check_has_exit_masking

    assert _check_has_exit_masking(["sh", "-c", "node t.js -f 2>&1 | tail -5"])[0] is True
    assert _check_has_exit_masking(["bash", "-c", "make test || true"])[0] is True
    assert _check_has_exit_masking(["sh", "-c", "run.sh 2>/dev/null"])[0] is True
    assert _check_has_exit_masking(["sh", "-c", "pytest -q"])[0] is False
    assert _check_has_exit_masking(["go", "test", "./..."])[0] is False  # list argv, no shell
    # a QUOTED `| tail` literal (e.g. a grep pattern) must NOT be flagged (shlex token scan)
    assert _check_has_exit_masking(["sh", "-c", "grep PATTERN '| tail'"])[0] is False
    # round-1 review CRITICAL: NO-SPACE operators must still be detected (shlex.split missed these)
    assert _check_has_exit_masking(["sh", "-c", "pytest -q|tail -1"])[0] is True
    assert _check_has_exit_masking(["bash", "-c", "make test||true"])[0] is True


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"), reason="exercises a POSIX-shell masked check (sh + tail)")
def test_verify_and_record_sets_masking_flag_on_receipt(tmp_path, monkeypatch):
    from ouroboros.outcomes import read_verification_receipts

    registry, repo, data, _desktop = _reg(tmp_path, monkeypatch)
    # masked: `echo hi | tail -1` exits 0 (tail), masking any upstream failure; expected matches.
    registry.execute(
        "verify_and_record",
        {"contract_kind": "explicit_command", "check": ["sh", "-c", "echo hi | tail -1"], "expected": "hi", "cwd": str(repo)},
    )
    rec = read_verification_receipts(data, "task1")[-1]
    assert rec["status"] == "pass"  # FLAG-ONLY: masking does not flip the verdict
    assert rec.get("check_exit_masking") is True
    assert "pipeline_tail" in (rec.get("check_exit_masking_reasons") or [])
    # clean check: no masking flag at all
    registry.execute(
        "verify_and_record",
        {"contract_kind": "explicit_command", "check": ["sh", "-c", "echo hi"], "expected": "hi", "cwd": str(repo)},
    )
    assert not read_verification_receipts(data, "task1")[-1].get("check_exit_masking")


def test_latest_unreconciled_masked_pass_predicate():
    from ouroboros.outcomes import latest_unreconciled_masked_pass as mp

    assert bool(mp([{"status": "pass", "check_exit_masking": True}])) is True
    # a later CLEAN (non-masked) pass reconciles it
    assert bool(mp([{"status": "pass", "check_exit_masking": True}, {"status": "pass"}])) is False
    # a clean-only history never flags
    assert bool(mp([{"status": "pass"}])) is False


def test_masking_flag_is_projected_into_ledger_status_unchanged():
    from ouroboros.outcomes import build_verification_ledger

    led = build_verification_ledger(
        task={"id": "t", "task_contract": {}},
        loop_outcome={"outcome_axes": {"execution": {"status": "ok"}, "objective": {"status": "not_evaluated"}}},
        llm_trace={"tool_calls": [], "verification_receipts": [{
            "status": "pass", "contract_kind": "explicit_command", "check": "sh -c 'x | tail'",
            "check_exit_masking": True, "check_exit_masking_reasons": ["pipeline_tail"],
        }]},
        artifact_bundle={},
    )
    entry = next(e for e in led["entries"] if e.get("kind") == "verification_receipt")
    assert entry["status"] == "pass"
    assert entry["check_exit_masking"] is True
    assert "pipeline_tail" in entry["check_exit_masking_reasons"]


def test_acceptance_summary_surfaces_masking():
    from ouroboros.review_evidence import _accept_verification_summary

    summary = _accept_verification_summary([
        {"status": "pass", "check": "sh -c 'x | tail'", "check_exit_masking": True, "check_exit_masking_reasons": ["pipeline_tail"]},
    ])
    assert summary["check_exit_masking_unreconciled"] is True
    assert "pipeline_tail" in summary["check_exit_masking_reasons"]


def test_masked_verification_nudge_one_shot_advisory_and_ordering(tmp_path):
    from ouroboros.loop import _maybe_inject_finalization_nudges
    from ouroboros.outcomes import append_verification_receipt

    drive = tmp_path / "drive"
    drive.mkdir()
    append_verification_receipt(drive, "t", {
        "status": "pass", "check": "sh -c 'node t | tail'",
        "check_exit_masking": True, "check_exit_masking_reasons": ["pipeline_tail"],
    })

    def _run(ctx_obj, msgs):
        return _maybe_inject_finalization_nudges(
            SimpleNamespace(_ctx=ctx_obj), drive, "t",
            {"reasoning_notes": [], "tool_calls": []}, "done", msgs, lambda *_: None,
        )

    ctx = SimpleNamespace()
    msgs: list = []
    assert _run(ctx, msgs) is True
    assert any("hide the real command's exit code" in m.get("content", "") for m in msgs)
    # one-shot: the latch suppresses a second injection
    assert _run(ctx, []) is False

    # a later CLEAN pass reconciles the masked pass -> no nudge on a fresh ctx
    append_verification_receipt(drive, "t", {"status": "pass", "check": "pytest -q"})
    assert _run(SimpleNamespace(), []) is False

    # ORDERING: a RED receipt makes the red nudge win (not the masked one)
    drive2 = tmp_path / "drive2"
    drive2.mkdir()
    append_verification_receipt(drive2, "t", {"status": "fail", "returncode": 1, "check": "pytest -q"})
    msgs2: list = []
    fired = _maybe_inject_finalization_nudges(
        SimpleNamespace(_ctx=SimpleNamespace()), drive2, "t",
        {"reasoning_notes": [], "tool_calls": []}, "done", msgs2, lambda *_: None,
    )
    assert fired is True
    assert any("RED" in m.get("content", "") for m in msgs2)
