"""Tests for mutative ("acting") subagents: authority, fail-closed profile,
registry gating, worktree lifecycle, spawn gate, and patch integration."""

import json
import pathlib
import subprocess
from types import SimpleNamespace

import pytest

from ouroboros.contracts.task_constraint import (
    VALID_WRITE_SURFACES,
    TaskConstraint,
    normalize_task_constraint,
)
from ouroboros.tool_access import active_tool_profile
from ouroboros.tool_capabilities import ACTING_SUBAGENT_MODE, ACTING_SUBAGENT_TOOL_NAMES
from ouroboros.runtime_mode_policy import mode_allows_protected_write
from ouroboros.tools.registry import ToolContext, ToolRegistry
from ouroboros import subagent_worktrees as sw


def _git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=check)


def _init_repo(path: pathlib.Path, files: dict) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    for rel, content in files.items():
        fp = path / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return _git(path, "rev-parse", "HEAD").stdout.strip()


# --------------------------------------------------------------------------- #
# 1. TaskConstraint normalization
# --------------------------------------------------------------------------- #
def test_acting_constraint_normalize_forces_invariants():
    c = normalize_task_constraint({
        "mode": "acting_subagent",
        "surface": "self_worktree",
        "write_root": "/tmp/wt",
        "base_sha": "abc",
        "protected_paths_grant": True,
        "external_tool_grants": ["mcp_x", "", "  "],
        "allow_enable": True,
        "allow_review": True,
    })
    assert c.mode == ACTING_SUBAGENT_MODE
    assert c.surface == "self_worktree"
    assert c.allow_enable is False and c.allow_review is False
    assert c.parent_only_commit is True
    assert c.external_tool_grants == ("mcp_x",)
    assert c.return_kind == "workspace_patch"


def test_acting_constraint_invalid_surface_blanked():
    c = normalize_task_constraint({"mode": "acting_subagent", "surface": "bogus"})
    assert c.mode == ACTING_SUBAGENT_MODE
    assert c.surface == ""


def test_acting_constraint_instance_repins_flags():
    c = normalize_task_constraint(TaskConstraint(mode="acting_subagent", allow_enable=True, allow_review=True))
    assert c.allow_enable is False and c.allow_review is False and c.parent_only_commit is True


# --------------------------------------------------------------------------- #
# 2. Fail-closed profile (the core safety invariant)
# --------------------------------------------------------------------------- #
def _profile_ctx(tmp_path, *, constraint=None, metadata=None):
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    drive = tmp_path / "data"; drive.mkdir(exist_ok=True)
    return ToolContext(
        repo_dir=repo, drive_root=drive,
        task_constraint=constraint,
        task_metadata=metadata or {},
    )


def test_profile_acting_valid_surface(tmp_path):
    ctx = _profile_ctx(tmp_path, constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree"))
    assert active_tool_profile(ctx) == "acting_subagent"


def test_profile_acting_invalid_surface_fails_closed(tmp_path):
    ctx = _profile_ctx(tmp_path, constraint=TaskConstraint(mode="acting_subagent", surface=""))
    assert active_tool_profile(ctx) == "local_readonly_subagent"


def test_profile_readonly(tmp_path):
    ctx = _profile_ctx(tmp_path, constraint=TaskConstraint(mode="local_readonly_subagent"))
    assert active_tool_profile(ctx) == "local_readonly_subagent"


def test_profile_subagent_without_constraint_fails_closed(tmp_path):
    # Delegated subagent with a missing constraint must NEVER inherit self_modification.
    ctx = _profile_ctx(tmp_path, constraint=None, metadata={"delegation_role": "subagent"})
    assert active_tool_profile(ctx) == "local_readonly_subagent"


def test_profile_normal_task_is_self_modification(tmp_path):
    ctx = _profile_ctx(tmp_path, constraint=None, metadata={})
    assert active_tool_profile(ctx) == "self_modification"


# --------------------------------------------------------------------------- #
# 3. Registry gating for acting subagents
# --------------------------------------------------------------------------- #
def _acting_registry(tmp_path, *, surface="self_worktree", grant=False, grants=()):
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    drive = tmp_path / "data"; drive.mkdir(exist_ok=True)
    worktree = tmp_path / "wt"; worktree.mkdir(exist_ok=True)
    ctx = ToolContext(
        repo_dir=repo, drive_root=drive,
        workspace_root=str(worktree), workspace_mode=surface,
        task_constraint=TaskConstraint(
            mode="acting_subagent", surface=surface, write_root=str(worktree),
            protected_paths_grant=grant, external_tool_grants=tuple(grants),
        ),
    )
    reg = ToolRegistry(repo_dir=repo, drive_root=drive)
    reg._ctx = ctx
    return reg, ctx, worktree


def test_acting_blocks_commit(tmp_path):
    reg, _ctx, _wt = _acting_registry(tmp_path)
    out = reg.execute("commit_reviewed", {"message": "x"})
    assert "ACTING_SUBAGENT_BLOCKED" in out


def test_acting_allows_write_in_worktree(tmp_path):
    reg, _ctx, worktree = _acting_registry(tmp_path)
    out = reg.execute("write_file", {"root": "active_workspace", "path": "feature.txt", "content": "hi\n"})
    assert "ACTING_SUBAGENT_BLOCKED" not in out
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "hi\n"


def test_acting_protected_write_blocked_without_pro_grant(tmp_path):
    reg, _ctx, _wt = _acting_registry(tmp_path, surface="self_worktree", grant=False)
    out = reg.execute("write_file", {"root": "active_workspace", "path": "ouroboros/safety.py", "content": "x\n"})
    # advanced (default) + no grant => protected block, regardless of grant.
    assert "protected" in out.lower() or "PROTECTED" in out


def test_acting_tool_visibility_is_acting_set(tmp_path):
    reg, _ctx, _wt = _acting_registry(tmp_path)
    names = set(reg.initial_tool_names())
    assert names == set(ACTING_SUBAGENT_TOOL_NAMES)
    assert "commit_reviewed" not in names
    assert "integrate_subagent_patch" in names


# --------------------------------------------------------------------------- #
# 4. Worktree lifecycle
# --------------------------------------------------------------------------- #
def test_worktree_provision_remove(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    head = _init_repo(repo, {"a.txt": "hi\n"})
    data = tmp_path / "data"; data.mkdir()
    wtroot = tmp_path / "wtroot"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data))
    h = sw.provision_worktree(repo_dir=repo, task_id="t1", worktree_root=wtroot, data_dir=data)
    assert pathlib.Path(h.path).exists()
    assert h.base_sha == head
    assert sw.list_worktrees(data_dir=data)[0]["task_id"] == "t1"
    assert sw.remove_worktree(task_id="t1", worktree_root=wtroot, data_dir=data)
    assert not pathlib.Path(h.path).exists()
    assert sw.list_worktrees(data_dir=data) == []


def test_worktree_root_isolation_guard(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    with pytest.raises(ValueError):
        sw.provision_worktree(repo_dir=repo, task_id="t", worktree_root=repo / "inside", data_dir=tmp_path / "d")


def test_worktree_prune_guards_outside_root(tmp_path):
    # A corrupt registry entry pointing OUTSIDE the worktree root must never cause deletion.
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    data = tmp_path / "data"; data.mkdir()
    wtroot = tmp_path / "wtroot"
    outside = tmp_path / "precious"; outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    sw._save_registry(
        [{"task_id": "evil", "path": str(outside), "branch": "", "repo_dir": str(repo), "created_at": 0.0}],
        data_dir=data,
    )
    sw.prune_orphans(worktree_root=wtroot, data_dir=data, retention_days=0)
    assert outside.exists() and (outside / "keep.txt").exists()  # guard prevented deletion


def test_worktree_prune_missing(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    data = tmp_path / "data"; data.mkdir()
    wtroot = tmp_path / "wtroot"
    h = sw.provision_worktree(repo_dir=repo, task_id="t2", worktree_root=wtroot, data_dir=data)
    import shutil
    shutil.rmtree(h.path)
    res = sw.prune_orphans(worktree_root=wtroot, data_dir=data, retention_days=9999)
    assert res["removed"] == 1 and res["kept"] == 0


# --------------------------------------------------------------------------- #
# 5. control._build_acting_constraint
# --------------------------------------------------------------------------- #
def test_build_acting_constraint_toggle(monkeypatch):
    from ouroboros.tools.control import _build_acting_constraint
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "false")
    err = _build_acting_constraint(write_surface="self_worktree", write_root="", protected_paths_grant=False, external_tool_grants=None, parent_workspace_root="")
    assert isinstance(err, str) and "MUTATIVE_SUBAGENTS_DISABLED" in err
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "true")
    c = _build_acting_constraint(write_surface="self_worktree", write_root="", protected_paths_grant=False, external_tool_grants=["x"], parent_workspace_root="")
    assert isinstance(c, dict) and c["mode"] == ACTING_SUBAGENT_MODE and c["external_tool_grants"] == ["x"]


def test_build_acting_constraint_bad_surface(monkeypatch):
    from ouroboros.tools.control import _build_acting_constraint
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "true")
    err = _build_acting_constraint(write_surface="bogus", write_root="", protected_paths_grant=False, external_tool_grants=None, parent_workspace_root="")
    assert isinstance(err, str) and "write_surface" in err


def test_build_acting_external_requires_root(monkeypatch):
    from ouroboros.tools.control import _build_acting_constraint
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "true")
    err = _build_acting_constraint(write_surface="external_workspace", write_root="", protected_paths_grant=False, external_tool_grants=None, parent_workspace_root="")
    assert isinstance(err, str) and "external_workspace" in err


# --------------------------------------------------------------------------- #
# 6. events._resolve_subagent_constraint (authoritative gate + provisioning)
# --------------------------------------------------------------------------- #
def test_resolve_readonly_passthrough(tmp_path):
    from supervisor.events import _resolve_subagent_constraint
    ctx = SimpleNamespace(REPO_DIR=tmp_path / "repo")
    c, wr, wm, detail = _resolve_subagent_constraint(
        ctx, tid="t", requested_constraint={"mode": "local_readonly_subagent"},
        workspace_root="", workspace_mode="", base_sha="", parent_task_id="",
    )
    assert c["mode"] == "local_readonly_subagent" and detail == ""


def test_resolve_acting_disabled_rejects(tmp_path, monkeypatch):
    from supervisor.events import _resolve_subagent_constraint
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "false")
    ctx = SimpleNamespace(REPO_DIR=tmp_path / "repo")
    c, wr, wm, detail = _resolve_subagent_constraint(
        ctx, tid="t", requested_constraint={"mode": "acting_subagent", "surface": "self_worktree"},
        workspace_root="", workspace_mode="", base_sha="", parent_task_id="",
    )
    assert c["mode"] == "local_readonly_subagent" and "disabled" in detail.lower()


def test_resolve_acting_self_worktree_provisions(tmp_path, monkeypatch):
    from supervisor.events import _resolve_subagent_constraint
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "true")
    monkeypatch.setenv("OUROBOROS_SUBAGENT_WORKTREE_ROOT", str(tmp_path / "wtroot"))
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path / "data"))
    ctx = SimpleNamespace(REPO_DIR=repo)
    c, wr, wm, detail = _resolve_subagent_constraint(
        ctx, tid="tw", requested_constraint={"mode": "acting_subagent", "surface": "self_worktree"},
        workspace_root="", workspace_mode="", base_sha="", parent_task_id="p",
    )
    assert detail == ""
    assert c["mode"] == "acting_subagent" and c["surface"] == "self_worktree"
    assert wr and pathlib.Path(wr).exists() and wm == "self_worktree"
    assert c["write_root"] == wr and c["base_sha"]


def test_reject_cleans_up_provisioned_worktree(tmp_path, monkeypatch):
    from supervisor.events import _resolve_subagent_constraint, _cleanup_rejected_worktree
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "true")
    monkeypatch.setenv("OUROBOROS_SUBAGENT_WORKTREE_ROOT", str(tmp_path / "wtroot"))
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path / "data"))
    ctx = SimpleNamespace(REPO_DIR=repo)
    c, wr, wm, detail = _resolve_subagent_constraint(
        ctx, tid="leak1", requested_constraint={"mode": "acting_subagent", "surface": "self_worktree"},
        workspace_root="", workspace_mode="", base_sha="", parent_task_id="p",
    )
    assert pathlib.Path(wr).exists()  # provisioned
    # A later rejection gate fires -> _reject_schedule_task tears the worktree down.
    _cleanup_rejected_worktree("leak1", {"task_constraint": c})
    assert not pathlib.Path(wr).exists()  # no leak


# --------------------------------------------------------------------------- #
# 7. integrate_subagent_patch
# --------------------------------------------------------------------------- #
def _make_child_patch(target_repo: pathlib.Path, drive: pathlib.Path, child_id: str, rel: str, new_content: str, parent_task_id: str = "parent1"):
    """Produce a real workspace.patch + manifest + lineage task_result for ``rel``."""
    from ouroboros.artifacts import task_artifact_dir_path
    from ouroboros.task_results import task_result_path
    from hashlib import sha256
    (target_repo / rel).parent.mkdir(parents=True, exist_ok=True)
    original = (target_repo / rel).read_text(encoding="utf-8") if (target_repo / rel).exists() else ""
    (target_repo / rel).write_text(new_content, encoding="utf-8")
    patch = _git(target_repo, "diff", "--binary", "HEAD", "--").stdout
    # revert working tree so the patch can be applied fresh by the tool
    _git(target_repo, "checkout", "--", rel) if original else (target_repo / rel).unlink()
    art = task_artifact_dir_path(drive, child_id, create=True)
    (art / "workspace.patch").write_text(patch, encoding="utf-8")
    digest = sha256(patch.encode("utf-8")).hexdigest()
    manifest = {
        "schema_version": 1, "status": "ready_with_changes",
        "patch_name": "workspace.patch", "sha256": digest,
        "tracked_changed": [rel] if original else [], "untracked_included": [] if original else [rel],
        "diffstat": f"{rel} | 1 +",
    }
    (art / "workspace_patch.json").write_text(json.dumps(manifest), encoding="utf-8")
    tr = task_result_path(drive, child_id)
    tr.parent.mkdir(parents=True, exist_ok=True)
    tr.write_text(json.dumps({"id": child_id, "parent_task_id": parent_task_id, "status": "done"}), encoding="utf-8")
    return art


def _integrate_ctx(target_repo, drive, **constraint_kw):
    tc = TaskConstraint(**constraint_kw) if constraint_kw else None
    return ToolContext(repo_dir=target_repo, drive_root=drive, task_constraint=tc, task_id="parent1")


def test_integrate_apply_happy(tmp_path):
    from ouroboros.tools.subagent_integration import _integrate_subagent_patch
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    drive = tmp_path / "data"; drive.mkdir()
    _make_child_patch(repo, drive, "child1", "a.txt", "hi\nworld\n")
    ctx = _integrate_ctx(repo, drive)
    out = _integrate_subagent_patch(ctx, task_id="child1", reason="best of N")
    assert "Integrated subagent patch" in out
    assert (repo / "a.txt").read_text(encoding="utf-8") == "hi\nworld\n"
    # verdict artifact written
    from ouroboros.artifacts import task_artifact_dir_path
    vp = task_artifact_dir_path(drive, "parent1") / "subagent_patch_verdict_child1.json"
    assert vp.exists() and json.loads(vp.read_text())["outcome"] == "applied"


def test_integrate_reject_records_verdict(tmp_path):
    from ouroboros.tools.subagent_integration import _integrate_subagent_patch
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    drive = tmp_path / "data"; drive.mkdir()
    _make_child_patch(repo, drive, "child2", "a.txt", "hi\nx\n")
    ctx = _integrate_ctx(repo, drive)
    out = _integrate_subagent_patch(ctx, task_id="child2", decision="reject", reason="worse")
    assert "Rejected subagent patch" in out
    assert (repo / "a.txt").read_text(encoding="utf-8") == "hi\n"  # unchanged


def test_integrate_protected_blocked_in_advanced(tmp_path):
    from ouroboros.tools.subagent_integration import _integrate_subagent_patch
    repo = tmp_path / "repo"
    _init_repo(repo, {"ouroboros/safety.py": "X = 1\n"})
    drive = tmp_path / "data"; drive.mkdir()
    _make_child_patch(repo, drive, "child3", "ouroboros/safety.py", "X = 2\n")
    ctx = _integrate_ctx(repo, drive)
    out = _integrate_subagent_patch(ctx, task_id="child3")
    assert "protected" in out.lower() or "PROTECTED" in out
    assert (repo / "ouroboros/safety.py").read_text(encoding="utf-8") == "X = 1\n"  # unchanged


def test_integrate_corrupt_sha_refused(tmp_path):
    from ouroboros.tools.subagent_integration import _integrate_subagent_patch
    from ouroboros.artifacts import task_artifact_dir_path
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    drive = tmp_path / "data"; drive.mkdir()
    art = _make_child_patch(repo, drive, "child4", "a.txt", "hi\nz\n")
    manifest = json.loads((art / "workspace_patch.json").read_text())
    manifest["sha256"] = "deadbeef"
    (art / "workspace_patch.json").write_text(json.dumps(manifest), encoding="utf-8")
    ctx = _integrate_ctx(repo, drive)
    out = _integrate_subagent_patch(ctx, task_id="child4")
    assert "INTEGRATE_PATCH_CORRUPT" in out


def test_mode_allows_protected_write_matrix():
    assert mode_allows_protected_write("pro") is True
    assert mode_allows_protected_write("advanced") is False
    assert mode_allows_protected_write("light") is False


# --------------------------------------------------------------------------- #
# 8. Adversarial round-1 fixes: ext/MCP schema deny-by-default + top-only target
# --------------------------------------------------------------------------- #
def test_acting_schemas_subset_of_acting_set(tmp_path):
    reg, _ctx, _wt = _acting_registry(tmp_path, grants=("mcp_foo",))
    names = {s["function"]["name"] for s in reg.schemas()}
    # No first-party tool outside the acting set; ungranted ext/MCP never leak.
    assert names <= set(ACTING_SUBAGENT_TOOL_NAMES)
    assert reg._acting_tool_grants() == {"mcp_foo"}


def test_integrate_acting_rejects_foreign_target_root(tmp_path):
    from ouroboros.tools.subagent_integration import _integrate_subagent_patch
    live = tmp_path / "live"
    _init_repo(live, {"a.txt": "hi\n"})
    worktree = tmp_path / "wt"
    _init_repo(worktree, {"a.txt": "hi\n"})
    drive = tmp_path / "data"; drive.mkdir()
    _make_child_patch(worktree, drive, "gc1", "a.txt", "hi\nx\n", parent_task_id="acting_parent")
    ctx = ToolContext(
        repo_dir=live, drive_root=drive, workspace_root=str(worktree), workspace_mode="self_worktree",
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree", write_root=str(worktree)),
        task_id="acting_parent",
    )
    out = _integrate_subagent_patch(ctx, task_id="gc1", target_root=str(live))
    assert "INTEGRATE_TARGET_FORBIDDEN" in out
    assert (live / "a.txt").read_text(encoding="utf-8") == "hi\n"  # live repo untouched


def test_acting_no_workspace_blocks_live_repo_write_and_shell(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    drive = tmp_path / "data"; drive.mkdir()
    # Fail-closed: an acting child whose isolated workspace did NOT resolve (no
    # workspace_root) — active_workspace/system_repo would fall back to the live repo.
    ctx = ToolContext(
        repo_dir=repo, drive_root=drive,
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree"),
    )
    reg = ToolRegistry(repo_dir=repo, drive_root=drive)
    reg._ctx = ctx
    out = reg.execute("write_file", {"root": "active_workspace", "path": "x.txt", "content": "y\n"})
    assert "ACTING_NO_WORKSPACE_BLOCKED" in out
    assert not (repo / "x.txt").exists()
    assert "ACTING_NO_WORKSPACE_BLOCKED" in reg.execute("run_command", {"cmd": "echo hi > z.txt"})
    # claude_code_edit is not in the acting tool set -> blocked by the acting hard-block.
    assert "ACTING_SUBAGENT_BLOCKED" in reg.execute("claude_code_edit", {"cwd": ".", "instructions": "x"})
    assert "ACTING_NO_WORKSPACE_BLOCKED" in reg.execute("start_service", {"name": "svc", "cmd": "sleep 1"})


def test_integrate_acting_into_own_worktree_ok(tmp_path):
    from ouroboros.tools.subagent_integration import _integrate_subagent_patch
    live = tmp_path / "live"
    _init_repo(live, {"a.txt": "hi\n"})
    worktree = tmp_path / "wt"
    _init_repo(worktree, {"a.txt": "hi\n"})
    drive = tmp_path / "data"; drive.mkdir()
    _make_child_patch(worktree, drive, "gc2", "a.txt", "hi\ny\n", parent_task_id="acting_parent2")
    ctx = ToolContext(
        repo_dir=live, drive_root=drive, workspace_root=str(worktree), workspace_mode="self_worktree",
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree", write_root=str(worktree)),
        task_id="acting_parent2",
    )
    out = _integrate_subagent_patch(ctx, task_id="gc2")  # no target_root -> own worktree
    assert "Integrated subagent patch" in out
    assert (worktree / "a.txt").read_text(encoding="utf-8") == "hi\ny\n"
    assert (live / "a.txt").read_text(encoding="utf-8") == "hi\n"  # live untouched (top-only)


# --------------------------------------------------------------------------- #
# 9. Triad+scope round-1 fixes: lineage, strict bool, owner toggle plumbing
# --------------------------------------------------------------------------- #
def test_acting_protected_grant_strict_bool():
    # String "false" must NOT grant protected authority (strict parse via normalize).
    c = normalize_task_constraint({"mode": "acting_subagent", "surface": "self_worktree", "protected_paths_grant": "false"})
    assert c.protected_paths_grant is False
    c2 = normalize_task_constraint({"mode": "acting_subagent", "surface": "self_worktree", "protected_paths_grant": "true"})
    assert c2.protected_paths_grant is True


def test_allow_mutative_settings_applied(monkeypatch):
    from ouroboros.config import apply_settings_to_env, get_allow_mutative_subagents
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "true")
    assert get_allow_mutative_subagents() is True
    # A persisted owner setting must take effect (key is in apply_settings_to_env env_keys).
    apply_settings_to_env({"OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS": "false"})
    assert get_allow_mutative_subagents() is False


def test_integrate_lineage_forbidden_for_non_child(tmp_path):
    from ouroboros.tools.subagent_integration import _integrate_subagent_patch
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    drive = tmp_path / "data"; drive.mkdir()
    # Child task result claims a DIFFERENT parent than this ctx.task_id ("parent1").
    _make_child_patch(repo, drive, "gcX", "a.txt", "hi\nq\n", parent_task_id="SOMEONE_ELSE")
    ctx = _integrate_ctx(repo, drive)  # task_id="parent1"
    out = _integrate_subagent_patch(ctx, task_id="gcX")
    assert "INTEGRATE_LINEAGE_FORBIDDEN" in out
    assert (repo / "a.txt").read_text(encoding="utf-8") == "hi\n"  # not applied


# --------------------------------------------------------------------------- #
# 10. Triad+scope round-2/3/4 deep fixes
# --------------------------------------------------------------------------- #
def test_integrate_protected_derived_from_patch_not_manifest(tmp_path):
    # A malicious child cannot hide a protected edit by omitting it from the manifest.
    from ouroboros.tools.subagent_integration import _integrate_subagent_patch
    repo = tmp_path / "repo"
    _init_repo(repo, {"ouroboros/safety.py": "X = 1\n"})
    drive = tmp_path / "data"; drive.mkdir()
    art = _make_child_patch(repo, drive, "evilc", "ouroboros/safety.py", "X = 2\n")
    manifest = json.loads((art / "workspace_patch.json").read_text())
    manifest["tracked_changed"] = ["README.md"]  # lie: hide the protected file
    (art / "workspace_patch.json").write_text(json.dumps(manifest), encoding="utf-8")
    ctx = _integrate_ctx(repo, drive)
    out = _integrate_subagent_patch(ctx, task_id="evilc")
    assert "protected" in out.lower() or "PROTECTED" in out  # patch-derived gate still catches it
    assert (repo / "ouroboros/safety.py").read_text(encoding="utf-8") == "X = 1\n"


def test_integrate_rejects_when_caller_has_no_task_id(tmp_path):
    from ouroboros.tools.subagent_integration import _integrate_subagent_patch
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    drive = tmp_path / "data"; drive.mkdir()
    _make_child_patch(repo, drive, "c5", "a.txt", "hi\nq\n")
    ctx = ToolContext(repo_dir=repo, drive_root=drive)  # no task_id -> cannot verify lineage
    out = _integrate_subagent_patch(ctx, task_id="c5")
    assert "INTEGRATE_LINEAGE_FORBIDDEN" in out


def test_profile_delegated_subagent_with_workspace_meta_fails_closed(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    drive = tmp_path / "data"; drive.mkdir()
    wt = tmp_path / "wt"; wt.mkdir()
    # Delegated subagent + workspace metadata + NO valid constraint -> read-only,
    # never workspace_task (fail-closed floor runs before the workspace branch).
    ctx = ToolContext(
        repo_dir=repo, drive_root=drive, workspace_root=str(wt), workspace_mode="external",
        task_constraint=None, task_metadata={"delegation_role": "subagent"},
    )
    assert active_tool_profile(ctx) == "local_readonly_subagent"


def test_remove_worktree_path_outside_root_guarded(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    data = tmp_path / "data"; data.mkdir()
    wtroot = tmp_path / "wtroot"
    outside = tmp_path / "precious2"; outside.mkdir()
    (outside / "k.txt").write_text("x", encoding="utf-8")
    # remove_worktree(path=<outside the configured root>) must refuse to delete.
    ok = sw.remove_worktree(path=str(outside), worktree_root=wtroot, data_dir=data)
    assert ok is False
    assert outside.exists() and (outside / "k.txt").exists()


# --------------------------------------------------------------------------- #
# 11. Triad+scope round-5 fixes: external_workspace validation + owner-only toggle
# --------------------------------------------------------------------------- #
def test_external_workspace_requires_git_outside_repo(tmp_path, monkeypatch):
    from supervisor.events import _resolve_subagent_constraint
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "true")
    repo = tmp_path / "repo"
    _init_repo(repo, {"a.txt": "hi\n"})
    ctx = SimpleNamespace(REPO_DIR=repo)
    # A non-git external workspace cannot return a workspace.patch -> rejected.
    nogit = tmp_path / "proj"; nogit.mkdir()
    c, wr, wm, detail = _resolve_subagent_constraint(
        ctx, tid="e1",
        requested_constraint={"mode": "acting_subagent", "surface": "external_workspace", "write_root": str(nogit)},
        workspace_root="", workspace_mode="", base_sha="", parent_task_id="p",
    )
    assert c["mode"] == "local_readonly_subagent" and "git working tree" in detail
    # A git working tree outside repo/data is accepted.
    proj = tmp_path / "gitproj"
    _init_repo(proj, {"x.txt": "y\n"})
    c2, wr2, wm2, detail2 = _resolve_subagent_constraint(
        ctx, tid="e2",
        requested_constraint={"mode": "acting_subagent", "surface": "external_workspace", "write_root": str(proj)},
        workspace_root="", workspace_mode="", base_sha="", parent_task_id="p",
    )
    assert detail2 == "" and c2["mode"] == "acting_subagent" and wm2 == "external_workspace" and wr2 == str(proj)


def test_mutative_toggle_self_change_detected():
    from ouroboros.tools.registry import _detect_mutative_toggle_self_change
    assert _detect_mutative_toggle_self_change('echo true >> data/settings.json # ouroboros_allow_mutative_subagents')
    assert _detect_mutative_toggle_self_change('save_settings({"ouroboros_allow_mutative_subagents": "true"})')
    # CLI settings-set path must also be caught.
    assert _detect_mutative_toggle_self_change("ouroboros settings set ouroboros_allow_mutative_subagents true")
    assert not _detect_mutative_toggle_self_change("echo hello world")


def test_pro_acting_shell_write_outside_surface_blocked(tmp_path):
    # Even in pro mode, an acting child's write-like shell targeting outside its
    # isolated surface is blocked (no pro workspace passthrough for acting subagents).
    repo = tmp_path / "repo"; repo.mkdir()
    drive = tmp_path / "data"; drive.mkdir()
    wt = tmp_path / "wt"; wt.mkdir()
    ctx = ToolContext(
        repo_dir=repo, drive_root=drive, workspace_root=str(wt), workspace_mode="self_worktree",
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree", write_root=str(wt)),
    )
    reg = ToolRegistry(repo_dir=repo, drive_root=drive)
    reg._ctx = ctx
    block = reg._run_shell_safety_check({"cmd": "echo x > ../outside.txt"}, "pro")
    assert block and "WORKSPACE_SHELL_BLOCKED" in block


def test_subagent_shell_secret_markers_cover_relative_paths():
    from ouroboros.tools.registry import _subagent_shell_targets_secret
    assert _subagent_shell_targets_secret("cat .env")
    assert _subagent_shell_targets_secret("cat .git/config")
    assert _subagent_shell_targets_secret("cat .git/credentials")
    assert _subagent_shell_targets_secret("cat ~/.ssh/id_rsa")
    assert not _subagent_shell_targets_secret("cat src/main.py")


def test_acting_read_schema_excludes_system_repo(tmp_path):
    reg, _ctx, _wt = _acting_registry(tmp_path)
    schemas = {s["function"]["name"]: s["function"] for s in reg.schemas()}
    rf = schemas.get("read_file")
    if rf:
        root_enum = rf["parameters"]["properties"].get("root", {}).get("enum")
        if isinstance(root_enum, list):
            assert "system_repo" not in root_enum  # matches acting _POLICY (no system_repo)


def test_acting_subagent_cannot_shell_read_secrets(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    drive = tmp_path / "data"; drive.mkdir()
    wt = tmp_path / "wt"; wt.mkdir()
    ctx = ToolContext(
        repo_dir=repo, drive_root=drive, workspace_root=str(wt), workspace_mode="self_worktree",
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree", write_root=str(wt)),
    )
    reg = ToolRegistry(repo_dir=repo, drive_root=drive)
    reg._ctx = ctx
    block = reg._run_shell_safety_check({"cmd": "cat ~/Ouroboros/data/settings.json"}, "pro")
    assert block and "SUBAGENT_SECRET_READ_BLOCKED" in block


def test_integrate_counts_as_reviewable_effect():
    from ouroboros.outcomes import turn_has_reviewable_effects
    trace = {"tool_calls": [{"tool": "integrate_subagent_patch", "status": "ok", "args": {"task_id": "c"}}]}
    assert turn_has_reviewable_effects(trace) is True


def test_readonly_subagent_cannot_spawn_acting_child(tmp_path):
    from ouroboros.tools.control import _schedule_task
    repo = tmp_path / "repo"; repo.mkdir()
    drive = tmp_path / "data"; drive.mkdir()
    ctx = ToolContext(
        repo_dir=repo, drive_root=drive,
        task_constraint=TaskConstraint(mode="local_readonly_subagent"),
    )
    out = _schedule_task(ctx, objective="do X", expected_output="Y", write_surface="self_worktree")
    assert "MUTATIVE_SUBAGENTS_DISABLED" in out


def test_acting_schema_narrows_write_root_and_browser(tmp_path):
    reg, _ctx, _wt = _acting_registry(tmp_path)
    schemas = {s["function"]["name"]: s["function"] for s in reg.schemas()}
    wf = schemas.get("write_file")
    assert wf is not None
    root_enum = wf["parameters"]["properties"].get("root", {}).get("enum")
    if isinstance(root_enum, list):  # acting writes only its isolated surface
        assert root_enum == ["active_workspace"]
    ba = schemas.get("browser_action")
    if ba:
        action_enum = ba["parameters"]["properties"].get("action", {}).get("enum")
        if isinstance(action_enum, list):
            assert "evaluate" not in action_enum


def test_no_workspace_acting_integrate_blocked(tmp_path):
    # An acting child without a resolved workspace must not integrate into the live repo.
    repo = tmp_path / "repo"; repo.mkdir()
    drive = tmp_path / "data"; drive.mkdir()
    ctx = ToolContext(
        repo_dir=repo, drive_root=drive,
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree"),
    )
    reg = ToolRegistry(repo_dir=repo, drive_root=drive)
    reg._ctx = ctx
    assert "ACTING_NO_WORKSPACE_BLOCKED" in reg.execute("integrate_subagent_patch", {"task_id": "x"})


def test_acting_subagent_cannot_read_secrets(tmp_path):
    # Acting children may write their surface but must NOT read owner secrets.
    from ouroboros.tools.core import _data_read
    repo = tmp_path / "repo"; repo.mkdir()
    drive = tmp_path / "data"; drive.mkdir()
    (drive / "settings.json").write_text('{"OPENAI_API_KEY": "sk-secret-xyz"}', encoding="utf-8")
    ctx = ToolContext(
        repo_dir=repo, drive_root=drive,
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree", write_root=str(tmp_path / "wt")),
    )
    out = _data_read(ctx, "settings.json")
    assert "DATA_READ_BLOCKED" in out and "sk-secret-xyz" not in out


def test_acting_subagent_keeps_workspace_access(tmp_path):
    # The strict-readonly resource block must NOT restrict acting children's worktree.
    from ouroboros.tools.core import _local_readonly_resource_block
    repo = tmp_path / "repo"; repo.mkdir()
    drive = tmp_path / "data"; drive.mkdir()
    ctx = ToolContext(
        repo_dir=repo, drive_root=drive,
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree", write_root=str(tmp_path / "wt")),
    )
    assert _local_readonly_resource_block(ctx, "active_workspace", tmp_path / "wt" / "f.txt", tmp_path / "wt", action="write") == ""
