"""Tests for OuroborosHub publish gate + PR-body advisory disclosure (Block C1).

Hermetic: no network, no real GitHub. Covers that publication now allows a
fresh advisory-only ``warnings`` review (not just ``clean``), still rejects
``blockers``/``pending`` and stale reviews, and that advisory findings are
surfaced in the generated PR body while the secret scan stays enforced.
"""
from __future__ import annotations

import types

import pytest

from ouroboros.config import SKILL_SOURCE_EXTERNAL
from ouroboros.skill_loader import LoadedSkill, SkillReviewState
from ouroboros.skill_review_status import (
    STATUS_BLOCKERS,
    STATUS_CLEAN,
    STATUS_PENDING,
    STATUS_WARNINGS,
)
from ouroboros.tools import skill_publish


def _manifest(version: str = "1.0.0") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        entry=None,
        scripts={},
        version=version,
        type="instruction",
        description="A test skill.",
    )


def _loaded(
    *,
    status: str,
    content_hash: str = "h",
    findings: list | None = None,
    source: str = SKILL_SOURCE_EXTERNAL,
    skill_dir=None,
) -> LoadedSkill:
    return LoadedSkill(
        name="demo",
        skill_dir=skill_dir or types.SimpleNamespace(),
        manifest=_manifest(),
        content_hash=content_hash,
        enabled=True,
        review=SkillReviewState(
            status=status,
            content_hash=content_hash,
            findings=list(findings or []),
        ),
        source=source,
    )


def _patch_validate(monkeypatch, loaded: LoadedSkill, computed_hash: str = "h") -> None:
    monkeypatch.setattr(skill_publish, "github_token_from_env_or_settings", lambda: "tok")
    monkeypatch.setattr(skill_publish, "find_skill", lambda root, name: loaded)
    monkeypatch.setattr(skill_publish, "compute_content_hash", lambda *a, **k: computed_hash)


# --- _advisory_findings_section (pure) ------------------------------------

def test_advisory_section_empty_when_no_advisory_findings():
    review = SkillReviewState(findings=[{"item": "manifest_schema", "verdict": "PASS"}])
    assert skill_publish._advisory_findings_section(review) == ""


def test_advisory_section_lists_and_dedups_all_fail_rows():
    # No severity filter: the publish gate already proves the review has no
    # blockers, so EVERY FAIL row present is non-blocking by construction and
    # must be disclosed — including rows labeled "critical" on generic items
    # whose aggregation ignores severity (the silently-hidden class).
    review = SkillReviewState(
        findings=[
            {"item": "bug_hunting", "verdict": "FAIL", "severity": "advisory", "reason": "minor off-by-one"},
            {"item": "bug_hunting", "verdict": "FAIL", "severity": "advisory", "reason": "minor off-by-one"},
            {"item": "style", "verdict": "FAIL", "severity": "warning", "reason": "long line"},
            {"item": "error_handling", "verdict": "FAIL", "severity": "critical", "reason": "swallowed exception"},
            {"item": "timeout_and_output_discipline", "verdict": "FAIL", "severity": "minor", "reason": "no timeout"},
            {"item": "doc", "verdict": "PASS", "severity": "advisory", "reason": "fine"},
        ]
    )
    section = skill_publish._advisory_findings_section(review)
    assert "## Known advisory findings" in section
    assert section.count("`bug_hunting`") == 1  # deduped
    assert "long line" in section
    assert "swallowed exception" in section  # critical-labeled FAIL IS disclosed
    assert "(critical)" in section
    assert "no timeout" in section  # free-form severity IS disclosed
    assert "fine" not in section  # PASS excluded


def test_advisory_section_sanitizes_reviewer_controlled_strings():
    # severity/item land in a PUBLIC hub PR body: inner newlines and backtick
    # fences must not break out of the markdown list row.
    review = SkillReviewState(
        findings=[
            {
                "item": "bug`hunting`\nrow",
                "verdict": "FAIL",
                "severity": "crit\nical\n## Injected",
                "reason": "multi\nline\nreason",
            },
        ]
    )
    section = skill_publish._advisory_findings_section(review)
    rows = [line for line in section.splitlines() if line.startswith("- ")]
    assert len(rows) == 1  # the whole finding stays on one list row
    assert "## Injected" not in section.splitlines()  # no new markdown block
    assert "crit ical" in rows[0]
    assert "bughunting row" in rows[0]
    assert "multi line reason" in rows[0]


# --- _validate_local_skill status gate ------------------------------------

@pytest.mark.parametrize("status", [STATUS_CLEAN, STATUS_WARNINGS, "advisory", "advisory_pass"])
def test_validate_accepts_clean_and_warnings(monkeypatch, status):
    loaded = _loaded(status=status)
    _patch_validate(monkeypatch, loaded)
    ctx = types.SimpleNamespace(drive_root="/tmp/whatever")
    safe, returned = skill_publish._validate_local_skill(ctx, "demo")
    assert returned is loaded


@pytest.mark.parametrize("status", [STATUS_BLOCKERS, STATUS_PENDING, "fail", "weird-unknown"])
def test_validate_rejects_blockers_and_pending(monkeypatch, status):
    loaded = _loaded(status=status)
    _patch_validate(monkeypatch, loaded)
    ctx = types.SimpleNamespace(drive_root="/tmp/whatever")
    with pytest.raises(ValueError, match="no blockers"):
        skill_publish._validate_local_skill(ctx, "demo")


def test_validate_rejects_stale_warnings(monkeypatch):
    loaded = _loaded(status=STATUS_WARNINGS, content_hash="old")
    _patch_validate(monkeypatch, loaded, computed_hash="new")
    ctx = types.SimpleNamespace(drive_root="/tmp/whatever")
    with pytest.raises(ValueError, match="stale"):
        skill_publish._validate_local_skill(ctx, "demo")


# --- _generate_pr_body advisory disclosure + secret scan ------------------

def _pr_loaded(tmp_path, review: SkillReviewState) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        manifest=_manifest(),
        skill_dir=tmp_path,
        source=SKILL_SOURCE_EXTERNAL,
        review=review,
    )


def test_pr_body_fallback_includes_advisory_block(tmp_path):
    # No SKILL.md in skill_dir -> deterministic fallback path.
    review = SkillReviewState(
        findings=[{"item": "bug_hunting", "verdict": "FAIL", "severity": "advisory", "reason": "rotates each round"}]
    )
    ctx = types.SimpleNamespace(emit_progress_fn=lambda *_: None, pending_events=[])
    body = skill_publish._generate_pr_body(ctx, "add", "demo", [], "", _pr_loaded(tmp_path, review))
    assert "## Known advisory findings" in body
    assert "rotates each round" in body


def test_pr_body_llm_heading_without_rows_is_replaced(tmp_path, monkeypatch):
    # Triad regression: an LLM body that contains the heading but OMITS rows
    # must not pass on heading presence — the deterministic sanitized block
    # replaces any model-authored section.
    review = SkillReviewState(
        findings=[
            {"item": "error_handling", "verdict": "FAIL", "severity": "critical", "reason": "swallowed exception"},
            {"item": "style", "verdict": "FAIL", "severity": "advisory", "reason": "long line"},
        ]
    )
    (tmp_path / "SKILL.md").write_text("---\nname: demo\n---\nDoc.", encoding="utf-8")

    class _FakeLLM:
        def chat(self, **_kwargs):
            return (
                {
                    "content": (
                        "## Summary\nNice skill.\n\n"
                        "## Known advisory findings\n- `style` (advisory): long line\n"
                    )
                },
                {"cost": 0.0},
            )

    monkeypatch.setattr(skill_publish, "LLMClient", lambda: _FakeLLM())
    ctx = types.SimpleNamespace(emit_progress_fn=lambda *_: None, pending_events=[])
    body = skill_publish._generate_pr_body(ctx, "add", "demo", [], "", _pr_loaded(tmp_path, review))
    assert body.count("## Known advisory findings") == 1
    assert "swallowed exception" in body  # the omitted row is restored
    assert "long line" in body


def test_strip_advisory_section_ignores_quoted_heading_mid_line():
    # A heading quoted mid-line (or in prose) is not a section boundary; only
    # line-anchored headings are stripped.
    body = (
        "## Summary\nThe phrase ## Known advisory findings appears quoted here.\n\n"
        "## Known advisory findings\n- `style` (advisory): stale row\n\n"
        "## Footer\ntail\n"
    )
    out = skill_publish._strip_advisory_findings_section(body)
    assert "quoted here" in out  # mid-line mention untouched
    assert "stale row" not in out  # real section removed
    assert "## Footer" in out


def test_pr_body_rejects_secret_note(tmp_path):
    review = SkillReviewState(findings=[])
    ctx = types.SimpleNamespace(emit_progress_fn=lambda *_: None, pending_events=[])
    secret_note = "deploy with key AKIAIOSFODNN7EXAMPLE please"
    with pytest.raises(ValueError, match="secret"):
        skill_publish._generate_pr_body(ctx, "add", "demo", [], secret_note, _pr_loaded(tmp_path, review))
