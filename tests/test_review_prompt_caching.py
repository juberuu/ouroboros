"""v6.69.0 review-economics tests: cache-friendly review prompts, TTL passthrough,
reviewer session affinity, durable parameter rejections, pre-routing $0 settlement,
and review-wave budget admission."""

from __future__ import annotations

import pathlib

import pytest

from ouroboros.llm import LLMClient, supports_message_cache_control
from ouroboros.tools.review_helpers import REVIEW_CACHE_TTL, cached_prompt_blocks


# ---------------------------------------------------------------------------
# cache_control TTL passthrough (llm._copy_messages_with_cache_policy)
# ---------------------------------------------------------------------------

def _policy(messages, allow=True, allow_ttl=False):
    return LLMClient._copy_messages_with_cache_policy(
        messages, allow_message_cache_control=allow, flatten_tool_content_blocks=False,
        allow_cache_ttl=allow_ttl,
    )


def test_cache_policy_preserves_valid_ttl():
    msgs = [{
        "role": "system",
        "content": [{"type": "text", "text": "stable", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
    }]
    out = _policy(msgs, allow_ttl=True)
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # Default stance: ttl is stripped unless the route explicitly allows it.
    default = _policy(msgs)
    assert default[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_cache_policy_drops_invalid_ttl_and_extra_fields():
    msgs = [{
        "role": "system",
        "content": [{"type": "text", "text": "stable", "cache_control": {"type": "x", "ttl": "7d", "junk": 1}}],
    }]
    out = _policy(msgs, allow_ttl=True)
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_cache_policy_still_strips_marker_when_not_allowed():
    msgs = [{
        "role": "system",
        "content": [{"type": "text", "text": "stable", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
    }]
    out = _policy(msgs, allow=False)
    assert "cache_control" not in out[0]["content"][0]


def test_cache_policy_empty_text_block_never_carries_marker():
    msgs = [{
        "role": "system",
        "content": [{"type": "text", "text": "  ", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
    }]
    out = _policy(msgs)
    assert "cache_control" not in out[0]["content"][0]


def test_prompt_cache_ttl_reports_extended_tier():
    messages = [{
        "role": "system",
        "content": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
    }]
    assert LLMClient._prompt_cache_ttl_from_payload(messages) == "1h"
    plain = [{
        "role": "system",
        "content": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
    }]
    assert LLMClient._prompt_cache_ttl_from_payload(plain) == "default"
    assert LLMClient._prompt_cache_ttl_from_payload([{"role": "user", "content": "x"}]) is None


# ---------------------------------------------------------------------------
# Explicit reviewer session affinity
# ---------------------------------------------------------------------------

def test_explicit_cache_affinity_stable_and_model_scoped():
    a1 = LLMClient._explicit_cache_affinity_identity("anthropic/claude-fable-5", "scope_review:task1")
    a2 = LLMClient._explicit_cache_affinity_identity("anthropic/claude-fable-5", "scope_review:task1")
    b = LLMClient._explicit_cache_affinity_identity("openai/gpt-5.6-sol", "scope_review:task1")
    c = LLMClient._explicit_cache_affinity_identity("anthropic/claude-fable-5", "scope_review:task2")
    assert a1 == a2
    assert a1 != b
    assert a1 != c
    assert a1.startswith("ouroboros-session-")
    assert LLMClient._explicit_cache_affinity_identity("m", "") == ""


def test_build_remote_kwargs_prefers_explicit_affinity():
    client = LLMClient(api_key="test")
    target = {
        "provider": "openrouter",
        "resolved_model": "anthropic/claude-fable-5",
        "usage_model": "anthropic/claude-fable-5",
        "supports_openrouter_extensions": True,
    }
    messages = [
        {"role": "system", "content": "stable governance"},
        {"role": "user", "content": "dynamic evidence round 1"},
    ]
    k1 = client._build_remote_kwargs(
        target, messages, "high", 1024, "auto", None, None,
        skip_capability_fetch=True, cache_affinity="scope_review:taskX",
    )
    messages2 = [
        {"role": "system", "content": "stable governance"},
        {"role": "user", "content": "dynamic evidence round 2 (changed)"},
    ]
    k2 = client._build_remote_kwargs(
        target, messages2, "high", 1024, "auto", None, None,
        skip_capability_fetch=True, cache_affinity="scope_review:taskX",
    )
    s1 = k1["extra_body"]["session_id"]
    s2 = k2["extra_body"]["session_id"]
    assert s1 == s2  # affinity is round-stable despite changed user content
    k3 = client._build_remote_kwargs(
        target, messages2, "high", 1024, "auto", None, None,
        skip_capability_fetch=True,
    )
    assert k3["extra_body"]["session_id"] != s1  # default derives from messages


# ---------------------------------------------------------------------------
# cached_prompt_blocks helper
# ---------------------------------------------------------------------------

def test_cached_prompt_blocks_structure():
    blocks = cached_prompt_blocks("STABLE", "DYNAMIC")
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": REVIEW_CACHE_TTL}
    assert blocks[0]["text"] == "STABLE"
    assert blocks[1]["text"] == "DYNAMIC"
    assert "cache_control" not in blocks[1]
    only = cached_prompt_blocks("STABLE")
    assert len(only) == 1


def test_reviewer_models_support_cache_markers_where_expected():
    assert supports_message_cache_control("anthropic/claude-fable-5")
    assert supports_message_cache_control("google/gemini-3.5-flash")
    assert not supports_message_cache_control("openai/gpt-5.6-sol")


# ---------------------------------------------------------------------------
# Stable-first prompt structure per surface
# ---------------------------------------------------------------------------

def test_triad_template_stable_part_has_no_dynamic_fields():
    from ouroboros.tools import review as review_mod

    stable = review_mod._REVIEW_PROMPT_TEMPLATE_STABLE
    for dynamic_field in ("{goal_section}", "{scope_section}", "{diff_text}",
                          "{current_files_section}", "{review_history_section}"):
        assert dynamic_field not in stable
    dynamic = review_mod._REVIEW_PROMPT_TEMPLATE_DYNAMIC
    for stable_field in ("{checklist_section}", "{dev_guide_text}", "{architecture_section}"):
        assert stable_field not in dynamic


def test_skill_review_prompt_stable_prefix_is_payload_independent(tmp_path):
    from ouroboros import skill_review

    p1, n1 = skill_review._build_review_prompt(
        "demo", tmp_path / "demo", "{\"a\": 1}", "hash-one", "plugin.py\nprint('one')",
    )
    p2, n2 = skill_review._build_review_prompt(
        "other", tmp_path / "other", "{\"b\": 2}", "hash-two", "plugin.py\nprint('two')",
    )
    assert n1 == n2
    assert p1[:n1] == p2[:n2]  # governance prefix is byte-identical
    assert "## Skill identity" not in p1[:n1]  # per-skill identity is dynamic-tail only
    assert "hash-one" not in p1[:n1]
    # Output contract (the anti-injection boundary) stays after the payload.
    assert p1.rindex("## Output contract") > p1.index("## Skill files")


def test_acceptance_request_messages_are_cache_blocked():
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot, _request_messages

    req = ReviewRequest(surface="task_acceptance", goal="check", evidence={"k": "v"},
                        policy={"classify_outcome_tier": True}, task_id="t1")
    slot = ReviewSlot(slot_id="slot_1", model="m")
    messages = _request_messages(req, slot)
    assert messages[0]["role"] == "system"
    blocks = messages[0]["content"]
    assert isinstance(blocks, list)
    assert blocks[0]["cache_control"]["ttl"] == REVIEW_CACHE_TTL
    assert messages[1]["role"] == "user"
    assert '"k": "v"' in messages[1]["content"]
    # Explicit request.messages keep full authority (no rewriting).
    explicit = ReviewRequest(surface="x", goal="g", messages=[{"role": "user", "content": "raw"}])
    assert _request_messages(explicit, slot) == [{"role": "user", "content": "raw"}]


def test_scope_prompt_records_stable_boundary(tmp_path, monkeypatch):
    from ouroboros.tools import scope_review as sr

    # The boundary contextvar is set by _assemble_prompt inside
    # _build_scope_prompt; validate via the recorded value on a tiny repo.
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f.py"], check=True)
    prompt, status = sr._build_scope_prompt(tmp_path, "test msg", drive_root=tmp_path)
    assert prompt is not None and status is None
    n = sr._SCOPE_STABLE_PREFIX_LEN.get()
    assert 0 < n < len(prompt)
    stable = prompt[:n]
    assert "Canonical Documentation Context" in stable
    assert "## Staged diff" not in stable
    assert "## Staged diff" in prompt[n:]


# ---------------------------------------------------------------------------
# Warm capability cache under skip_capability_fetch
# ---------------------------------------------------------------------------

def test_warm_supported_params_cache_used_when_fetch_skipped(monkeypatch):
    client = LLMClient(api_key="test")
    monkeypatch.setattr(LLMClient, "_SUPPORTED_PARAMS_FETCHED", True)
    monkeypatch.setattr(
        LLMClient, "_SUPPORTED_PARAMS_CACHE",
        {"anthropic/claude-fable-5": {"max_tokens", "tools"}},  # no temperature
    )
    target = {
        "provider": "openrouter",
        "resolved_model": "anthropic/claude-fable-5",
        "usage_model": "anthropic/claude-fable-5",
        "supports_openrouter_extensions": True,
    }
    kwargs = client._build_remote_kwargs(
        target, [{"role": "user", "content": "x"}], "high", 512, "auto", 0.2, None,
        skip_capability_fetch=True,
    )
    assert "temperature" not in kwargs  # proactively stripped from the warm cache


# ---------------------------------------------------------------------------
# Durable rejected-parameter evidence
# ---------------------------------------------------------------------------

def test_rejected_params_survive_process_boundary(tmp_path, monkeypatch):
    from ouroboros import capability_evidence as ce

    ce.record_rejected_params(tmp_path, "anthropic/claude-fable-5", {"temperature"})
    assert ce.get_rejected_params(tmp_path, "anthropic/claude-fable-5") == {"temperature"}

    # Fresh "process": empty in-memory caches, durable store is consulted once.
    monkeypatch.setattr(LLMClient, "_REJECTED_PARAMS_CACHE", {})
    monkeypatch.setattr(LLMClient, "_REJECTED_PARAMS_LOADED", {})
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    known = LLMClient._known_rejected_params("anthropic/claude-fable-5")
    assert "temperature" in known


def test_rejected_params_expiry_heals_long_running_process(tmp_path, monkeypatch):
    """A process older than the reload interval re-syncs from the durable store,
    so a durable expiry evicts the parameter WITHOUT a restart."""
    from ouroboros import capability_evidence as ce

    ce.record_rejected_params(tmp_path, "anthropic/claude-fable-5", {"temperature"})
    monkeypatch.setattr(LLMClient, "_REJECTED_PARAMS_CACHE", {})
    monkeypatch.setattr(LLMClient, "_REJECTED_PARAMS_LOADED", {})
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    assert "temperature" in LLMClient._known_rejected_params("anthropic/claude-fable-5")

    # Expire the durable entry, then age the process cache past the reload TTL.
    data = ce._load(tmp_path)
    data["rejected_params"]["anthropic/claude-fable-5"]["observed_at"] = "2020-01-01T00:00:00+00:00"
    ce._save(tmp_path, data)
    LLMClient._REJECTED_PARAMS_LOADED["anthropic/claude-fable-5"] -= (
        LLMClient._REJECTED_PARAMS_RELOAD_SEC + 1
    )
    assert "temperature" not in LLMClient._known_rejected_params("anthropic/claude-fable-5")


def test_rejected_params_expire(tmp_path):
    from ouroboros import capability_evidence as ce

    ce.record_rejected_params(tmp_path, "m/x", {"temperature"})
    data = ce._load(tmp_path)
    data["rejected_params"]["m/x"]["observed_at"] = "2020-01-01T00:00:00+00:00"
    ce._save(tmp_path, data)
    assert ce.get_rejected_params(tmp_path, "m/x") == set()


# ---------------------------------------------------------------------------
# Pre-routing rejection settles $0
# ---------------------------------------------------------------------------

class _RouterRejection(Exception):
    def __init__(self):
        super().__init__(
            "Error code: 404 - {'error': {'message': 'No endpoints found that "
            "can handle the requested parameters: temperature', 'code': 404}}"
        )
        self.status_code = 404


def test_is_pre_routing_rejection_classification():
    from ouroboros.usage_accounting import _is_pre_routing_rejection

    assert _is_pre_routing_rejection(_RouterRejection())
    assert not _is_pre_routing_rejection(Exception("520 Provider returned error"))
    assert not _is_pre_routing_rejection(Exception("No endpoints found"))  # no 404 evidence
    plain_404 = Exception("Error code: 404 - {'error': {'message': 'model not found'}}")
    assert not _is_pre_routing_rejection(plain_404)  # 404 without the router signature


def test_pre_routing_rejection_releases_reservation(tmp_path, monkeypatch):
    from ouroboros import usage_accounting as ua

    request = ua.AttemptRequest(
        model="anthropic/claude-fable-5", provider="openrouter",
        prompt_tokens_estimate=1000, max_completion_tokens=100,
        reservation_usd=5.0, drive_root=tmp_path,
        task_id="t", root_task_id="t", global_limit_usd=100.0,
    )
    with pytest.raises(_RouterRejection):
        ua.execute_physical_attempt(request, lambda: (_ for _ in ()).throw(_RouterRejection()))
    projection = ua.usage_projection(tmp_path)
    assert projection["unresolved_upper_bound_usd"] == 0.0
    assert projection["settled_usd"] == 0.0
    assert projection["attempt_counts"].get("settled") == 1

    # A generic provider failure keeps its unresolved upper bound.
    with pytest.raises(RuntimeError):
        ua.execute_physical_attempt(request, lambda: (_ for _ in ()).throw(RuntimeError("520 boom")))
    projection = ua.usage_projection(tmp_path)
    assert projection["unresolved_upper_bound_usd"] == 5.0


# ---------------------------------------------------------------------------
# Review-wave budget admission
# ---------------------------------------------------------------------------

def test_review_wave_admission_fail_open_paths(tmp_path):
    from ouroboros.usage_accounting import review_wave_admission

    assert review_wave_admission(tmp_path, root_task_id="", models=["m"], prompt_chars=10)["fits"]
    assert review_wave_admission(tmp_path, root_task_id="r", models=[], prompt_chars=10)["fits"]
    # Root with no ledger rows → no known limit → fail-open.
    assert review_wave_admission(tmp_path, root_task_id="ghost", models=["m"], prompt_chars=10)["fits"]


def test_review_wave_admission_blocks_known_overrun(tmp_path, monkeypatch):
    from ouroboros import pricing as pricing_mod
    from ouroboros import usage_accounting as ua

    # Deterministic catalog: no live pricing fetch in the default test lane.
    class _P(tuple):
        tiers = ()

    monkeypatch.setattr(
        pricing_mod, "get_pricing",
        lambda **k: {"anthropic/claude-fable-5": _P((10.0, 1.0, 12.5, 50.0))},
    )

    # Seed the ledger with a settled row carrying a root limit.
    request = ua.AttemptRequest(
        model="anthropic/claude-fable-5", provider="openrouter",
        reservation_usd=4.0, drive_root=tmp_path,
        task_id="root1", root_task_id="root1", root_limit_usd=5.0,
        global_limit_usd=100.0,
    )
    reservation = ua.reserve_attempt(request)
    ua.mark_dispatched(reservation)
    ua.settle_attempt(reservation, {}, cost_usd=4.0, cost_final=True)

    admission = ua.review_wave_admission(
        tmp_path, root_task_id="root1",
        models=["anthropic/claude-fable-5"] * 3,
        prompt_chars=4_000_000,  # ~1M tokens per slot — cannot fit $1 remaining
    )
    assert admission["estimated_wave_usd"] is not None
    assert admission["remaining_usd"] == pytest.approx(1.0)
    assert not admission["fits"]


# ---------------------------------------------------------------------------
# llm_usage lineage attribution
# ---------------------------------------------------------------------------

def test_emit_review_usage_carries_scope_lineage():
    from ouroboros.tools.review_helpers import emit_review_usage
    from ouroboros.usage_accounting import UsageScope, usage_scope

    events = []

    class _Ctx:
        task_id = "child1"
        event_queue = None
        pending_events = events

    scope = UsageScope(task_id="child1", root_task_id="root9", parent_task_id="parent5")
    with usage_scope(scope):
        emit_review_usage(_Ctx(), model="anthropic/claude-fable-5",
                          usage={"prompt_tokens": 10}, source="test")
    assert events and events[0]["root_task_id"] == "root9"
    assert events[0]["parent_task_id"] == "parent5"


def test_supervisor_backfills_lineage_from_running(monkeypatch):
    from supervisor import events as sup_events

    captured = {}
    monkeypatch.setattr(sup_events, "append_jsonl", lambda path, row: captured.update(row))

    class _Ctx:
        RUNNING = {
            "t1": {
                "task": {
                    "id": "t1", "root_task_id": "rootX", "parent_task_id": "pX",
                    "delegation_role": "subagent", "effective_model_lane": "light",
                },
            }
        }
        DRIVE_ROOT = pathlib.Path("/tmp")

        @staticmethod
        def update_budget_from_usage(usage):
            return None

    evt = {"type": "llm_usage", "task_id": "t1", "usage": {"prompt_tokens": 1}}
    sup_events._handle_llm_usage(evt, _Ctx())
    assert captured.get("root_task_id") == "rootX"
    assert captured.get("parent_task_id") == "pX"
    assert captured.get("delegation_role") == "subagent"
    assert captured.get("effective_model_lane") == "light"


# ---------------------------------------------------------------------------
# Round-2 adversarial-review fixes (v6.69.0)
# ---------------------------------------------------------------------------

def test_cache_ttl_is_anthropic_route_only():
    """ttl passthrough is gated per route: anthropic keeps a valid ttl, every
    other message-cache route (gemini) collapses to the bare marker — an
    undocumented field on the Gemini route risks a hard 400 on every call."""
    msgs = [{
        "role": "system",
        "content": [{"type": "text", "text": "stable", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
    }]
    kept = LLMClient._copy_messages_with_cache_policy(
        msgs, allow_message_cache_control=True, flatten_tool_content_blocks=False,
        allow_cache_ttl=True,
    )
    assert kept[0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    stripped = LLMClient._copy_messages_with_cache_policy(
        msgs, allow_message_cache_control=True, flatten_tool_content_blocks=False,
    )
    assert stripped[0]["content"][0]["cache_control"] == {"type": "ephemeral"}

    client = LLMClient(api_key="test")
    for model, expect_ttl in (("anthropic/claude-fable-5", True), ("google/gemini-3.5-flash", False)):
        kwargs = client._build_remote_kwargs(
            {"provider": "openrouter", "resolved_model": model, "usage_model": model,
             "supports_openrouter_extensions": True},
            msgs, "high", 512, "auto", None, None, skip_capability_fetch=True,
        )
        cc = kwargs["messages"][0]["content"][0]["cache_control"]
        assert ("ttl" in cc) is expect_ttl, (model, cc)


def test_direct_anthropic_blocks_preserve_valid_ttl():
    client = LLMClient(api_key="test")
    blocks = client._anthropic_blocks_from_content([
        {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": "junk-ttl", "cache_control": {"type": "ephemeral", "ttl": "7d"}},
    ])
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}


def test_plan_user_content_stable_prefix_is_plan_independent():
    from ouroboros.tools.review_synthesis import build_plan_review_user_content

    class _Req:
        goal = "improve planning"
        files_to_touch = ["a.py"]
        context_level = "localized"
        context_notes = ""
        include_tests = False
        scope = {}

    r1, r2 = _Req(), _Req()
    r1.plan = "PLAN VERSION ONE"
    r2.plan = "PLAN VERSION TWO (revised)"
    c1, n1 = build_plan_review_user_content(r1, "HEAD SNAPSHOTS", "ATLAS PACK", "")
    c2, n2 = build_plan_review_user_content(r2, "HEAD SNAPSHOTS", "ATLAS PACK", "")
    assert n1 == n2
    assert c1[:n1] == c2[:n2]  # evidence prefix survives a plan revision
    assert "PLAN VERSION ONE" not in c1[:n1]  # plan text is dynamic-tail only
    assert "ATLAS PACK" in c1[:n1]


def test_plan_atlas_substitution_ignores_placeholder_quoted_in_plan():
    """The Atlas slot is substituted INSIDE the stable prefix even when the
    dynamic plan text quotes the placeholder literal (a plan touching
    plan_review.py routinely does)."""
    from ouroboros.tools.review_synthesis import build_plan_review_user_content

    placeholder = "__GENERATED_PLAN_ATLAS_PENDING__"

    class _Req:
        goal = "g"
        plan = f"quote the literal {placeholder} in prose"
        files_to_touch = []
        context_level = "localized"
        context_notes = ""
        include_tests = False
        scope = {}

    content, stable_len = build_plan_review_user_content(_Req(), "", placeholder, "")
    slot = content.rfind(placeholder, 0, stable_len)
    assert 0 <= slot < stable_len  # the REAL slot is inside the stable prefix
    substituted = content[:slot] + "ATLAS TEXT" + content[slot + len(placeholder):]
    tail = substituted[slot + len("ATLAS TEXT"):]
    assert placeholder in tail  # the quoted literal in the plan is untouched


def test_plan_atlas_substitution_ignores_placeholder_in_head_snapshot():
    """A HEAD snapshot of a file that CONTAINS the placeholder literal sits in
    the stable prefix BEFORE the Atlas slot; the substitution must target the
    LAST in-boundary occurrence (the real slot is the final stable section)."""
    from ouroboros.tools.review_synthesis import build_plan_review_user_content

    placeholder = "__GENERATED_PLAN_ATLAS_PENDING__"

    class _Req:
        goal = "g"
        plan = "ordinary plan"
        files_to_touch = ["ouroboros/tools/plan_review.py"]
        context_level = "localized"
        context_notes = ""
        include_tests = False
        scope = {}

    snapshot = f"### plan_review.py\nplaceholder = \"{placeholder}\"\n"
    content, stable_len = build_plan_review_user_content(_Req(), snapshot, placeholder, "")
    assert content.count(placeholder) == 2
    slot = content.rfind(placeholder, 0, stable_len)
    first = content.find(placeholder, 0, stable_len)
    assert first < slot < stable_len  # the LAST in-boundary occurrence is the slot
    substituted = content[:slot] + "ATLAS TEXT" + content[slot + len(placeholder):]
    assert "ATLAS TEXT" in substituted[:slot + len("ATLAS TEXT")]
    assert placeholder in substituted[:slot]  # the snapshot literal is untouched


def test_plan_review_messages_builder_blocks():
    from ouroboros.tools.review_synthesis import build_plan_review_messages

    msgs = build_plan_review_messages("SYSTEM", "STABLEDYNAMIC", 6)
    assert msgs[0]["content"][0]["cache_control"]["ttl"] == REVIEW_CACHE_TTL
    user_blocks = msgs[1]["content"]
    assert user_blocks[0]["text"] == "STABLE" and "cache_control" in user_blocks[0]
    assert user_blocks[1]["text"] == "DYNAMIC" and "cache_control" not in user_blocks[1]
    flat = build_plan_review_messages("SYSTEM", "ALLDYNAMIC", 0)
    assert flat[1]["content"] == "ALLDYNAMIC"


def test_extended_ttl_scales_cache_write_estimate(monkeypatch):
    from ouroboros import pricing as pricing_mod

    class _P(tuple):
        tiers = ()

    table = {"anthropic/claude-fable-5": _P((10.0, 1.0, 12.5, 50.0))}
    monkeypatch.setattr(pricing_mod, "get_pricing", lambda **k: table)
    base = pricing_mod.estimate_cost_optional(
        "anthropic/claude-fable-5", 1_000_000, 0, cache_write_tokens=1_000_000,
        prompt_cache_ttl=None, allow_live_fetch=False,
    )
    extended = pricing_mod.estimate_cost_optional(
        "anthropic/claude-fable-5", 1_000_000, 0, cache_write_tokens=1_000_000,
        prompt_cache_ttl="1h", allow_live_fetch=False,
    )
    assert base == pytest.approx(12.5)
    assert extended == pytest.approx(12.5 * 2.0 / 1.25)


def test_supervisor_handles_review_wave_budget_event(monkeypatch):
    from supervisor import events as sup_events

    assert "review_wave_budget_insufficient" in sup_events.EVENT_HANDLERS
    captured = {}
    monkeypatch.setattr(sup_events, "append_jsonl", lambda path, row: captured.update(row))

    class _Ctx:
        DRIVE_ROOT = pathlib.Path("/tmp")

    sup_events.EVENT_HANDLERS["review_wave_budget_insufficient"](
        {"type": "review_wave_budget_insufficient", "surface": "skill_review",
         "estimated_wave_usd": 30.0}, _Ctx(),
    )
    assert captured.get("type") == "review_wave_budget_insufficient"
    assert captured.get("surface") == "skill_review"


def test_scope_review_usage_flows_through_substrate_once():
    """Behavioral pin for the v6.69.0 dedup: one scope call → exactly one
    llm_usage event, emitted by the review substrate per-slot path (the former
    job-level re-emit in run_scope_review is gone)."""
    from ouroboros.tools.scope_review import _call_scope_llm

    events = []

    class _Ctx:
        task_id = "scope-task"
        event_queue = None
        pending_events = events
        drive_root = "/tmp"

    class _StubLLM:
        def chat(self, **kwargs):
            return (
                {"content": '[{"item": "intent_alignment", "verdict": "PASS", "reason": "ok"}]'},
                {"prompt_tokens": 10, "completion_tokens": 2, "ledger_attempt_ids": ["a1"]},
            )

    import ouroboros.review_substrate as rs
    original = rs.ReviewCoordinator.__init__

    def _patched(self, *, llm=None, drive_root=None, usage_ctx=None):
        original(self, llm=_StubLLM(), drive_root=drive_root, usage_ctx=usage_ctx)

    rs.ReviewCoordinator.__init__ = _patched
    try:
        raw, usage, err = _call_scope_llm("scope prompt", scope_model="anthropic/claude-fable-5", ctx=_Ctx())
    finally:
        rs.ReviewCoordinator.__init__ = original
    assert err == "" and raw
    usage_events = [e for e in events if e.get("type") == "llm_usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["source"] == "review_substrate:scope_review"
    assert usage_events[0]["ledger_attempt_ids"] == ["a1"]


def test_acceptance_panel_declines_wave_on_insufficient_budget(monkeypatch, tmp_path):
    """The acceptance admission decline returns a terminal DEGRADED without a
    single reviewer call (loop-side wiring of the shared budget gate)."""
    from types import SimpleNamespace
    import ouroboros.loop as loop_mod
    import ouroboros.review_substrate as rs
    from ouroboros.tools import review_helpers

    calls = {"panel": 0}
    monkeypatch.setattr(rs, "reviewer_slots", lambda **k: [SimpleNamespace(model="m1"), SimpleNamespace(model="m2")])
    def _boom(*a, **k):
        calls["panel"] += 1
        raise AssertionError("reviewer must not be called")
    monkeypatch.setattr(rs, "run_review_request", _boom)
    monkeypatch.setattr(
        review_helpers, "review_wave_budget_gate",
        lambda ctx, **k: {"fits": False, "estimated_wave_usd": 30.0, "remaining_usd": 1.0, "limit_usd": 50.0, "slots": 2},
    )
    tools = SimpleNamespace(_ctx=SimpleNamespace(task_id="t", drive_root=str(tmp_path), pending_events=[]))
    ctx = loop_mod._TaskAcceptanceContext(
        tools=tools, content="done", task_id="t", task_type="task",
        llm_trace={"tool_calls": []}, drive_root=tmp_path,
        messages=[{"role": "system", "content": ""}, {"role": "user", "content": "goal"}],
        emit_progress=lambda _m: None, mode="required", subtree_statuses=[],
        budget_profile=None, passes_done=0, evidence={"k": "v"},
    )
    result = loop_mod._execute_task_acceptance_panel(ctx)
    assert calls["panel"] == 0
    assert result.aggregate_signal == "DEGRADED" and result.degraded
    assert any("review_wave_budget_insufficient" in r for r in result.degraded_reasons)


def test_pre_routing_zero_settlement_requires_openrouter_provider(tmp_path):
    """A direct-provider 404 with the router signature stays unresolved: the
    confirmed-$0 class is gated on provider == openrouter."""
    from ouroboros import usage_accounting as ua

    request = ua.AttemptRequest(
        model="openai::gpt-5.5", provider="openai",
        reservation_usd=3.0, drive_root=tmp_path,
        task_id="t2", root_task_id="t2", global_limit_usd=100.0,
    )
    with pytest.raises(_RouterRejection):
        ua.execute_physical_attempt(request, lambda: (_ for _ in ()).throw(_RouterRejection()))
    projection = ua.usage_projection(tmp_path)
    assert projection["unresolved_upper_bound_usd"] == 3.0
