"""Tests for review_synthesis.py — LLM-based claim deduplication (Phase 1).

Coverage areas:
  - Fallback: empty / single finding passes through without LLM call
  - Fallback: LLM error returns original findings unchanged
  - Fallback: LLM parse failure returns original findings unchanged
  - Dedup: multiple claims with same root cause merged to one canonical issue
  - No-merge: distinct bugs in same file remain separate
  - obligation_id preservation: existing id carried forward when matched
  - Truncation: claims beyond _MAX_CLAIMS_FOR_SYNTHESIS are appended unchanged
  - _parse_synthesis_output: valid JSON, markdown-fenced JSON, invalid JSON
  - synthesize_to_canonical_issues: end-to-end with mocked LLM
  - commit_gate: synthesis step fires ONLY on status="blocked" with findings
  - commit_gate: synthesis step is no-op (fail-open) when module import fails
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_finding(item: str, reason: str, severity: str = "critical",
                   tag: str = "triad", obligation_id: str = "") -> Dict[str, Any]:
    f = {"item": item, "reason": reason, "severity": severity, "tag": tag}
    if obligation_id:
        f["obligation_id"] = obligation_id
    return f


def _make_obligation(obligation_id: str, item: str, reason: str) -> Any:
    o = MagicMock()
    o.obligation_id = obligation_id
    o.item = item
    o.reason = reason
    return o


# ---------------------------------------------------------------------------
# _parse_synthesis_output
# ---------------------------------------------------------------------------

class TestParseSynthesisOutput:
    def _parse(self, text: str):
        from ouroboros.tools.review_synthesis import _parse_synthesis_output
        return _parse_synthesis_output(text)

    def test_valid_json_array(self):
        raw = json.dumps([
            {"item": "code_quality", "severity": "critical", "reason": "null deref",
             "obligation_id": "", "evidence_from_reviewers": ["triad"]}
        ])
        result = self._parse(raw)
        assert result is not None
        assert len(result) == 1
        assert result[0]["item"] == "code_quality"

    def test_markdown_fenced_json(self):
        raw = "```json\n" + json.dumps([
            {"item": "tests_affected", "severity": "critical", "reason": "no test",
             "obligation_id": "obl-0001", "evidence_from_reviewers": ["scope"]}
        ]) + "\n```"
        result = self._parse(raw)
        assert result is not None
        assert result[0]["obligation_id"] == "obl-0001"

    def test_empty_string_returns_none(self):
        assert self._parse("") is None

    def test_invalid_json_returns_none(self):
        assert self._parse("not json at all") is None

    def test_empty_array_returns_none(self):
        # An empty array means the synthesizer found nothing — treat as parse failure
        assert self._parse("[]") is None

    def test_entry_without_item_skipped(self):
        raw = json.dumps([
            {"severity": "critical", "reason": "some reason", "obligation_id": ""},
            {"item": "code_quality", "severity": "critical", "reason": "real finding",
             "obligation_id": "", "evidence_from_reviewers": []},
        ])
        result = self._parse(raw)
        assert result is not None
        assert len(result) == 1
        assert result[0]["item"] == "code_quality"

    def test_extra_fields_preserved(self):
        """Extra fields like 'tag', 'verdict', 'model' should be carried forward."""
        raw = json.dumps([
            {"item": "code_quality", "severity": "critical", "reason": "bug",
             "obligation_id": "", "evidence_from_reviewers": [],
             "tag": "triad", "verdict": "FAIL"}
        ])
        result = self._parse(raw)
        assert result[0].get("tag") == "triad"
        assert result[0].get("verdict") == "FAIL"


# ---------------------------------------------------------------------------
# synthesize_to_canonical_issues — unit tests with mocked LLM
# ---------------------------------------------------------------------------

class TestSynthesizeToCanonicalIssues:

    def _synth(self, findings, open_obligations=None, llm_response=None,
               llm_raises=False):
        from ouroboros.tools.review_synthesis import synthesize_to_canonical_issues

        def _fake_call(prompt, ctx=None):
            if llm_raises:
                raise RuntimeError("fake LLM error")
            return llm_response

        with patch(
            "ouroboros.tools.review_synthesis._call_synthesis_llm",
            side_effect=_fake_call,
        ):
            return synthesize_to_canonical_issues(
                findings,
                open_obligations=open_obligations,
                ctx=None,
            )

    def test_empty_list_passthrough_no_llm(self):
        """Empty findings list → returned unchanged, no LLM call."""
        result = self._synth([])
        assert result == []

    def test_single_finding_passthrough_no_llm(self):
        """Single finding → returned unchanged (below MIN_CLAIMS threshold)."""
        finding = _make_finding("code_quality", "null deref")
        result = self._synth([finding])
        assert result == [finding]

    def test_llm_error_returns_original(self):
        """LLM raises → fail-open, return original findings."""
        findings = [
            _make_finding("code_quality", "null deref"),
            _make_finding("tests_affected", "no test"),
        ]
        result = self._synth(findings, llm_raises=True)
        assert result == findings

    def test_llm_parse_failure_returns_original(self):
        """LLM returns unparseable output → return original findings."""
        findings = [
            _make_finding("code_quality", "null deref"),
            _make_finding("tests_affected", "no test"),
        ]
        result = self._synth(findings, llm_response="not valid json at all")
        assert result == findings

    def test_llm_none_response_returns_original(self):
        """LLM returns None → return original findings."""
        findings = [
            _make_finding("code_quality", "null deref"),
            _make_finding("code_quality", "same bug from scope reviewer"),
        ]
        result = self._synth(findings, llm_response=None)
        assert result == findings

    def test_successful_dedup_reduces_count(self):
        """Two claims about the same root cause → merged to one canonical issue."""
        findings = [
            _make_finding("code_quality", "null deref in foo.py", tag="triad"),
            _make_finding("code_quality", "null pointer in foo.py line 42", tag="scope"),
        ]
        canonical_response = json.dumps([{
            "item": "code_quality",
            "severity": "critical",
            "reason": "null deref in foo.py line 42",
            "obligation_id": "",
            "evidence_from_reviewers": ["triad", "scope"],
        }])
        result = self._synth(findings, llm_response=canonical_response)
        assert len(result) == 1
        assert result[0]["item"] == "code_quality"
        assert result[0]["evidence_from_reviewers"] == ["triad", "scope"]

    def test_distinct_bugs_not_merged(self):
        """Two genuinely different findings remain separate after synthesis."""
        findings = [
            _make_finding("code_quality", "null deref in foo.py"),
            _make_finding("tests_affected", "no test for bar.py"),
        ]
        canonical_response = json.dumps([
            {"item": "code_quality", "severity": "critical",
             "reason": "null deref in foo.py",
             "obligation_id": "", "evidence_from_reviewers": ["triad"]},
            {"item": "tests_affected", "severity": "critical",
             "reason": "no test for bar.py",
             "obligation_id": "", "evidence_from_reviewers": ["triad"]},
        ])
        result = self._synth(findings, llm_response=canonical_response)
        assert len(result) == 2
        items = {r["item"] for r in result}
        assert items == {"code_quality", "tests_affected"}

    def test_existing_obligation_id_preserved(self):
        """When synthesizer matches an existing obligation, its id is preserved."""
        findings = [
            _make_finding("tests_affected", "no tests for new logic"),
            _make_finding("tests_affected", "missing coverage for path X"),
        ]
        obligations = [_make_obligation("obl-0001", "tests_affected", "no tests")]
        canonical_response = json.dumps([{
            "item": "tests_affected",
            "severity": "critical",
            "reason": "no tests for new logic or path X",
            "obligation_id": "obl-0001",
            "evidence_from_reviewers": ["triad", "scope"],
        }])
        result = self._synth(findings, open_obligations=obligations,
                              llm_response=canonical_response)
        assert len(result) == 1
        assert result[0]["obligation_id"] == "obl-0001"

    def test_overflow_returns_original_unchanged(self):
        """Findings beyond _MAX_CLAIMS_FOR_SYNTHESIS → original returned (no mixed list)."""
        from ouroboros.tools.review_synthesis import _MAX_CLAIMS_FOR_SYNTHESIS

        # Create more findings than the limit
        findings = [
            _make_finding("code_quality", f"issue {i}") for i in range(_MAX_CLAIMS_FOR_SYNTHESIS + 5)
        ]
        # LLM would deduplicate, but should NOT be called — original returned
        result = self._synth(findings, llm_response="[]")
        # Original list returned unchanged — no hybrid mixing
        assert result == findings
        assert len(result) == _MAX_CLAIMS_FOR_SYNTHESIS + 5

    def test_no_model_available_returns_original(self):
        """When _call_synthesis_llm raises (no LLM), return original."""
        findings = [
            _make_finding("code_quality", "bug A"),
            _make_finding("code_quality", "bug B"),
        ]
        result = self._synth(findings, llm_raises=True)
        assert result == findings


# ---------------------------------------------------------------------------
# commit_gate integration: structural contract tests
# ---------------------------------------------------------------------------

class TestEmitSynthesisUsage:
    """_emit_synthesis_usage must prefer resolved metadata from usage dict."""

    def _emit(self, usage, model="anthropic/claude-sonnet-4.6"):
        from ouroboros.tools.review_synthesis import _emit_synthesis_usage
        import types
        ctx = types.SimpleNamespace(task_id="test-task", event_queue=None, pending_events=[])
        _emit_synthesis_usage(ctx, model=model, usage=usage)
        return ctx.pending_events

    def test_resolved_model_and_provider_from_usage(self):
        """When usage contains resolved_model and provider, those override the configured model."""
        usage = {
            "prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001,
            "resolved_model": "openai::gpt-5.4-mini",
            "provider": "openai",
        }
        events = self._emit(usage, model="anthropic/claude-sonnet-4.6")
        assert len(events) == 1
        ev = events[0]
        assert ev["model"] == "openai::gpt-5.4-mini"
        assert ev["provider"] == "openai"

    def test_fallback_to_configured_model_when_no_resolved(self):
        """When usage lacks resolved_model/provider, falls back to configured model string."""
        usage = {"prompt_tokens": 80, "completion_tokens": 40, "cost": 0.0005}
        events = self._emit(usage, model="anthropic/claude-sonnet-4.6")
        assert len(events) == 1
        ev = events[0]
        assert ev["model"] == "anthropic/claude-sonnet-4.6"

    def test_empty_usage_emits_nothing(self):
        """Zero tokens and zero cost → no event emitted."""
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        events = self._emit(usage)
        assert events == []

    def test_none_usage_emits_nothing(self):
        """None usage → no event emitted."""
        events = self._emit(None)
        assert events == []


class TestNormalizeEvidence:
    """_normalize_evidence must guard against bare-string and non-list LLM output."""

    def test_bare_string_wrapped_in_list(self):
        from ouroboros.tools.review_synthesis import _normalize_evidence
        assert _normalize_evidence("triad") == ["triad"]

    def test_proper_list_passthrough(self):
        from ouroboros.tools.review_synthesis import _normalize_evidence
        assert _normalize_evidence(["triad", "scope"]) == ["triad", "scope"]

    def test_none_returns_empty(self):
        from ouroboros.tools.review_synthesis import _normalize_evidence
        assert _normalize_evidence(None) == []

    def test_non_string_members_filtered(self):
        from ouroboros.tools.review_synthesis import _normalize_evidence
        assert _normalize_evidence(["triad", 42, None, "scope"]) == ["triad", "scope"]


class TestParseVerdictDefault:
    """_parse_synthesis_output must default verdict to 'FAIL'."""

    def test_verdict_defaulted_to_fail_when_absent(self):
        """When synthesizer omits 'verdict', it must be defaulted to 'FAIL'."""
        from ouroboros.tools.review_synthesis import _parse_synthesis_output
        raw = json.dumps([{
            "item": "code_quality",
            "severity": "critical",
            "reason": "null deref",
            "obligation_id": "",
            "evidence_from_reviewers": [],
        }])
        result = _parse_synthesis_output(raw)
        assert result is not None
        assert result[0]["verdict"] == "FAIL"

    def test_explicit_verdict_preserved(self):
        """When synthesizer provides explicit verdict, it is preserved."""
        from ouroboros.tools.review_synthesis import _parse_synthesis_output
        raw = json.dumps([{
            "item": "code_quality",
            "severity": "critical",
            "reason": "null deref",
            "obligation_id": "",
            "evidence_from_reviewers": [],
            "verdict": "FAIL",
        }])
        result = _parse_synthesis_output(raw)
        assert result is not None
        assert result[0]["verdict"] == "FAIL"


class TestSecretRedaction:
    """_format_claims and _format_obligations must apply redact_prompt_secrets to reason strings."""

    def test_claims_reason_passes_through_redact(self):
        """_format_claims must call _redact on reason strings (KEY=value patterns are redacted)."""
        from ouroboros.tools.review_synthesis import _format_claims
        # Use a key=value pattern that redact_prompt_secrets matches via _SECRET_LINE_RE
        findings = [_make_finding("secrets_check", "OPENROUTER_API_KEY=sk-or-v1-supersecret")]
        result = _format_claims(findings)
        assert "sk-or-v1-supersecret" not in result
        assert "REDACTED" in result

    def test_obligations_reason_passes_through_redact(self):
        """_format_obligations must call _redact on obligation reason excerpts."""
        from ouroboros.tools.review_synthesis import _format_obligations
        ob = _make_obligation("obl-0001", "secrets_check",
                               "ANTHROPIC_API_KEY=sk-ant-supersecret was in the diff")
        result = _format_obligations([ob])
        assert "sk-ant-supersecret" not in result
        assert "REDACTED" in result

    def test_redact_called_on_claims(self):
        """_format_claims must invoke _redact for each claim reason (verified via mock)."""
        from ouroboros.tools import review_synthesis
        with patch.object(review_synthesis, "_redact", wraps=review_synthesis._redact) as mock_redact:
            findings = [_make_finding("code_quality", "some reason text")]
            review_synthesis._format_claims(findings)
            assert mock_redact.call_count >= 1


class TestCommitGateSynthesisIntegration:
    """Verify the synthesis integration contract in _record_commit_attempt source.

    Structural tests — inspect source to verify the synthesis step is wired
    correctly without needing a full git worktree or live LLM calls.
    """

    def test_synthesis_called_on_blocked_with_findings(self):
        """Module-level code after update_state must call synthesize_to_canonical_issues."""
        import inspect
        from ouroboros.tools import commit_gate
        source = inspect.getsource(commit_gate._record_commit_attempt)
        assert "synthesize_to_canonical_issues" in source, (
            "_record_commit_attempt must call synthesize_to_canonical_issues"
        )
        assert 'status == "blocked" and critical_findings' in source, (
            "Synthesis must be gated on status='blocked' with non-empty findings"
        )

    def test_synthesis_import_path_correct(self):
        """Synthesis must be imported from review_synthesis module."""
        import inspect
        from ouroboros.tools import commit_gate
        source = inspect.getsource(commit_gate._record_commit_attempt)
        assert "from ouroboros.tools.review_synthesis import synthesize_to_canonical_issues" in source

    def test_synthesis_fail_open_on_exception(self):
        """Synthesis step must be wrapped in try/except for fail-open behavior."""
        import inspect
        from ouroboros.tools import commit_gate
        source = inspect.getsource(commit_gate._record_commit_attempt)
        assert "except Exception as _synth_exc" in source, (
            "Synthesis step must be wrapped in try/except for fail-open behavior"
        )

    def test_synthesis_gated_on_blocked_only(self):
        """Synthesis runs ONLY when status='blocked' AND findings non-empty."""
        import inspect
        from ouroboros.tools import commit_gate
        source = inspect.getsource(commit_gate._record_commit_attempt)
        assert 'status == "blocked" and critical_findings' in source

    def test_synthesis_outside_state_lock(self):
        """Synthesis must run BEFORE update_state() so that the single
        update_state call persists synthesized (or fallback-original) findings,
        and no remote I/O occurs while the review-state file lock is held."""
        import inspect
        from ouroboros.tools import commit_gate
        source = inspect.getsource(commit_gate._record_commit_attempt)
        # synthesize_to_canonical_issues must appear before update_state(dr, _mutate)
        update_pos = source.find("update_state(dr, _mutate)")
        synth_pos = source.find("synthesize_to_canonical_issues")
        assert update_pos != -1 and synth_pos != -1
        assert synth_pos < update_pos, (
            "synthesize_to_canonical_issues must be called BEFORE update_state(dr, _mutate) "
            "so the single update_state call persists the synthesized findings "
            "and no remote I/O occurs while the review-state file lock is held"
        )
        # Assert exactly ONE `update_state(dr, _mutate` occurrence (no second call).
        assert source.count("update_state(dr, _mutate") == 1, (
            "_record_commit_attempt must make exactly ONE update_state call with _mutate; "
            "the old post-synth `update_state(dr, _mutate_synth)` path has been removed."
        )

    def test_synthesis_uses_open_obligations(self):
        """Synthesis call must load open_obligations from durable state for id preservation."""
        import inspect
        from ouroboros.tools import commit_gate
        source = inspect.getsource(commit_gate._record_commit_attempt)
        assert "get_open_obligations" in source, (
            "Synthesis must pass open_obligations from durable state for obligation_id round-trip"
        )

    def test_synthesis_module_importable(self):
        """review_synthesis module must be importable without errors."""
        from ouroboros.tools import review_synthesis
        assert callable(review_synthesis.synthesize_to_canonical_issues)
        assert callable(review_synthesis._parse_synthesis_output)
        assert callable(review_synthesis._call_synthesis_llm)

    def test_synthesis_end_to_end_with_mocked_llm(self):
        """End-to-end: synthesis with mocked LLM produces deduplicated output."""
        canonical_response = json.dumps([{
            "item": "code_quality",
            "severity": "critical",
            "reason": "null deref in foo.py — canonical",
            "obligation_id": "",
            "evidence_from_reviewers": ["triad", "scope"],
        }])

        findings = [
            _make_finding("code_quality", "null deref in foo.py", tag="triad"),
            _make_finding("code_quality", "null pointer foo line 42", tag="scope"),
        ]

        with patch(
            "ouroboros.tools.review_synthesis._call_synthesis_llm",
            return_value=canonical_response,
        ):
            from ouroboros.tools.review_synthesis import synthesize_to_canonical_issues
            result = synthesize_to_canonical_issues(findings, open_obligations=[], ctx=None)

        assert len(result) == 1
        assert result[0]["item"] == "code_quality"
        assert "triad" in result[0]["evidence_from_reviewers"]
        assert "scope" in result[0]["evidence_from_reviewers"]

    def test_synthesis_runtime_synthesized_findings_persisted(self, tmp_path):
        """Runtime: when synthesis returns a deduplicated canonical list,
        ``_record_commit_attempt`` makes a SINGLE ``update_state`` call whose
        ``_mutate`` closure records the SYNTHESIZED findings (not the originals)
        on the new attempt.

        Patches ``ouroboros.review_state.update_state`` (the true source of the
        name; ``commit_gate`` imports it locally inside the function, so a
        patch on ``commit_gate.update_state`` would not intercept the lookup).
        """
        import types
        from ouroboros.tools.commit_gate import _record_commit_attempt

        update_state_mock = MagicMock()

        # load_state is called (a) in the synthesis block to fetch open
        # obligations and (b) in the continuation block; both get this stub.
        fake_state = MagicMock()
        fake_state.get_open_obligations.return_value = []
        fake_state.latest_attempt_for.return_value = None

        ctx = types.SimpleNamespace(
            drive_root=str(tmp_path),
            repo_dir=str(tmp_path),
            task_id="test-task",
            event_queue=None,
            pending_events=[],
            _current_review_attempt_number=1,
        )

        f1 = _make_finding("code_quality", "null deref in foo.py", tag="triad")
        f2 = _make_finding("code_quality", "null deref rephrased", tag="scope")
        synthesized = [_make_finding("code_quality", "canonical dedup", tag="triad")]

        with patch("ouroboros.review_state.update_state", update_state_mock), \
             patch("ouroboros.review_state.load_state", return_value=fake_state), \
             patch(
                 "ouroboros.tools.review_synthesis.synthesize_to_canonical_issues",
                 return_value=synthesized,
             ), \
             patch("ouroboros.task_continuation.save_review_continuation"), \
             patch("ouroboros.task_continuation.clear_review_continuation"):
            _record_commit_attempt(
                ctx,
                "test commit",
                status="blocked",
                critical_findings=[f1, f2],
            )

        # Single call = the one _mutate path, which now captures the
        # already-synthesized findings via the pre-lock synthesis step.
        assert update_state_mock.call_count == 1, (
            f"Expected 1 update_state call (synthesis happens BEFORE the single "
            f"update_state, so only one persist is needed), "
            f"got {update_state_mock.call_count}"
        )

        # Exercise the single _mutate closure to confirm it records the
        # synthesized findings on the new attempt.
        _mutate = update_state_mock.call_args_list[0][0][1]

        # Restore attempt number consumed by the end-of-function reset.
        ctx._current_review_attempt_number = 1

        recorded = []
        state_stub = MagicMock()
        state_stub.latest_attempt_for.return_value = None
        state_stub.next_attempt_number.return_value = 1
        state_stub.record_attempt.side_effect = lambda a: recorded.append(a)

        _mutate(state_stub)

        assert len(recorded) == 1
        assert recorded[0].critical_findings == synthesized, (
            "The single _mutate closure must record the SYNTHESIZED findings, "
            "not the raw originals."
        )

    def test_synthesis_runtime_fail_open_preserves_original(self, tmp_path):
        """Runtime: synthesis exception is caught; a SINGLE ``update_state`` call
        is made and its ``_mutate`` closure records the ORIGINAL findings
        unchanged (fail-open).
        """
        import types
        from ouroboros.tools.commit_gate import _record_commit_attempt

        update_state_mock = MagicMock()

        fake_state = MagicMock()
        fake_state.get_open_obligations.return_value = []
        fake_state.latest_attempt_for.return_value = None

        ctx = types.SimpleNamespace(
            drive_root=str(tmp_path),
            repo_dir=str(tmp_path),
            task_id="test-task",
            event_queue=None,
            pending_events=[],
            _current_review_attempt_number=1,
        )

        original = [
            _make_finding("code_quality", "bug A", tag="triad"),
            _make_finding("tests_affected", "missing test", tag="scope"),
        ]

        with patch("ouroboros.review_state.update_state", update_state_mock), \
             patch("ouroboros.review_state.load_state", return_value=fake_state), \
             patch(
                 "ouroboros.tools.review_synthesis.synthesize_to_canonical_issues",
                 side_effect=RuntimeError("synthesis crashed"),
             ), \
             patch("ouroboros.task_continuation.save_review_continuation"), \
             patch("ouroboros.task_continuation.clear_review_continuation"):
            _record_commit_attempt(
                ctx,
                "test commit",
                status="blocked",
                critical_findings=list(original),
            )

        # Synthesis exception is caught inside the try/except; the single
        # update_state call still fires and persists the original findings.
        assert update_state_mock.call_count == 1, (
            f"Expected 1 update_state call (synth failure → fall back to "
            f"originals → still one persist), "
            f"got {update_state_mock.call_count}"
        )

        # Exercise the single _mutate closure to confirm the original findings
        # were the critical_findings captured by the closure (fail-open).
        _mutate = update_state_mock.call_args_list[0][0][1]

        # Restore attempt number consumed by the end-of-function reset.
        ctx._current_review_attempt_number = 1

        recorded = []
        state_stub = MagicMock()
        state_stub.latest_attempt_for.return_value = None
        state_stub.next_attempt_number.return_value = 1
        state_stub.record_attempt.side_effect = lambda a: recorded.append(a)

        _mutate(state_stub)

        assert len(recorded) == 1
        assert recorded[0].critical_findings == original, (
            "When synthesis raises, the single _mutate closure must record the "
            "ORIGINAL findings unchanged (fail-open)."
        )
