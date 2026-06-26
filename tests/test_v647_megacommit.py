"""v6.47.0 mega-commit immune tests (TIER-5): verify-before-done (FR3), cooperative
subagents (FR2), workspace-aware code-intel (R1/R2/R5), skill-publish SSOT (FR1),
and the M2/M6 reliability invariants. Pure-logic where possible; a few use a tmp
git tree / user_files root."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

PY = sys.executable or "python3"  # portable interpreter for cross-platform check commands


# ── R2: PYTHONPATH repo-scrub env isolation ───────────────────────────────────
def test_scrub_repo_from_pythonpath_drops_only_repo_entry():
    from ouroboros.platform_layer import scrub_repo_from_pythonpath

    repo = "/obo/repo"
    sep = os.pathsep
    env = {"PYTHONPATH": sep.join([repo, "/app", "/usr/lib/py"]), "X": "1"}
    out = scrub_repo_from_pythonpath(env, repo)
    assert out["PYTHONPATH"] == sep.join(["/app", "/usr/lib/py"])
    assert out["X"] == "1"
    assert env["PYTHONPATH"].startswith(repo)  # original not mutated
    # only the repo entry -> PYTHONPATH removed entirely
    assert "PYTHONPATH" not in scrub_repo_from_pythonpath({"PYTHONPATH": repo}, repo)
    # no PYTHONPATH / no repo_dir -> no-op
    assert scrub_repo_from_pythonpath({"A": "b"}, repo) == {"A": "b"}
    assert scrub_repo_from_pythonpath({"PYTHONPATH": repo}, None) == {"PYTHONPATH": repo}
    # trailing-slash equivalence
    assert "PYTHONPATH" not in scrub_repo_from_pythonpath({"PYTHONPATH": repo + "/"}, repo)


def test_shell_env_for_cwd_scrubs_external_keeps_repo():
    from ouroboros.tools.shell import _shell_env_for_cwd

    repo = Path(tempfile.mkdtemp())
    (repo / "sub").mkdir()
    ext = Path(tempfile.mkdtemp())
    ctx = types.SimpleNamespace(repo_dir=str(repo))
    # a command inside the repo inherits os.environ (None -> no scrub)
    assert _shell_env_for_cwd(ctx, repo / "sub") is None
    # a command outside the repo gets a scrubbed env (dict, not None)
    env = _shell_env_for_cwd(ctx, ext)
    assert isinstance(env, dict)


# ── R5: effect-based artifact-audit gate ──────────────────────────────────────
def test_user_files_run_effect_gate():
    from ouroboros.tools.shell import _shallow_listing, _user_files_run_had_effect

    d = Path(tempfile.mkdtemp())
    (d / "a.txt").write_text("1")
    sig = _shallow_listing(d)
    assert _user_files_run_had_effect([], [], sig, d) is False  # read-only
    (d / "b.txt").write_text("2")
    assert _user_files_run_had_effect([], [], sig, d) is True   # new file
    assert _user_files_run_had_effect(["x"], ["x", "y"], None, d) is True  # git delta
    assert _user_files_run_had_effect(["x"], ["x"], None, d) is False


# ── R1: query_code root=user_files guards ─────────────────────────────────────
def test_query_code_user_files_empty_path_hard_error():
    from ouroboros.tools.query_code import _query_code

    ctx = types.SimpleNamespace(
        drive_root=tempfile.mkdtemp(), repo_dir=tempfile.mkdtemp(),
        workspace_root="", workspace_mode="", task_constraint=None,
    )
    out = _query_code(ctx, "symbols", root="user_files", path="")
    assert "requires an explicit path" in out


def test_query_code_user_files_blocked_for_subagent():
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.query_code import _query_code

    ctx = types.SimpleNamespace(
        drive_root=tempfile.mkdtemp(), repo_dir=tempfile.mkdtemp(),
        workspace_root="", workspace_mode="", task_constraint=TaskConstraint(mode="local_readonly_subagent"),
    )
    out = _query_code(ctx, "symbols", root="user_files", path="/whatever")
    assert "not available to subagents" in out


def test_query_code_structural_walk_is_bounded_and_symlink_safe():
    from ouroboros.tools.query_code import _walk_candidate_files

    d = Path(tempfile.mkdtemp())
    (d / "a.py").write_text("x=1")
    (d / "sub").mkdir()
    (d / "sub" / "b.py").write_text("y=2")
    outside = Path(tempfile.mkdtemp())
    (outside / "secret.py").write_text("S=1")
    try:
        os.symlink(outside, d / "escape")
    except OSError:
        pass
    files, note = _walk_candidate_files(d, d)
    names = {f.name for f in files}
    assert "a.py" in names and "b.py" in names
    assert "secret.py" not in names  # symlink escaping the root is dropped


# ── FR3: receipt store, grounding, flag, nudge ────────────────────────────────
def test_receipt_store_roundtrip_and_task_id_guard():
    from ouroboros import outcomes as O

    dr = tempfile.mkdtemp()
    O.append_verification_receipt(dr, "task-1", {"status": "pass", "check": "pytest"})
    rs = O.read_verification_receipts(dr, "task-1")
    assert rs and rs[0]["status"] == "pass"
    # an invalid task id must not escape the artifacts dir
    with pytest.raises(Exception):
        O.verification_receipts_path(dr, "../escape", create=True)


def test_merge_objective_warning_coexist():
    from ouroboros.outcomes import _merge_objective_warning

    obj = {"status": "not_evaluated"}
    _merge_objective_warning(obj, "residual_tool_errors_without_review")
    _merge_objective_warning(obj, "receipt_absent")
    assert obj["warning"] == "residual_tool_errors_without_review"  # primary unchanged
    assert obj["warnings"] == ["residual_tool_errors_without_review", "receipt_absent"]


def test_receipt_absent_flag_and_suppression():
    from ouroboros import outcomes as O

    def lo():
        return {"outcome_axes": {"execution": {"status": "ok"}, "objective": {"status": "not_evaluated"}}, "final_answer": ""}

    # effects + no grounding -> receipt_absent
    a = lo()
    O.apply_receipt_absent_flag(a, {"tool_calls": [{"tool": "commit_reviewed", "status": "ok"}]}, tempfile.mkdtemp(), "t1")
    assert a["outcome_axes"]["objective"].get("warning") == "receipt_absent"
    # a write/edit deliverable is its own grounding -> no flag
    b = lo()
    O.apply_receipt_absent_flag(b, {"tool_calls": [{"tool": "write_file", "status": "ok", "args": {"root": "user_files"}}]}, tempfile.mkdtemp(), "t2")
    assert "warning" not in b["outcome_axes"]["objective"]
    # a verify receipt -> no flag, and the receipt is injected into the trace for the ledger
    dr = tempfile.mkdtemp()
    O.append_verification_receipt(dr, "t3", {"status": "pass"})
    c = lo()
    tr = {"tool_calls": [{"tool": "commit_reviewed", "status": "ok"}]}
    O.apply_receipt_absent_flag(c, tr, dr, "t3")
    assert "warning" not in c["outcome_axes"]["objective"]
    assert tr.get("verification_receipts")


def test_receipt_absent_never_on_best_effort():
    from ouroboros import outcomes as O

    d = {"outcome_axes": {"execution": {"status": "best_effort"}, "objective": {"status": "not_evaluated"}}, "final_answer": ""}
    O.apply_receipt_absent_flag(d, {"tool_calls": [{"tool": "commit_reviewed", "status": "ok"}]}, tempfile.mkdtemp(), "t4")
    assert "warning" not in d["outcome_axes"]["objective"]


def test_m2_zero_grounding_flag():
    from ouroboros import outcomes as O

    # declared expected_output, no tool work, no structured answer -> M2
    a = {"outcome_axes": {"execution": {"status": "ok"}, "objective": {"status": "not_evaluated"}}, "final_answer": ""}
    O.apply_receipt_absent_flag(a, {"tool_calls": []}, tempfile.mkdtemp(), "m1", expected_output="report.html")
    assert a["outcome_axes"]["objective"].get("warning") == "expected_output_ungrounded"
    # a text-answer task (FINAL ANSWER present) is never M2-flagged
    b = {"outcome_axes": {"execution": {"status": "ok"}, "objective": {"status": "not_evaluated"}}, "final_answer": "42"}
    O.apply_receipt_absent_flag(b, {"tool_calls": []}, tempfile.mkdtemp(), "m2", expected_output="the number")
    assert "warning" not in b["outcome_axes"]["objective"]
    # no declared expected_output -> never M2
    c = {"outcome_axes": {"execution": {"status": "ok"}, "objective": {"status": "not_evaluated"}}, "final_answer": ""}
    O.apply_receipt_absent_flag(c, {"tool_calls": []}, tempfile.mkdtemp(), "m3", expected_output="")
    assert "warning" not in c["outcome_axes"]["objective"]


def test_nudge_gate_and_auto_equals_required():
    from ouroboros import outcomes as O

    tr = {"tool_calls": [{"tool": "commit_reviewed", "status": "ok"}]}
    # effects + no grounding -> nudge
    assert O.should_nudge_verification(tr, tempfile.mkdtemp(), "n1") is True
    # the durable flag is identical regardless of review mode (auto/required read the SAME store)
    dr = tempfile.mkdtemp()
    O.append_verification_receipt(dr, "n2", {"status": "pass"})
    assert O.should_nudge_verification(tr, dr, "n2") is False
    # no effects -> no nudge
    assert O.should_nudge_verification({"tool_calls": [{"tool": "read_file", "status": "ok"}]}, tempfile.mkdtemp(), "n3") is False


def test_verification_receipts_in_ledger():
    from ouroboros.outcomes import build_verification_ledger

    led = build_verification_ledger(
        task={"id": "t", "task_contract": {}},
        loop_outcome={"outcome_axes": {"execution": {"status": "ok"}, "objective": {"status": "not_evaluated"}}},
        llm_trace={"tool_calls": [], "verification_receipts": [{"status": "pass", "contract_kind": "explicit_command", "check": "pytest"}]},
        artifact_bundle={},
    )
    kinds = [e.get("kind") for e in led.get("entries", [])]
    assert "verification_receipt" in kinds


# ── M6: cosmetic/recovered tool errors never produce a terminal tool_failure ──
def test_m6_cosmetic_errors_no_terminal_tool_failure():
    from ouroboros.outcomes import REASON_TOOL_FAILURE, derive_loop_outcome

    # an unrecovered one-shot run_command non-zero exit is COSMETIC (T4) — the turn
    # finished with a real answer, so it must land execution ok / final_message, never
    # a terminal tool_failure.
    trace = {"tool_calls": [{"tool": "run_command", "status": "non_zero_exit", "is_error": True, "exit_code": 1, "result": "boom"}]}
    out = derive_loop_outcome("done", {}, trace)
    axes = out["outcome_axes"]
    assert axes["execution"]["status"] == "ok"
    assert out["reason_code"] != REASON_TOOL_FAILURE
    # the cosmetic residual is still surfaced as a warning (just never as tool_failure)
    assert axes["objective"].get("warning") == "residual_tool_errors_without_review"


# ── FR2: cooperative subagent shared tree + depth reservation ─────────────────
def test_depth_reservation_admits():
    from supervisor.events import _depth_reservation_admits

    def t(tid, parent, rt="R"):
        return {"id": tid, "parent_task_id": parent, "root_task_id": rt, "delegation_role": "subagent"}

    running = {"w1": {"task": t("P", "ROOT")}}
    pending = [t(f"c{i}", "ROOT") for i in range(6)]  # tree at cap=6
    # P is a running subagent with no direct child -> reservation admits ONE
    assert _depth_reservation_admits("R", "P", pending, running, 6) is True
    # once P has a direct child, no further reservation for P
    assert _depth_reservation_admits("R", "P", pending + [t("pc", "P")], running, 6) is False
    # parent not a running subagent -> no reservation
    assert _depth_reservation_admits("R", "ROOT", pending, running, 6) is False
    # hard ceiling (2*cap) bounds it
    assert _depth_reservation_admits("R", "P", [t(f"d{i}", "ROOT") for i in range(12)], running, 6) is False


def test_ensure_cooperative_shared_root_mints_git_tree(monkeypatch):
    from ouroboros.tools import control_delegation as CD

    projects_root = Path(tempfile.mkdtemp()) / "coop_projects"
    monkeypatch.setenv("OUROBOROS_SUBAGENT_PROJECTS_ROOT", str(projects_root))
    CD._COOP_SHARED_ROOTS.clear()
    ctx = types.SimpleNamespace(repo_dir=str(Path(tempfile.mkdtemp())), task_id="root-1")
    path = CD.ensure_cooperative_shared_root(ctx, "root-1")
    assert not path.startswith("⚠️"), path
    assert (Path(path) / ".git").exists()  # a real git tree was minted
    head = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert head  # has the seed commit
    # cached: a second call returns the SAME tree (one shared tree per task-tree)
    assert CD.ensure_cooperative_shared_root(ctx, "root-1") == path


def test_resolve_cooperative_write_root_routes_flat_parent(monkeypatch):
    from ouroboros.tools import control_delegation as CD

    monkeypatch.setenv("OUROBOROS_SUBAGENT_PROJECTS_ROOT", str(Path(tempfile.mkdtemp()) / "p"))
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "true")
    CD._COOP_SHARED_ROOTS.clear()
    ctx = types.SimpleNamespace(repo_dir=str(Path(tempfile.mkdtemp())), task_id="r2", task_constraint=None,
                                is_workspace_mode=lambda: False, is_direct_chat=False)
    eff, profile, err = CD.resolve_cooperative_write_root(ctx, "external_workspace", "", "", {"root_task_id": "r2"})
    assert err == "" and eff and (Path(eff) / ".git").exists()
    # an explicit write_root is passed through untouched
    eff2, _p, err2 = CD.resolve_cooperative_write_root(ctx, "external_workspace", "/some/dir", "", {})
    assert err2 == "" and eff2 == "/some/dir"


# ── FR1: skill-publish SSOT predicate ─────────────────────────────────────────
def test_submit_hub_eligibility_warnings_now_enabled():
    from ouroboros.skill_publish_eligibility import submit_hub_eligibility as E

    # THE desync fix: advisory-only warnings are publishable (was UI-disabled before)
    assert E(source="external", review_status="warnings", github_token_configured=True)["disabled"] is False
    assert E(source="external", review_status="clean", github_token_configured=True)["disabled"] is False
    assert E(source="external", review_status="blockers", github_token_configured=True)["disabled"] is True
    assert E(source="external", review_status="pending", github_token_configured=True)["disabled"] is True
    assert E(source="external", review_status="clean", review_profile="owner_attested", github_token_configured=True)["disabled"] is True
    assert E(source="external", review_status="clean", review_stale=True, github_token_configured=True)["disabled"] is True
    assert "GITHUB_TOKEN" in E(source="external", review_status="clean")["reason"]
    assert E(source="native", review_status="clean", github_token_configured=True)["visible"] is False


def test_publish_gate_and_predicate_share_statuses():
    # The backend publish gate uses the SAME SSOT status set as the UI predicate.
    from ouroboros.skill_publish_eligibility import PUBLISHABLE_STATUSES
    from ouroboros.skill_review_status import STATUS_CLEAN, STATUS_WARNINGS

    assert PUBLISHABLE_STATUSES == frozenset({STATUS_CLEAN, STATUS_WARNINGS})


# ── verify_and_record safety policy + dispatch guard ──────────────────────────
def test_verify_and_record_safety_policy_is_conditional():
    from ouroboros.safety import POLICY_CHECK_CONDITIONAL, TOOL_POLICY

    assert TOOL_POLICY.get("verify_and_record") == POLICY_CHECK_CONDITIONAL


def test_verify_and_record_check_is_shell_guarded_against_subagent_secret_read():
    # F1 (review #1): an acting subagent must NOT be able to read Ouroboros secrets
    # through verify_and_record's `check` — it routes through the same deterministic
    # shell guard as run_command.
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tools.shell_guards import process_shell_guard_args

    reg = ToolRegistry(repo_dir=".", drive_root=tempfile.mkdtemp())
    reg._ctx.task_constraint = TaskConstraint(mode="acting_subagent", surface="external_workspace", write_root=tempfile.mkdtemp())
    mapped = process_shell_guard_args("verify_and_record", {"check": "cat data/settings.json", "cwd": ""})
    assert mapped["cmd"] == ["sh", "-lc", "cat data/settings.json"]
    block = reg._run_shell_safety_check(mapped, "advanced")
    assert block and "SECRET" in block.upper()


def test_verify_string_check_no_safe_subject_bypass(monkeypatch):
    # triad round-3 #1: a STRING check runs via `sh -lc`, so a safe-looking first word
    # cannot bypass the LLM safety review (a compound `cat x; rm` would be shell-run);
    # a LIST check (argv, no shell) stays safe-subject-eligible like run_command.
    import ouroboros.safety as S

    monkeypatch.setattr(S, "_run_llm_check", lambda *a, **k: (True, "LLM_REACHED"))
    _ok, msg_list = S.check_safety("verify_and_record", {"contract_kind": "explicit_command", "check": ["cat", "x"]}, messages=[], ctx=None)
    assert msg_list != "LLM_REACHED"  # safe-subject bypass for an argv list
    _ok, msg_str = S.check_safety("verify_and_record", {"contract_kind": "explicit_command", "check": "cat x; rm -rf y"}, messages=[], ctx=None)
    assert msg_str == "LLM_REACHED"  # string check forced through the LLM review


def test_verify_and_record_reachable_in_workspace_mode():
    # F2 (review #1): the FR3 flagship must be callable by a top-level workspace task
    # (the benchmark /app context where verify-before-done matters most).
    from ouroboros.tools.registry import _WORKSPACE_ALLOWED_TOOLS

    assert "verify_and_record" in _WORKSPACE_ALLOWED_TOOLS


def test_verify_and_record_is_shell_guarded_not_process_command():
    # triad round-5: verify_and_record clears the PRE-EXECUTION shell guards (the security
    # boundary, which blocks a forbidden mutation before the handler runs) but is NOT in
    # _PROCESS_COMMAND_TOOLS — those POST-execution checks run AFTER the handler already
    # wrote the receipt, so they would not gate the durable receipt (an ordering inversion).
    from ouroboros.tools.registry import _PROCESS_COMMAND_TOOLS, _SHELL_GUARDED_TOOLS

    assert "verify_and_record" in _SHELL_GUARDED_TOOLS
    assert "verify_and_record" not in _PROCESS_COMMAND_TOOLS


def _verify_ctx(tmp_path, *, task_id="vhandler"):
    from ouroboros.tools.registry import ToolContext

    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    (tmp_path / "drive").mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(repo_dir=str(tmp_path / "repo"), drive_root=str(tmp_path / "drive"))
    ctx.task_id = task_id
    return ctx


def test_verify_and_record_handler_run_kinds(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    from ouroboros.outcomes import read_verification_receipts, verification_grounding_present
    from ouroboros.tools.verify import _verify_and_record

    ctx = _verify_ctx(tmp_path)
    assert "PASS" in _verify_and_record(ctx, contract_kind="explicit_command", check=[PY, "-c", "print('ok')"])
    rs = read_verification_receipts(ctx.drive_root, "vhandler")
    assert rs[-1]["status"] == "pass" and rs[-1]["returncode"] == 0
    assert "FAIL" in _verify_and_record(ctx, contract_kind="explicit_command", check=[PY, "-c", "import sys; sys.exit(1)"])
    assert read_verification_receipts(ctx.drive_root, "vhandler")[-1]["status"] == "fail"
    # expected-substring gates pass/fail even on exit 0
    assert "FAIL" in _verify_and_record(ctx, contract_kind="explicit_metric", check=[PY, "-c", "print('hello')"], expected="WORLD")
    assert "PASS" in _verify_and_record(ctx, contract_kind="explicit_metric", check=[PY, "-c", "print('hello')"], expected="hello")
    # the handler's pass status actually grounds the turn (handler<->grounding contract)
    assert verification_grounding_present({"tool_calls": []}, ctx.drive_root, "vhandler") is True


def test_verify_and_record_handler_fail_closed(tmp_path):
    from ouroboros.tools.verify import _verify_and_record

    ctx = _verify_ctx(tmp_path)
    assert "TOOL_ARG_ERROR" in _verify_and_record(ctx, contract_kind="some_future_kind")
    assert "requires `check`" in _verify_and_record(ctx, contract_kind="explicit_command")


def test_verify_and_record_handler_artifact_and_declared(tmp_path):
    from ouroboros.outcomes import read_verification_receipts
    from ouroboros.tools.verify import _verify_and_record

    ctx = _verify_ctx(tmp_path)
    # a relative artifact path resolves under the active workspace (the repo dir here)
    (Path(ctx.repo_dir) / "deliv.txt").write_text("x")
    assert "OBSERVED" in _verify_and_record(ctx, contract_kind="artifact_observation", artifact_paths=["deliv.txt"])
    assert "FAIL" in _verify_and_record(ctx, contract_kind="artifact_observation", artifact_paths=["missing.txt"])
    # triad #A: a RELATIVE path that escapes the workspace cannot probe arbitrary host
    # files (no `../../../etc/passwd` existence oracle) — confined post-resolution. The
    # security invariant is simply that the traversal target is NEVER OBSERVED (it is
    # refused on POSIX where the confinement path exists, or resolves to a missing
    # Windows path — either way, not an existence oracle).
    escaped = _verify_and_record(ctx, contract_kind="artifact_observation", artifact_paths=["../../../../../../etc/passwd"])
    assert "OBSERVED" not in escaped and "FAIL" in escaped
    # no_visible_machine_contract -> honest declared receipt (grounding)
    assert "DECLARED" in _verify_and_record(ctx, contract_kind="no_visible_machine_contract", check="manual UI review")
    assert read_verification_receipts(ctx.drive_root, "vhandler")[-1]["status"] == "declared"


def test_verify_and_record_receipt_truncation_is_disclosed(tmp_path, monkeypatch):
    # triad #C (BIBLE P1): a large check output is bounded in the durable receipt but the
    # truncation is DISCLOSED, never silent.
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    from ouroboros.outcomes import read_verification_receipts
    from ouroboros.tools.verify import _RECEIPT_OUTPUT_CAP, _verify_and_record

    ctx = _verify_ctx(tmp_path)
    _verify_and_record(ctx, contract_kind="explicit_command", check=[PY, "-c", f"print('x' * {_RECEIPT_OUTPUT_CAP + 5000})"])
    summary = read_verification_receipts(ctx.drive_root, "vhandler")[-1]["summary"]
    assert "truncated" in summary and "chars]" in summary
    assert len(summary) < _RECEIPT_OUTPUT_CAP + 200  # bounded


def test_grounding_statuses_match_handler_vocabulary():
    # F7 (review #1): every grounding status is one the handler can actually emit (no
    # dangling 'recorded'); 'fail' is excluded.
    from ouroboros.outcomes import _RECEIPT_GROUNDING_STATUSES

    assert _RECEIPT_GROUNDING_STATUSES == frozenset({"pass", "observed", "declared"})
    assert "fail" not in _RECEIPT_GROUNDING_STATUSES
    assert "recorded" not in _RECEIPT_GROUNDING_STATUSES


def test_fr2_deep_inheritance_resolves_shared_tree(monkeypatch, tmp_path):
    # F6 (review #1): the deep-inheritance lynchpin — an external_workspace child with
    # an EMPTY write_root inherits the parent's workspace_root (the shared cooperative
    # tree), so a grandchild builds in the same tree.
    monkeypatch.setenv("OUROBOROS_SUBAGENT_PROJECTS_ROOT", str(tmp_path / "p"))
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "true")
    from ouroboros.subagent_worktrees import provision_genesis_project
    from supervisor.events import _resolve_subagent_constraint

    handle = provision_genesis_project(repo_dir=str(tmp_path / "repo"), task_id="root")
    shared = handle.path
    ctx = types.SimpleNamespace(repo_dir=str(tmp_path / "repo"))
    requested = {"mode": "acting_subagent", "surface": "external_workspace", "write_root": "", "base_sha": handle.base_sha}
    constraint, resolved_ws, ws_mode, reject = _resolve_subagent_constraint(
        ctx, tid="grandchild", requested_constraint=requested, workspace_root=shared,
        workspace_mode="", base_sha=handle.base_sha, parent_task_id="root",
    )
    assert reject == "", reject
    assert resolved_ws == shared and ws_mode == "external_workspace"
    assert constraint["write_root"] == shared
