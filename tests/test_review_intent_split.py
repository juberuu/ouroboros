"""Tests for v4.33.0 intent split in review_helpers.

Before v4.33.0 `resolve_intent` fell back to the full multi-line commit
message as the "intended transformation" reviewers should fact-check
against the code. Long commit bodies turned every explanatory sentence into
a verifiable claim — and reviewers (correctly) flagged prose-vs-code
divergence as `cross_surface_consistency` critical findings, creating a
loop where each retry added more prose and thus more claim surface.

v4.33.0 separates the two:
  - `resolve_intent` returns only the SUBJECT line (first line, capped at
    _COMMIT_SUBJECT_MAX_CHARS) as the auditable intent.
  - `build_goal_section` surfaces the full commit body separately under a
    `## Informational context` header with explicit "narrative, NOT a
    contract" framing so reviewers don't audit wording against code.
"""

from __future__ import annotations


class TestResolveIntentSubjectOnly:
    def test_subject_only_from_multiline_commit_message(self):
        from ouroboros.tools.review_helpers import resolve_intent

        msg = (
            "feat: unify review pipeline reliability\n"
            "\n"
            "This commit fixes N things:\n"
            "- opus-4.7 temperature 404\n"
            "- scope budget via HEAD snapshot removal\n"
            "- CHECKLISTS whitelist for prose mismatches\n"
        )
        text, source = resolve_intent(goal="", scope="", commit_message=msg)
        assert text == "feat: unify review pipeline reliability"
        assert source == "commit message (subject)"
        # Body contents must NOT appear in the resolved intent
        assert "opus-4.7 temperature 404" not in text
        assert "CHECKLISTS" not in text

    def test_goal_still_wins_over_commit_message(self):
        from ouroboros.tools.review_helpers import resolve_intent

        text, source = resolve_intent(
            goal="Explicit goal text",
            scope="",
            commit_message="fix: something entirely different",
        )
        assert text == "Explicit goal text"
        assert source == "goal"

    def test_scope_wins_over_commit_message_when_goal_empty(self):
        from ouroboros.tools.review_helpers import resolve_intent

        text, source = resolve_intent(
            goal="",
            scope="Scope description",
            commit_message="fix: something",
        )
        assert text == "Scope description"
        assert source == "scope"

    def test_empty_commit_yields_fallback(self):
        from ouroboros.tools.review_helpers import resolve_intent

        text, source = resolve_intent(goal="", scope="", commit_message="")
        assert source == "fallback"
        assert "Review the diff on its own merits" in text

    def test_subject_is_hard_capped(self):
        """Extremely long subject lines are capped so the intent section stays terse."""
        from ouroboros.tools.review_helpers import (
            _COMMIT_SUBJECT_MAX_CHARS,
            resolve_intent,
        )

        long_subject = "feat: " + "x" * 300
        text, source = resolve_intent(goal="", scope="", commit_message=long_subject)
        assert source == "commit message (subject)"
        assert len(text) <= _COMMIT_SUBJECT_MAX_CHARS


class TestBuildGoalSectionInformationalContext:
    def test_body_rendered_as_narrative_not_contract(self):
        from ouroboros.tools.review_helpers import build_goal_section

        msg = (
            "fix: retry review-state lock contention on windows\n"
            "\n"
            "Adds an exponential backoff around the lock.acquire loop.\n"
            "Also tweaks the docstring so Windows operators know to\n"
            "expect occasional delays under high concurrency.\n"
        )
        section = build_goal_section(goal="", scope="", commit_message=msg)

        # Intended transformation has ONLY the subject line
        assert "fix: retry review-state lock contention on windows" in section
        # The body appears in a SEPARATE informational block
        assert "## Informational context" in section
        assert "narrative" in section.lower()
        assert "NOT a contract" in section
        # Full body is still visible (for reviewer-human readers)
        assert "exponential backoff" in section

    def test_subject_only_commit_has_no_redundant_informational_block(self):
        from ouroboros.tools.review_helpers import build_goal_section

        section = build_goal_section(
            goal="",
            scope="",
            commit_message="fix: something small",
        )
        # No duplicate-body block when body == subject
        # (either Informational context is absent, or the subject is the only
        # thing in it — the latter would be redundant)
        if "## Informational context" in section:
            # Subject is the ONLY content, so it must not appear twice as the
            # narrative body after being rendered as intent
            assert section.count("fix: something small") <= 2

    def test_goal_and_commit_message_both_preserved(self):
        from ouroboros.tools.review_helpers import build_goal_section

        section = build_goal_section(
            goal="Add supported_parameters cache to LLMClient",
            scope="",
            commit_message="feat: pipeline reliability\n\nmany details here",
        )
        # Intent is the explicit goal
        assert "Add supported_parameters cache to LLMClient" in section
        # Commit body still available as informational narrative
        assert "## Informational context" in section
        assert "many details here" in section
