"""WS5 — v6.33.0 review carryover fixes (v6.34.0)."""

from __future__ import annotations



# --- CW1: the P3 scope-review floor is owner-only, not a generic settings write ---

def test_scope_review_floor_is_owner_only_not_generic_settings():
    from ouroboros.gateway.settings import _merge_settings_payload

    current = {"OUROBOROS_SCOPE_REVIEW_FLOOR": "blocking_1m"}
    merged = _merge_settings_payload(current, {"OUROBOROS_SCOPE_REVIEW_FLOOR": "advisory"})
    # The generic /api/settings merge must NOT weaken the blocking >=1M scope gate.
    assert merged["OUROBOROS_SCOPE_REVIEW_FLOOR"] == "blocking_1m"


def test_owner_scope_review_floor_endpoint_validates_and_persists(monkeypatch, tmp_path):
    import asyncio
    import json

    import ouroboros.config as cfg
    from ouroboros.gateway import settings as smod

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    # The endpoint writes os.environ directly; pin it via monkeypatch so the
    # "advisory" it sets is restored after this test (no leak into scope-review tests).
    monkeypatch.setenv("OUROBOROS_SCOPE_REVIEW_FLOOR", "blocking_1m")
    monkeypatch.setattr(smod, "_owner_read_settings_raw", lambda: {})
    written = {}
    monkeypatch.setattr(smod, "_owner_write_settings", lambda s, **k: written.update(s))
    monkeypatch.setattr(smod, "_owner_audit", lambda *a, **k: None)

    class _Req:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    bad = json.loads(asyncio.run(smod.api_owner_scope_review_floor(_Req({"floor": "nope"}))).body)
    assert "must be one of" in (bad.get("error") or "")
    ok = json.loads(asyncio.run(smod.api_owner_scope_review_floor(_Req({"floor": "advisory"}))).body)
    assert ok["ok"] is True and ok["scope_review_floor"] == "advisory"
    assert written["OUROBOROS_SCOPE_REVIEW_FLOOR"] == "advisory"


# --- CW3: an ephemeral decision turn is barred from durable mutators ---

def test_ephemeral_turn_blocks_durable_mutators(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    reg = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    reg.set_context(ToolContext(repo_dir=tmp_path, drive_root=tmp_path, is_ephemeral_turn=True))

    out = reg.execute("update_identity", {"content": "x"})
    assert "EPHEMERAL_TURN_RESTRICTED" in out  # failed closed, not executed

    names = {(s.get("function") or {}).get("name") or s.get("name") for s in reg.schemas()}
    assert "update_identity" not in names and "knowledge_write" not in names
    assert "toggle_evolution" not in names and "set_tool_timeout" not in names
    # The decision/answer/steer tools remain available to the ephemeral turn.
    assert "steer_task" in names and "promote_chat_to_task" in names


def test_non_ephemeral_turn_allows_durable_mutators(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    reg = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    reg.set_context(ToolContext(repo_dir=tmp_path, drive_root=tmp_path, is_ephemeral_turn=False))
    names = {(s.get("function") or {}).get("name") or s.get("name") for s in reg.schemas()}
    assert "update_identity" in names  # a normal turn sees the durable mutators
    out = reg.execute("update_identity", {})
    assert "EPHEMERAL_TURN_RESTRICTED" not in out  # the ephemeral gate did not fire


# --- CW4: the external-shell secret guard catches relative interpreter paths ---

def test_secret_guard_catches_relative_interpreter_path():
    from ouroboros.tools.registry import _subagent_shell_targets_secret

    assert _subagent_shell_targets_secret("python -c \"open('data/settings.json')\"") is True
    assert _subagent_shell_targets_secret("node -e \"readfilesync('../../data/settings.json')\"") is True
    assert _subagent_shell_targets_secret("cat ~/.ssh/id_rsa") is True
    assert _subagent_shell_targets_secret("cat /tmp/notes.txt") is False


# --- CW7: the Max gate route honours USE_LOCAL_MAIN ---

def test_active_main_route_honours_use_local_main():
    from ouroboros.gateway.settings import _active_main_route

    local = _active_main_route({"OUROBOROS_MODEL": "openai/gpt-5.5", "USE_LOCAL_MAIN": True})
    assert local["use_local"] is True and local["provider"] == "local"
    remote = _active_main_route({"OUROBOROS_MODEL": "openai/gpt-5.5", "USE_LOCAL_MAIN": False})
    assert remote["use_local"] is False and remote["provider"] != "local"


# --- CW2: the Max-mode contract is enforced on the task path, fail-closed ---

def test_active_route_confirms_max_reports_unknown_without_evidence(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import ouroboros.capability_evidence as capability_evidence
    import ouroboros.config as cfg
    from ouroboros.gateway import settings as smod

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)  # empty store: no capability evidence
    monkeypatch.setattr(smod, "_owner_read_settings_raw", lambda: {"OUROBOROS_MODEL": "openai/gpt-5.5"})
    # No evidence is UNKNOWN, distinct from positive evidence of a sub-1M route.
    assert smod._active_route_confirms_max() is None
    monkeypatch.setattr(
        capability_evidence,
        "probe",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="confirmed", stale=False, window_tokens=200_000,
        ),
    )
    assert smod._active_route_confirms_max() is False
    monkeypatch.setattr(
        capability_evidence,
        "probe",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="confirmed", stale=False, window_tokens=1_000_000,
        ),
    )
    assert smod._active_route_confirms_max() is True


# --- CW9: the pacing-interval timeout constant lives in the SETTINGS_DEFAULTS SSOT ---

def test_pacing_interval_in_settings_defaults():
    from ouroboros.config import PACING_INTERVAL_DEFAULT_SEC, SETTINGS_DEFAULTS

    assert SETTINGS_DEFAULTS.get("OUROBOROS_PACING_INTERVAL_SEC") == PACING_INTERVAL_DEFAULT_SEC


# === Triad+scope review-fix regressions (v6.34.0) ===

# --- v6.64: known-small routes use Low; unknown routes try Max once ---

def test_maybe_downgrade_max_uses_positive_route_evidence(monkeypatch):
    from types import SimpleNamespace
    from ouroboros import context as contextmod
    from ouroboros import loop as loopmod

    seen = {}

    def _known_small(task, *, allow_fetch=False):
        seen["model"] = task["model"]
        seen["use_local"] = task["use_local_model"]
        return {}, SimpleNamespace(status="confirmed", stale=False, window_tokens=16_384)

    monkeypatch.setattr(contextmod, "_context_fit_route", _known_small)
    # A positively-known small local route selects Low.
    assert loopmod._maybe_downgrade_max_unconfirmed("max", True, "local-model") == "low"
    assert seen["use_local"] is True and seen["model"] == "local-model"
    # Missing remote evidence stays Max until a real overflow proves otherwise.
    monkeypatch.setattr(
        contextmod,
        "_context_fit_route",
        lambda *_a, **_kw: ({}, SimpleNamespace(status="unprobeable", stale=False, window_tokens=0)),
    )
    assert loopmod._maybe_downgrade_max_unconfirmed("max", False, "openai/gpt-5.5") == "max"
    # A non-max mode is returned untouched.
    assert loopmod._maybe_downgrade_max_unconfirmed("low", True, "x") == "low"


def test_maybe_downgrade_max_keeps_max_when_confirmed(monkeypatch):
    from types import SimpleNamespace
    from ouroboros import context as contextmod
    from ouroboros import loop as loopmod

    monkeypatch.setattr(
        contextmod, "_context_fit_route",
        lambda *_a, **_kw: ({}, SimpleNamespace(status="confirmed", stale=False, window_tokens=1_000_000)),
    )
    assert loopmod._maybe_downgrade_max_unconfirmed("max", True, "confirmed-local") == "max"


def test_maybe_downgrade_max_probe_error_preserves_max(monkeypatch):
    from ouroboros import context as contextmod
    from ouroboros import loop as loopmod

    def _boom(*a, **k):
        raise RuntimeError("probe machinery unavailable")

    monkeypatch.setattr(contextmod, "_context_fit_route", _boom)
    # Probe failure is unknown and must not fabricate a Low/200K capability.
    assert loopmod._maybe_downgrade_max_unconfirmed("max", True, "x") == "max"


def test_active_route_confirms_max_local_override_reports_unknown(monkeypatch, tmp_path):
    import ouroboros.config as cfg
    from ouroboros.gateway import settings as smod

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)  # empty store: no evidence on disk
    monkeypatch.setattr(smod, "_owner_read_settings_raw", lambda: {"OUROBOROS_MODEL": "openai/gpt-5.5"})
    # Pinning to a local route with no local-health evidence remains UNKNOWN.
    assert smod._active_route_confirms_max(model="local-x", use_local=True) is None


# --- CW3: the ephemeral deny surface is complete (core envelope + non-core mutators) ---

def test_ephemeral_allowlist_excludes_every_mutator_class():
    from ouroboros.tools.registry import _EPHEMERAL_ALLOWED_TOOLS, _REPO_MUTATION_TOOLS

    # CW3 default-deny: no durable repo/git mutator is in the allowlist...
    assert not (_REPO_MUTATION_TOOLS & _EPHEMERAL_ALLOWED_TOOLS)
    # ...nor any review/skill/publish/control mutator (the whack-a-mole denylist kept
    # missing these), nor run_command (shell is durable-capable).
    for name in ("fetch_pr_ref", "create_integration_branch", "advisory_review", "skill_review",
                 "submit_skill_to_hub", "skill_exec", "toggle_skill", "cancel_task",
                 "task_acceptance_review", "run_command", "switch_model", "update_identity",
                 "commit_reviewed", "toggle_evolution",
                 # subagent-only tools must NOT leak in: spawn / blocking-wait / page-interaction
                 "schedule_subagent", "wait_task", "wait_tasks", "browser_action"):
        assert name not in _EPHEMERAL_ALLOWED_TOOLS
    # ...while the read/inspect + decision tools ARE allowed.
    for name in ("read_file", "query_code", "search_code", "web_search",
                 "route_to_project", "promote_chat_to_task", "steer_task"):
        assert name in _EPHEMERAL_ALLOWED_TOOLS


def test_ephemeral_core_envelope_is_allowlisted_and_mutators_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    from ouroboros.tools.registry import ToolContext, ToolRegistry, _EPHEMERAL_ALLOWED_TOOLS

    reg = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    reg.set_context(ToolContext(repo_dir=tmp_path, drive_root=tmp_path, is_ephemeral_turn=True))

    # The CORE/initial envelope is allowlisted too (every visible tool is allowed).
    core_names = {(s.get("function") or {}).get("name") or s.get("name") for s in reg.schemas(core_only=True)}
    assert core_names <= _EPHEMERAL_ALLOWED_TOOLS

    # A non-allowlisted mutator fails closed at execute() up front (so enabling it via
    # enable_tools cannot bypass the gate), and get_schema_by_name won't surface it.
    assert "EPHEMERAL_TURN_RESTRICTED" in reg.execute("fetch_pr_ref", {})
    assert "EPHEMERAL_TURN_RESTRICTED" in reg.execute("advisory_review", {})
    assert reg.get_schema_by_name("skill_review") is None  # enable_tools can't surface it


# --- CW1: the scope-review-floor self-lowering shell detector ---

def test_scope_review_floor_self_lowering_detector():
    from ouroboros.tools.registry import _detect_scope_review_floor_self_lowering as det

    assert det("curl -x post http://127.0.0.1:8765/api/owner/scope-review-floor") is True
    assert det("save_settings({'ouroboros_scope_review_floor': 'advisory'})") is True
    assert det("ouroboros settings scope-review-floor advisory") is True
    assert det("ouroboros.cli settings scope-review-floor advisory") is True
    # Benign mentions (reading docs/logs) must not trip the guard.
    assert det("grep scope_review_floor data/logs/events.jsonl") is False
    assert det("echo reading about the scope review floor") is False


# --- CW2 (round-4): switch_model refuses a sub-1M route while the transcript is max-sized ---

def test_switch_model_blocks_sub1m_route_while_max(monkeypatch, tmp_path):
    from ouroboros.gateway import settings as smod
    from ouroboros.tools import control
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setattr("ouroboros.llm.LLMClient.available_models", lambda self: ["big-model", "small-model"])
    monkeypatch.setattr(smod, "_active_route_confirms_max",
                        lambda settings=None, *, model="", use_local=None, allow_fetch=False: model == "big-model")

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.active_context_mode = "max"
    # sub-1M route while the transcript is max-sized -> blocked, override NOT applied
    out = control._switch_model(ctx, model="small-model")
    assert "SWITCH_BLOCKED" in out
    assert ctx.active_model_override is None
    # a >=1M route -> allowed
    out2 = control._switch_model(ctx, model="big-model")
    assert "SWITCH_BLOCKED" not in out2 and ctx.active_model_override == "big-model"
    # not max (low transcript) -> a sub-1M switch is fine
    ctx_low = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx_low.active_context_mode = "low"
    out3 = control._switch_model(ctx_low, model="small-model")
    assert "SWITCH_BLOCKED" not in out3 and ctx_low.active_model_override == "small-model"


def test_switch_model_allows_unknown_route_for_one_max_attempt(monkeypatch, tmp_path):
    from ouroboros.gateway import settings as smod
    from ouroboros.tools import control
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setattr("ouroboros.llm.LLMClient.available_models", lambda self: ["unknown-model"])
    monkeypatch.setattr(smod, "_active_route_confirms_max", lambda **_kwargs: None)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.active_context_mode = "max"

    out = control._switch_model(ctx, model="unknown-model")

    assert "SWITCH_BLOCKED" not in out
    assert ctx.active_model_override == "unknown-model"


def test_switch_model_fails_closed_when_capability_check_errors(monkeypatch, tmp_path):
    from ouroboros.gateway import settings as smod
    from ouroboros.tools import control
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setattr("ouroboros.llm.LLMClient.available_models", lambda self: ["small-model"])

    def _boom(*a, **k):
        raise RuntimeError("probe down")

    monkeypatch.setattr(smod, "_active_route_confirms_max", _boom)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.active_context_mode = "max"
    # An errored capability check must FAIL CLOSED (block), not apply the override.
    out = control._switch_model(ctx, model="small-model")
    assert "SWITCH_BLOCKED" in out
    assert ctx.active_model_override is None


# --- CW3 (claudexor): the ephemeral turn is barred from extension/MCP tools too ---

def test_ephemeral_blocks_extension_and_mcp_tools(tmp_path):
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    reg = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    reg.set_context(ToolContext(repo_dir=tmp_path, drive_root=tmp_path, is_ephemeral_turn=True))
    # an extension tool (resolved ext_tool) and an MCP tool both fail closed at execute()
    assert "EPHEMERAL_TURN_RESTRICTED" in reg._ephemeral_block("skill__do", ext_tool={"name": "skill__do"})
    assert "EPHEMERAL_TURN_RESTRICTED" in reg._ephemeral_block("mcp__srv__x", is_mcp=True)
    # a normal turn does not block external tools
    reg.set_context(ToolContext(repo_dir=tmp_path, drive_root=tmp_path, is_ephemeral_turn=False))
    assert reg._ephemeral_block("skill__do", ext_tool={"name": "skill__do"}) == ""


def test_ephemeral_schemas_omit_extension_and_mcp_surfaces(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    reg = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    reg.set_context(ToolContext(repo_dir=tmp_path, drive_root=tmp_path, is_ephemeral_turn=True))
    reg.schemas()  # populates capability_omissions
    omissions = {(o.get("surface"), o.get("reason")) for o in reg.capability_omissions()}
    assert ("extensions", "ephemeral_turn") in omissions
    assert ("mcp", "ephemeral_turn") in omissions


# --- running_tasks routing context never silently truncates (codex no-[:N] rule) ---

def test_running_tasks_clip_marker_is_explicit():
    import server

    assert server._clip_marked("short objective", 600) == "short objective"
    clipped = server._clip_marked("x" * 1000, 600)
    assert clipped.startswith("x" * 600)
    assert "chars omitted]" in clipped  # explicit omission marker, not a silent cut
