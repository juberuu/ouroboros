"""Phase 5 (v6.39) C1: skill owner-attestation — owner skips the EXPENSIVE LLM review for
their OWN skill, but the deterministic preflight floor still gates it and the verdict is
valid only while the owner-issued marker is present."""

from __future__ import annotations

import json

from ouroboros.contracts.skill_payload_policy import (
    SKILL_OWNER_STATE_FILENAMES,
    SKILL_OWNER_STATE_STEMS,
    is_skill_owner_state_target,
)


def test_owner_attestation_marker_is_owner_state():
    # The marker is an OWNER-STATE file: the agent can never forge it (that would
    # self-bypass the immune system's review).
    assert "owner_attestation.json" in SKILL_OWNER_STATE_FILENAMES
    assert "owner_attestation" in SKILL_OWNER_STATE_STEMS


def test_owner_attestation_target_is_agent_write_protected(tmp_path):
    # is_skill_owner_state_target recognizes the marker under data/state/skills/<name>/.
    data_root = tmp_path / "data"
    target = data_root / "state" / "skills" / "myskill" / "owner_attestation.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    assert is_skill_owner_state_target(target, data_root) is True


def _write_owner_attested_review(state_dir, content_hash, with_marker):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "review.json").write_text(json.dumps({
        "status": "clean",
        "content_hash": content_hash,
        "review_profile": "owner_attested",
        "reviewer_models": ["owner_attestation"],
        "findings": [{"item": "owner_attestation", "verdict": "PASS", "severity": "info",
                      "reason": "owner attested"}],
    }), encoding="utf-8")
    if with_marker:
        (state_dir / "owner_attestation.json").write_text(
            json.dumps({"attested_at": "now", "content_hash": content_hash}), encoding="utf-8")


def test_owner_attest_self_call_is_blocked_in_shell_and_browser():
    # The agent must not loopback-call the owner-only attestation endpoint through any
    # channel (otherwise it could self-bypass the immune system's skill review).
    from ouroboros.tools.registry import _detect_owner_skill_attest_self_call
    from ouroboros.tools.browser import _blocks_owner_skill_attest_js
    cmd = "curl -X post http://127.0.0.1:8765/api/owner/skills/myskill/attest-review"
    assert _detect_owner_skill_attest_self_call(cmd.lower()) is True
    js = "fetch('/api/owner/skills/myskill/attest-review', {method:'POST'})"
    assert _blocks_owner_skill_attest_js(js) is True
    # Percent-encoded paths (which the server decodes before routing) must NOT slip past.
    enc = "curl -x post http://127.0.0.1:8765/api/owner/skills/x/%61ttest-review"
    assert _detect_owner_skill_attest_self_call(enc.lower()) is True
    enc_js = "fetch('/api/owner/skills/x/attest%2dreview', {method:'POST'})"
    assert _blocks_owner_skill_attest_js(enc_js) is True
    # A normal command / JS is not blocked.
    assert _detect_owner_skill_attest_self_call("ls -la") is False
    assert _blocks_owner_skill_attest_js("document.title") is False


def test_owner_attest_route_level_post_blocked():
    # The Playwright route-level guard aborts a click/form-triggered POST (not just an
    # evaluate fetch) to the owner-only endpoint.
    from ouroboros.tools.browser import _is_owner_skill_attest_post

    class _Req:
        def __init__(self, method, url):
            self.method = method
            self.url = url

    assert _is_owner_skill_attest_post(_Req("POST", "http://127.0.0.1:8765/api/owner/skills/x/attest-review")) is True
    # The broad route glob reaches the handler for an encoded path; the handler decodes it.
    assert _is_owner_skill_attest_post(_Req("POST", "http://127.0.0.1:8765/api/owner/skills/x/attest%2Dreview")) is True
    assert _is_owner_skill_attest_post(_Req("GET", "http://127.0.0.1:8765/api/owner/skills/x/attest-review")) is False
    assert _is_owner_skill_attest_post(_Req("POST", "http://127.0.0.1:8765/api/skills/x/review")) is False


def test_lifecycle_invokes_attest_impl_positionally(tmp_path):
    # The owner endpoint routes through run_skill_review_lifecycle, which invokes
    # review_impl(ctx, skill_name) POSITIONALLY (no persist=/review_rebuttal=). Prove that
    # review_skill_owner_attest's signature matches exactly, so the integration cannot
    # TypeError at runtime.
    import inspect
    import types
    from ouroboros.skill_review_runner import _call_review_with_lifecycle_guard
    from ouroboros.skill_review import review_skill_owner_attest, SkillReviewOutcome, STATUS_PENDING

    seen = {}

    def _impl(ctx, skill_name):
        seen["args"] = (ctx, skill_name)
        return SkillReviewOutcome(skill_name=skill_name, status=STATUS_PENDING)

    fake_ctx = types.SimpleNamespace(drive_root=str(tmp_path))
    _call_review_with_lifecycle_guard(_impl, fake_ctx, "myskill")
    assert seen["args"] == (fake_ctx, "myskill")
    params = list(inspect.signature(review_skill_owner_attest).parameters)
    assert params == ["ctx", "skill_name"]


def test_owner_attest_rejects_marketplace_source(monkeypatch):
    import ouroboros.skill_review as sr

    class _Skill:
        name = "mk"
        load_error = None
        source = "clawhub"          # marketplace-managed -> NOT owner-own
        is_self_authored = False

    monkeypatch.setattr(sr, "find_skill", lambda dr, n: _Skill())

    class _Ctx:
        drive_root = "/tmp/nope"

    outcome = sr.review_skill_owner_attest(_Ctx(), "mk")
    assert outcome.status != sr.STATUS_CLEAN  # third-party payloads must use the full review
    assert "owner-attestation" in str(outcome.error or "").lower() or "marketplace" in str(outcome.error or "").lower()


def test_owner_attest_refuses_invalid_manifest(monkeypatch, tmp_path):
    # The deterministic floor must cover what the SKIPPED LLM manifest reviewer would catch:
    # a parsed-but-invalid manifest (validate() warnings) is NOT attestable.
    import types
    import ouroboros.skill_review as sr

    class _Manifest:
        def validate(self):
            return ["unknown type 'extension'", "type=extension requires a non-empty entry"]

    class _Skill:
        name = "s"
        manifest = _Manifest()

    # Preflight passes; the manifest-validate floor must still refuse.
    monkeypatch.setattr(sr, "_run_deterministic_preflight", lambda *a, **k: None)
    ctx = types.SimpleNamespace(drive_root=str(tmp_path))
    out = sr.run_owner_attestation(ctx, tmp_path, _Skill(), "hash")
    assert out.status == sr.STATUS_PENDING
    assert "validation" in str(out.error or "").lower()


def test_real_instruction_manifest_with_scripts_is_flagged():
    # A REAL parsed instruction manifest that declares executable scripts is a structural
    # mismatch the owner-attestation deterministic floor (via SkillManifest.validate) catches.
    from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
    m = parse_skill_manifest_text('{"name": "s", "type": "instruction", "scripts": ["run.py"]}')
    warnings = m.validate()
    assert any("must not declare executable" in w for w in warnings), warnings


def test_owner_attested_verdict_valid_only_with_marker(tmp_path):
    from ouroboros.skill_loader import load_review_state, skill_state_dir
    drive_root = tmp_path / "data"
    name = "myskill"
    state_dir = skill_state_dir(drive_root, name)

    # With the owner-issued marker present -> the attested verdict loads (clean, profile kept).
    _write_owner_attested_review(state_dir, "hash123", with_marker=True)
    state = load_review_state(drive_root, name, skill_type="script")
    assert state.review_profile == "owner_attested"
    assert state.content_hash == "hash123"
    assert state.status == "clean"

    # Remove the marker -> the verdict is INVALIDATED (fail-safe: drops to a blank/pending
    # state), exactly like native_seed provenance.
    (state_dir / "owner_attestation.json").unlink()
    state2 = load_review_state(drive_root, name, skill_type="script")
    assert state2.review_profile == ""
    assert state2.content_hash == ""
