"""Phase 3 regression tests for ``ouroboros.skill_review``.

These tests mock out ``_handle_multi_model_review`` so no real LLM calls
happen. The focus is on:

- Parsing the flat ``{"results": [{"model", "text", "verdict", ...}]}``
  shape that the real review machinery emits.
- Aggregating PASS / FAIL / advisory verdicts across the seven skill
  checklist items.
- Quorum failure handling (fewer than 2 parseable reviewers).
- Persistence to ``data/state/skills/<name>/review.json``.
- Staleness detection across content-hash changes.
"""
from __future__ import annotations

import json
import pathlib
from typing import List
from unittest.mock import patch

import pytest

from ouroboros.skill_loader import (
    compute_content_hash,
    load_review_state,
)
from ouroboros.skill_review import (
    SkillReviewOutcome,
    _aggregate_status,
    _extract_actor_findings,
    _parse_json_array,
    review_skill,
)
from ouroboros.tools.registry import ToolContext


def _pass_array_for_script_skill() -> str:
    """Return a JSON array that PASSes every applicable skill checklist item."""
    return json.dumps(
        [
            {"item": "manifest_schema", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "permissions_honesty", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "no_repo_mutation", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "path_confinement", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "env_allowlist", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "timeout_and_output_discipline", "verdict": "PASS", "severity": "advisory", "reason": "ok"},
            {
                "item": "extension_namespace_discipline",
                "verdict": "PASS",
                "severity": "critical",
                "reason": "Not applicable — type != extension",
            },
            {
                "item": "widget_module_safety",
                "verdict": "PASS",
                "severity": "critical",
                "reason": "Not applicable — no module widget",
            },
        ]
    )


def _fail_array_on_manifest() -> str:
    return json.dumps(
        [
            {"item": "manifest_schema", "verdict": "FAIL", "severity": "critical", "reason": "type does not match payload"},
            {"item": "permissions_honesty", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "no_repo_mutation", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "path_confinement", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "env_allowlist", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "timeout_and_output_discipline", "verdict": "PASS", "severity": "advisory", "reason": "ok"},
            {"item": "extension_namespace_discipline", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "widget_module_safety", "verdict": "PASS", "severity": "critical", "reason": "ok"},
        ]
    )


def _advisory_only_array() -> str:
    return json.dumps(
        [
            {"item": "manifest_schema", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "permissions_honesty", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "no_repo_mutation", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "path_confinement", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "env_allowlist", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "timeout_and_output_discipline", "verdict": "FAIL", "severity": "advisory", "reason": "unbounded loop"},
            {"item": "extension_namespace_discipline", "verdict": "PASS", "severity": "critical", "reason": "ok"},
            {"item": "widget_module_safety", "verdict": "PASS", "severity": "critical", "reason": "ok"},
        ]
    )


def _make_actor(model: str, text: str) -> dict:
    """Mimic the flattened actor shape produced by _parse_model_response."""
    return {
        "model": model,
        "request_model": model,
        "provider": "openrouter",
        "verdict": "REVIEW",
        "text": text,
        "tokens_in": 100,
        "tokens_out": 50,
    }


def _build_skill(tmp_path: pathlib.Path, *, name: str = "weather") -> pathlib.Path:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / name
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            "description: Check the weather.\n"
            "version: 0.1.0\n"
            "type: script\n"
            "runtime: python3\n"
            "timeout_sec: 30\n"
            "scripts:\n"
            "  - name: fetch.py\n"
            "    description: Fetch data.\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "fetch.py").write_text("print('hi')\n", encoding="utf-8")
    return skills_root


def _make_ctx(tmp_path: pathlib.Path) -> ToolContext:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    return ToolContext(repo_dir=repo_dir, drive_root=drive_root)


def _patch_review(return_value: str):
    """Patch ``_handle_multi_model_review`` to return a canned result.

    The returned shape mirrors what the real function produces:
    ``json.dumps({"results": [...]})``.
    """
    return patch(
        "ouroboros.tools.review._handle_multi_model_review",
        return_value=return_value,
    )


# ---------------------------------------------------------------------------
# _parse_json_array + _extract_actor_findings
# ---------------------------------------------------------------------------


def test_parse_json_array_handles_fenced_code_blocks():
    text = "```json\n[{\"item\": \"x\", \"verdict\": \"PASS\"}]\n```"
    assert _parse_json_array(text) == [{"item": "x", "verdict": "PASS"}]


def test_parse_json_array_tolerates_leading_prose():
    text = "Sure! Here is the review:\n\n[{\"item\": \"x\", \"verdict\": \"PASS\"}]\nThanks."
    assert _parse_json_array(text) == [{"item": "x", "verdict": "PASS"}]


def test_parse_json_array_returns_empty_on_malformed_json():
    assert _parse_json_array("not json at all") == []
    assert _parse_json_array("[{broken") == []


def test_extract_actor_findings_reads_flat_text_field():
    """Regression: ``_parse_model_response`` flattens responses to
    ``{"model", "text", ...}`` — extract_actor_findings must read ``text``,
    not ``choices[0].message.content``."""
    result_json = {
        "results": [
            _make_actor("openai/gpt-5.5", _pass_array_for_script_skill()),
            _make_actor("google/gemini-3.1-pro-preview", _pass_array_for_script_skill()),
        ]
    }
    findings, responded = _extract_actor_findings(result_json)
    assert len(findings) == 16
    assert set(responded) == {
        "openai/gpt-5.5",
        "google/gemini-3.1-pro-preview",
    }
    assert all(f["verdict"] == "PASS" for f in findings)


def test_extract_actor_findings_skips_error_verdict_actors():
    """Transport errors (verdict=ERROR) must not contribute fake findings."""
    result_json = {
        "results": [
            _make_actor("openai/gpt-5.5", _pass_array_for_script_skill()),
            {
                "model": "google/gemini-3.1-pro-preview",
                "request_model": "google/gemini-3.1-pro-preview",
                "verdict": "ERROR",
                "text": "OpenRouter 404",
                "tokens_in": 0,
                "tokens_out": 0,
            },
        ]
    }
    findings, responded = _extract_actor_findings(result_json)
    assert all(f["model"] == "openai/gpt-5.5" for f in findings)
    assert responded == ["openai/gpt-5.5"]


def test_extract_actor_findings_rejects_partial_responses():
    """Phase 3 round 5 regression: a reviewer that returns only a subset
    of the 7 skill checklist items must NOT count toward quorum.

    Otherwise an actor returning just ``[{"item": "manifest_schema",
    "verdict": "PASS"}]`` would hand the pipeline a false PASS on the
    other 6 items simply by omitting them.
    """
    partial_text = json.dumps(
        [
            {"item": "manifest_schema", "verdict": "PASS", "severity": "critical", "reason": "ok"},
        ]
    )
    result_json = {
        "results": [
            _make_actor("openai/gpt-5.5", _pass_array_for_script_skill()),
            _make_actor("google/gemini-3.1-pro-preview", partial_text),
        ]
    }
    findings, responded = _extract_actor_findings(result_json)
    # Partial reviewer must be excluded from both findings and responded set.
    assert "google/gemini-3.1-pro-preview" not in responded
    assert set(responded) == {"openai/gpt-5.5"}
    for f in findings:
        assert f["model"] == "openai/gpt-5.5"


# ---------------------------------------------------------------------------
# _aggregate_status
# ---------------------------------------------------------------------------


def test_aggregate_status_pass_when_all_critical_pass():
    findings = [
        {"item": "manifest_schema", "verdict": "PASS", "severity": "critical"},
        {"item": "permissions_honesty", "verdict": "PASS", "severity": "critical"},
    ]
    assert _aggregate_status(findings, skill_type="script") == "pass"


def test_aggregate_status_fail_on_critical_fail():
    findings = [
        {"item": "no_repo_mutation", "verdict": "FAIL", "severity": "critical", "reason": "writes to repo"},
    ]
    assert _aggregate_status(findings, skill_type="script") == "fail"


def test_aggregate_status_advisory_on_soft_fail():
    findings = [
        {"item": "timeout_and_output_discipline", "verdict": "FAIL", "severity": "advisory", "reason": "unbounded loop"},
    ]
    assert _aggregate_status(findings, skill_type="script") == "advisory"


def test_aggregate_status_extension_namespace_fail_is_critical_only_for_extension():
    findings = [
        {"item": "extension_namespace_discipline", "verdict": "FAIL", "severity": "critical", "reason": "collides with built-in"},
    ]
    # For non-extension skills the extension_namespace_discipline FAIL is not blocking.
    assert _aggregate_status(findings, skill_type="script") == "advisory"
    # For extension skills it IS blocking.
    assert _aggregate_status(findings, skill_type="extension") == "fail"


def test_aggregate_status_widget_module_safety_fail_is_critical_only_for_module_widgets():
    findings = [
        {"item": "widget_module_safety", "verdict": "FAIL", "severity": "critical", "reason": "touches localStorage"},
    ]
    assert _aggregate_status(findings, skill_type="script") == "advisory"
    assert _aggregate_status(findings, skill_type="extension", is_module_widget=False) == "fail"
    assert _aggregate_status(findings, skill_type="extension", is_module_widget=True) == "fail"


# ---------------------------------------------------------------------------
# review_skill end-to-end (mocked LLM)
# ---------------------------------------------------------------------------


def test_review_skill_persists_pass_verdict(tmp_path, monkeypatch):
    skills_root = _build_skill(tmp_path)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    ctx = _make_ctx(tmp_path)
    pass_array = _pass_array_for_script_skill()
    canned = json.dumps(
        {
            "results": [
                _make_actor("openai/gpt-5.5", pass_array),
                _make_actor("google/gemini-3.1-pro-preview", pass_array),
                _make_actor("anthropic/claude-opus-4.6", pass_array),
            ]
        }
    )
    with _patch_review(canned):
        outcome = review_skill(ctx, "weather")

    assert isinstance(outcome, SkillReviewOutcome)
    assert outcome.status == "pass"
    assert outcome.error == ""
    assert set(outcome.reviewer_models) >= {
        "openai/gpt-5.5",
        "google/gemini-3.1-pro-preview",
    }
    persisted = load_review_state(ctx.drive_root, "weather")
    assert persisted.status == "pass"
    assert persisted.content_hash == outcome.content_hash
    # Content hash must actually match the on-disk snapshot so the
    # stale-review gate stays honest.
    expected_hash = compute_content_hash(skills_root / "weather")
    assert persisted.content_hash == expected_hash


def test_review_skill_returns_fail_on_critical_finding(tmp_path, monkeypatch):
    skills_root = _build_skill(tmp_path)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    ctx = _make_ctx(tmp_path)
    canned = json.dumps(
        {
            "results": [
                _make_actor("openai/gpt-5.5", _fail_array_on_manifest()),
                _make_actor("google/gemini-3.1-pro-preview", _fail_array_on_manifest()),
                _make_actor("anthropic/claude-opus-4.6", _fail_array_on_manifest()),
            ]
        }
    )
    with _patch_review(canned):
        outcome = review_skill(ctx, "weather")
    assert outcome.status == "fail"
    reasons = {f["reason"] for f in outcome.findings if f["verdict"] == "FAIL"}
    assert any("type does not match payload" in r for r in reasons)


def test_review_skill_returns_advisory_for_soft_only_fail(tmp_path, monkeypatch):
    skills_root = _build_skill(tmp_path)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    ctx = _make_ctx(tmp_path)
    canned = json.dumps(
        {
            "results": [
                _make_actor("openai/gpt-5.5", _advisory_only_array()),
                _make_actor("google/gemini-3.1-pro-preview", _advisory_only_array()),
                _make_actor("anthropic/claude-opus-4.6", _advisory_only_array()),
            ]
        }
    )
    with _patch_review(canned):
        outcome = review_skill(ctx, "weather")
    assert outcome.status == "advisory"


def test_review_skill_quorum_failure_on_one_responder(tmp_path, monkeypatch):
    skills_root = _build_skill(tmp_path)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    ctx = _make_ctx(tmp_path)
    # Only one responder, two ERROR legs.
    canned = json.dumps(
        {
            "results": [
                _make_actor("openai/gpt-5.5", _pass_array_for_script_skill()),
                {
                    "model": "google/gemini-3.1-pro-preview",
                    "request_model": "google/gemini-3.1-pro-preview",
                    "verdict": "ERROR",
                    "text": "OpenRouter 404",
                    "tokens_in": 0, "tokens_out": 0,
                },
                {
                    "model": "anthropic/claude-opus-4.6",
                    "request_model": "anthropic/claude-opus-4.6",
                    "verdict": "ERROR",
                    "text": "OpenRouter 429",
                    "tokens_in": 0, "tokens_out": 0,
                },
            ]
        }
    )
    with _patch_review(canned):
        outcome = review_skill(ctx, "weather")
    assert outcome.status == "pending"
    assert "quorum" in outcome.error.lower()


def test_review_skill_error_on_non_json_top_level(tmp_path, monkeypatch):
    """A non-JSON top-level response from ``_handle_multi_model_review``
    must surface as status=pending with the error populated, not crash
    and not be mistaken for a successful review."""
    skills_root = _build_skill(tmp_path)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    ctx = _make_ctx(tmp_path)
    with _patch_review("not json"):
        outcome = review_skill(ctx, "weather")
    assert outcome.status == "pending"
    assert "non-JSON" in outcome.error


def test_review_skill_missing_skill_returns_pending_with_error(tmp_path, monkeypatch):
    skills_root = _build_skill(tmp_path)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    ctx = _make_ctx(tmp_path)
    outcome = review_skill(ctx, "does-not-exist")
    assert outcome.status == "pending"
    assert "not found" in outcome.error


def test_skill_review_hard_blocks_extensionless_binary(tmp_path, monkeypatch):
    """Phase 3 round 15 regression: ANY non-UTF8 file in the runtime-
    reachable surface is a hard-block, not just extension-matched
    loadable formats. An extensionless disguised binary must still
    raise ``_SkillBinaryPayload`` so raw bytes never reach reviewer
    models and no PASS verdict ships over an opaque hash."""
    from ouroboros.skill_review import _read_capped_text, _SkillBinaryPayload

    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "bin1"
    skill_dir.mkdir(parents=True)
    # Invalid UTF-8 bytes, no telltale extension (could be a Mach-O or
    # ELF blob disguised with a misleading ``.dat`` suffix).
    payload = b"\xff\xfeBEGIN CERT leak-me-please\xff\xc0\xc1\xfe\xff"
    (skill_dir / "cert.dat").write_bytes(payload)

    with pytest.raises(_SkillBinaryPayload):
        _read_capped_text(skill_dir / "cert.dat", relpath="cert.dat")


def test_skill_review_blocks_loadable_native_binaries(tmp_path):
    """Phase 3 round 13 regression: loadable native code
    (``.so``/``.dylib``/``.pyc``/``.node``/``.wasm``) must hard-block
    review. The subprocess could otherwise ``ctypes.CDLL`` / import /
    require the blob and execute never-reviewed code even under a
    PASS verdict."""
    from ouroboros.skill_review import _read_capped_text, _SkillBinaryPayload

    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "nativelink"
    skill_dir.mkdir(parents=True)
    target = skill_dir / "evil.so"
    target.write_bytes(b"\x7fELF" + b"\x00" * 128)
    with pytest.raises(_SkillBinaryPayload):
        _read_capped_text(target, relpath="evil.so")


def test_review_skill_fails_closed_on_unreadable_payload(tmp_path, monkeypatch):
    """Phase 3 round 18 regression: an unreadable payload file must
    fail review CLOSED (pending + error) instead of letting the
    placeholder slip past the gate. Regression for the old behaviour
    where ``_read_capped_text`` returned a string on OSError and
    ``compute_content_hash`` silently skipped the file."""
    import os, platform
    if platform.system() == "Windows":
        pytest.skip("chmod-based permission test not portable to Windows")
    if os.geteuid() == 0:  # pragma: no cover
        pytest.skip("root user bypasses 0o000 chmod")
    skills_root = _build_skill(tmp_path)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    script = skills_root / "weather" / "scripts" / "fetch.py"
    original = script.stat().st_mode
    os.chmod(script, 0o000)
    try:
        ctx = _make_ctx(tmp_path)
        with patch(
            "ouroboros.tools.review._handle_multi_model_review",
            side_effect=AssertionError("must not call reviewer on unreadable payload"),
        ):
            outcome = review_skill(ctx, "weather")
    finally:
        os.chmod(script, original)
    assert outcome.status == "pending"
    assert "unreadable" in outcome.error.lower()


def test_review_skill_refuses_when_payload_contains_native_binary(tmp_path, monkeypatch):
    """End-to-end regression for loadable-binary block: ``review_skill``
    returns ``pending`` with an actionable error instead of persisting a
    verdict over a content hash that covers opaque machine code."""
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "nativepack"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: nativepack\ntype: script\nversion: 0.1.0\nruntime: python3\ntimeout_sec: 30\nscripts:\n  - name: main.py\n---\nbody\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / "libevil.dylib").write_bytes(b"\xca\xfe\xba\xbe" + b"\x00" * 64)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    ctx = _make_ctx(tmp_path)
    with patch(
        "ouroboros.tools.review._handle_multi_model_review",
        side_effect=AssertionError("must not call reviewer when native blob present"),
    ):
        outcome = review_skill(ctx, "nativepack")
    assert outcome.status == "pending"
    assert "binary" in outcome.error.lower()
    assert "opaque" in outcome.error.lower()


def test_review_skill_refuses_oversized_individual_file(tmp_path, monkeypatch):
    """Phase 3 round 8 regression: a single oversized script must block
    review rather than be truncated. Truncation would let malicious
    logic hide past the cap and still ship a PASS verdict tied to the
    full on-disk content hash."""
    from ouroboros.skill_review import _MAX_SKILL_FILE_BYTES

    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "whale"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: whale\n"
            "description: A single huge file.\n"
            "version: 0.1.0\n"
            "type: script\n"
            "runtime: python3\n"
            "timeout_sec: 30\n"
            "scripts:\n"
            "  - name: big.py\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    # Build a file just over the per-file cap.
    (skill_dir / "scripts" / "big.py").write_text(
        "# " + "x" * (_MAX_SKILL_FILE_BYTES + 10) + "\nprint('hi')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    ctx = _make_ctx(tmp_path)
    with patch(
        "ouroboros.tools.review._handle_multi_model_review",
        side_effect=AssertionError("must not be called for oversized files"),
    ):
        outcome = review_skill(ctx, "whale")
    assert outcome.status == "pending"
    assert "per-file cap" in outcome.error
    persisted = load_review_state(ctx.drive_root, "whale")
    assert persisted.status == "pending"


def test_review_skill_refuses_oversized_skill_pack(tmp_path, monkeypatch):
    """Phase 3 round 5 regression: a skill with more payload files than
    ``_MAX_SKILL_FILES`` must not silently truncate the review pack.

    Review returns status=pending with a descriptive error; the verdict
    is NOT persisted, and `_handle_multi_model_review` is never even
    invoked — belt-and-braces against a pathological skill sneaking
    executable logic past review.
    """
    from ouroboros.skill_review import _MAX_SKILL_FILES

    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "huge"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: huge\n"
            "description: Too many files to review.\n"
            "version: 0.1.0\n"
            "type: script\n"
            "runtime: python3\n"
            "timeout_sec: 30\n"
            "scripts:\n"
            "  - name: first.py\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    # Generate > _MAX_SKILL_FILES payload files so the cap fires.
    for i in range(_MAX_SKILL_FILES + 5):
        (skill_dir / "scripts" / f"f_{i:03d}.py").write_text(
            f"print({i})\n", encoding="utf-8"
        )
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    ctx = _make_ctx(tmp_path)
    # We expect review to short-circuit BEFORE calling into the LLM
    # machinery; patching `_handle_multi_model_review` to raise guarantees
    # that happens.
    with patch(
        "ouroboros.tools.review._handle_multi_model_review",
        side_effect=AssertionError("must not be called when pack is oversized"),
    ):
        outcome = review_skill(ctx, "huge")
    assert outcome.status == "pending"
    assert "Skill pack exceeds reviewable cap" in outcome.error
    persisted = load_review_state(ctx.drive_root, "huge")
    # Must NOT write a verdict on transport/infra failures.
    assert persisted.status == "pending"


def test_review_skill_prompt_loads_core_governance_artifacts(tmp_path, monkeypatch):
    """DEVELOPMENT.md 'When adding a new reasoning flow' rule requires
    ARCHITECTURE.md and DEVELOPMENT.md to appear in the assembled skill
    review prompt. Regression guard for Phase 3 round 6 finding."""
    skills_root = _build_skill(tmp_path)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    ctx = _make_ctx(tmp_path)

    captured = {}

    def fake_review(ctx_, *, content, prompt, models):
        captured["prompt"] = prompt
        return json.dumps(
            {
                "results": [
                    _make_actor("openai/gpt-5.5", _pass_array_for_script_skill()),
                    _make_actor("google/gemini-3.1-pro-preview", _pass_array_for_script_skill()),
                ]
            }
        )

    with patch("ouroboros.tools.review._handle_multi_model_review", side_effect=fake_review):
        review_skill(ctx, "weather")

    prompt = captured.get("prompt", "")
    assert prompt, "review_skill did not invoke _handle_multi_model_review"
    assert "docs/ARCHITECTURE.md" in prompt, (
        "skill review prompt must cite ARCHITECTURE.md as governance context"
    )
    assert "docs/DEVELOPMENT.md" in prompt, (
        "skill review prompt must cite DEVELOPMENT.md as governance context"
    )
    # Phase 3 round 10 regression: BIBLE.md must also be loaded so the
    # reviewer has constitutional tie-breaker context.
    assert "BIBLE.md" in prompt, (
        "skill review prompt must cite BIBLE.md for constitutional context"
    )
    # Minimal content-presence check: Section 10 key-invariants header is
    # referenced by label, and the actual body should appear (shipping
    # repo has the canonical text there).
    assert "Key Invariants" in prompt


def test_review_skill_persist_false_does_not_write(tmp_path, monkeypatch):
    skills_root = _build_skill(tmp_path)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    ctx = _make_ctx(tmp_path)
    pass_array = _pass_array_for_script_skill()
    canned = json.dumps(
        {
            "results": [
                _make_actor("openai/gpt-5.5", pass_array),
                _make_actor("google/gemini-3.1-pro-preview", pass_array),
            ]
        }
    )
    with _patch_review(canned):
        outcome = review_skill(ctx, "weather", persist=False)
    assert outcome.status == "pass"
    persisted = load_review_state(ctx.drive_root, "weather")
    # Default state: nothing written.
    assert persisted.status == "pending"
    assert persisted.content_hash == ""
