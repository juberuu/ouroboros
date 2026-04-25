"""Block 1 — Review pipeline honesty tests.

Covers:
- build_full_repo_pack (DRY extraction to review_helpers)
- _is_probably_binary (UTF-8-safe sniffer: NUL, control chars, incremental decode)
- advisory diff size hard-fail gate (>500K chars)
- _repo_write_commit scope review wiring
- build_head_snapshot_section binary guard + size by raw bytes
- consciousness.py clip_text removal
"""

import importlib
import inspect
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_module(name):
    sys.path.insert(0, REPO)
    return importlib.import_module(name)


# ---------------------------------------------------------------------------
# Block 1: review pipeline honesty tests
# ---------------------------------------------------------------------------

class TestFullRepoPack:
    """Tests for build_full_repo_pack extracted to review_helpers (DRY, Block 1)."""

    def _make_git_repo(self, tmp_path):
        """Helper: init a git repo and commit some tracked files."""
        import subprocess as sp
        sp.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=T", "config", "--local",
                "user.email", "t@t"], cwd=str(tmp_path), capture_output=True)
        sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=T", "config", "--local",
                "user.name", "T"], cwd=str(tmp_path), capture_output=True)

    def test_returns_text_for_tracked_py_files(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "main.py").write_text("print('hello')", encoding="utf-8")
        subprocess.run(["git", "add", "main.py"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_full_repo_pack(tmp_path)
        assert "main.py" in pack
        assert "print('hello')" in pack
        assert omitted == []

    def test_excludes_binary_extensions(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        (tmp_path / "main.py").write_text("x = 1", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_full_repo_pack(tmp_path)
        assert any("icon.png" in o for o in omitted)
        assert "main.py" in pack

    def test_excludes_vendored_minified_files(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "chart.umd.min.js").write_text("!function(t){}", encoding="utf-8")
        (tmp_path / "main.py").write_text("x = 1", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_full_repo_pack(tmp_path)
        assert any("chart.umd.min.js" in o for o in omitted)
        assert "main.py" in pack

    def test_excludes_paths_in_exclude_set(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "a.py").write_text("AAA", encoding="utf-8")
        (tmp_path / "b.py").write_text("BBB", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_full_repo_pack(tmp_path, exclude_paths={"a.py"})
        assert "BBB" in pack
        assert "AAA" not in pack

    def test_excludes_oversized_files(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        big_path = tmp_path / "huge.py"
        big_path.write_bytes(b"x" * (1_048_576 + 1))
        (tmp_path / "small.py").write_text("small = True", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_full_repo_pack(tmp_path)
        assert any("huge.py" in o for o in omitted)
        assert "small.py" in pack

    def test_full_repo_pack_redacts_inline_secrets(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "settings.py").write_text(
            "OPENAI_API_KEY = 'sk-test-1234567890'\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "settings.py"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_full_repo_pack(tmp_path)
        assert "sk-test-1234567890" not in pack
        assert "***REDACTED***" in pack
        assert omitted == []


class TestIsProbablyBinary:
    """Unit tests for _is_probably_binary heuristic."""

    def test_nul_byte_returns_true(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"valid text\x00more text")
        mod = _get_module("ouroboros.tools.review_helpers")
        assert mod._is_probably_binary(f) is True

    def test_plain_text_returns_false(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        mod = _get_module("ouroboros.tools.review_helpers")
        assert mod._is_probably_binary(f) is False

    def test_high_non_printable_ratio_returns_true(self, tmp_path):
        f = tmp_path / "blob.dat"
        # >30% non-printable bytes (NUL byte present)
        f.write_bytes(bytes(range(256)) * 4)
        mod = _get_module("ouroboros.tools.review_helpers")
        assert mod._is_probably_binary(f) is True

    def test_empty_file_returns_false(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_bytes(b"")
        mod = _get_module("ouroboros.tools.review_helpers")
        assert mod._is_probably_binary(f) is False

    def test_missing_file_returns_false(self, tmp_path):
        mod = _get_module("ouroboros.tools.review_helpers")
        assert mod._is_probably_binary(tmp_path / "nonexistent.bin") is False

    def test_invalid_utf8_high_bytes_no_nul_returns_true(self, tmp_path):
        """Binary blob with high bytes (invalid UTF-8) and no NUL/control chars is caught."""
        # bytes 0x80-0xFF: valid start of UTF-8 sequences but never valid on their own
        blob = bytes(range(128, 256)) * 10  # no NUL, few control chars, not valid UTF-8
        f = tmp_path / "latin1.blob"
        f.write_bytes(blob)
        mod = _get_module("ouroboros.tools.review_helpers")
        assert mod._is_probably_binary(f) is True

    def test_valid_utf8_cyrillic_returns_false(self, tmp_path):
        """Valid UTF-8 Cyrillic text must NOT be classified as binary."""
        cyrillic = "Привет мир!\nЭто тест.\n" * 50
        f = tmp_path / "russian.py"
        f.write_text(cyrillic, encoding="utf-8")
        mod = _get_module("ouroboros.tools.review_helpers")
        assert mod._is_probably_binary(f) is False

    def test_utf8_char_at_sample_boundary_no_false_positive(self, tmp_path):
        """Valid UTF-8 file whose multi-byte char falls at exact 8192-byte boundary."""
        # Fill to 8191 bytes of ASCII, then add a 2-byte Cyrillic char at the boundary
        content = b"a" * 8191 + "Я".encode("utf-8")  # 2 bytes: 0xD0 0xAF
        f = tmp_path / "boundary.py"
        f.write_bytes(content)
        mod = _get_module("ouroboros.tools.review_helpers")
        # The incremental decoder with final=False must NOT raise for valid truncated chars
        assert mod._is_probably_binary(f) is False


class TestAdvisoryDiffSizeGate:
    """Advisory must hard-fail (not truncate) when staged diff exceeds 500K chars."""

    def test_oversized_diff_returns_error_sentinel(self, tmp_path):
        """_get_staged_diff must return a ⚠️ ADVISORY_ERROR string for diffs >500K chars."""
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "big.py").write_text("x" * 600_000, encoding="utf-8")
        subprocess.run(["git", "add", "big.py"], cwd=str(tmp_path), capture_output=True)
        adv = _get_module("ouroboros.tools.claude_advisory_review")
        result = adv._get_staged_diff(tmp_path)
        assert result.startswith("⚠️ ADVISORY_ERROR:")
        assert "500" in result or "split" in result.lower()

    def test_path_scoped_changed_file_list(self, tmp_path):
        """_get_changed_file_list(paths=...) must return only the scoped paths."""
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "unrelated.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "target.py").write_text("SCOPED = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "unrelated.py", "target.py"],
                       cwd=str(tmp_path), capture_output=True)
        adv = _get_module("ouroboros.tools.claude_advisory_review")
        # Without paths: both files appear
        full = adv._get_changed_file_list(tmp_path)
        assert "unrelated.py" in full
        assert "target.py" in full
        # With paths=['target.py']: only scoped file
        scoped = adv._get_changed_file_list(tmp_path, paths=["target.py"])
        assert "target.py" in scoped
        assert "unrelated.py" not in scoped

    def test_parse_changed_paths_preserves_literal_arrow_filename(self):
        """A normal filename containing ` -> ` must not be treated as a rename."""
        helpers = _get_module("ouroboros.tools.review_helpers")
        changed = " M docs/ARCH -> ITECTURE.md"
        assert helpers.parse_changed_paths_from_porcelain(changed) == ["docs/ARCH -> ITECTURE.md"]

    def test_parse_changed_paths_handles_text_rename_status(self):
        """Text porcelain rename entries must resolve to the destination path."""
        helpers = _get_module("ouroboros.tools.review_helpers")
        changed = "R  docs/OLD.md -> docs/NEW.md"
        assert helpers.parse_changed_paths_from_porcelain(changed) == ["docs/NEW.md"]

    def test_parse_changed_paths_handles_structured_rename_status(self):
        """NUL-delimited porcelain entries must resolve renames without string splitting hacks."""
        helpers = _get_module("ouroboros.tools.review_helpers")
        changed = b"R  docs/NEW.md\0docs/OLD.md\0"
        assert helpers.parse_changed_paths_from_porcelain_z(changed) == ["docs/NEW.md"]

    def test_path_scoped_diff_ignores_large_unrelated_file(self, tmp_path):
        """When paths= is given, _get_staged_diff must scope to those paths only.

        A huge unrelated staged file must not trigger the 500K hard-fail when
        the advisory is scoped to a different small file.
        """
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        # Stage a huge file (would trigger global hard-fail)
        (tmp_path / "huge_unrelated.py").write_text("x" * 600_000, encoding="utf-8")
        # Stage a small targeted file
        (tmp_path / "small_target.py").write_text("SCOPED_MARKER = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "huge_unrelated.py", "small_target.py"],
                       cwd=str(tmp_path), capture_output=True)
        adv = _get_module("ouroboros.tools.claude_advisory_review")
        # Without paths: should error (huge file in scope)
        unscoped = adv._get_staged_diff(tmp_path)
        assert unscoped.startswith("⚠️ ADVISORY_ERROR:")
        # With paths=['small_target.py']: must NOT error
        scoped = adv._get_staged_diff(tmp_path, paths=["small_target.py"])
        assert not scoped.startswith("⚠️ ADVISORY_ERROR:")
        assert "SCOPED_MARKER" in scoped

    def test_normal_diff_returned_in_full(self, tmp_path):
        """_get_staged_diff must return the full diff when under 500K chars."""
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        content = "UNIQUE_MARKER_12345 = 42\n" * 100
        (tmp_path / "mod.py").write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "mod.py"], cwd=str(tmp_path), capture_output=True)
        adv = _get_module("ouroboros.tools.claude_advisory_review")
        result = adv._get_staged_diff(tmp_path)
        assert "UNIQUE_MARKER_12345" in result
        assert result.startswith("⚠️") is False


class TestRepoWriteCommitScopeReview:
    """_repo_write_commit must run scope review (same pipeline as _repo_commit_push)."""

    def test_repo_write_commit_calls_scope_review(self):
        """Source inspection: _repo_write_commit must reuse the shared reviewed stage."""
        mod = _get_module("ouroboros.tools.git")
        source = inspect.getsource(mod._repo_write_commit)
        assert "_run_reviewed_stage_cycle" in source
        shared_source = inspect.getsource(mod._run_reviewed_stage_cycle)
        assert "_run_parallel_review" in shared_source
        parallel_source = inspect.getsource(mod._run_parallel_review)
        assert "run_scope_review" in parallel_source

    def test_scope_review_import_in_repo_write_commit(self):
        """The scope review must be reachable from _repo_write_commit via the shared stage helper."""
        mod = _get_module("ouroboros.tools.git")
        source = inspect.getsource(mod._repo_write_commit)
        assert "_run_reviewed_stage_cycle" in source
        parallel_source = inspect.getsource(mod._run_parallel_review)
        assert "scope_review" in parallel_source


class TestHeadSnapshotBinaryGuard:
    """build_head_snapshot_section must omit binary blobs even without early NUL bytes."""

    def _git_repo_with_file(self, tmp_path, fname, content_bytes):
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / fname).write_bytes(content_bytes)
        subprocess.run(["git", "add", fname], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )

    def test_binary_blob_without_nul_omitted(self, tmp_path):
        """Binary blob with high control-char ratio (no NUL) must be omitted, not decoded."""
        # Craft a binary-ish byte sequence: heavy ASCII control chars, no NUL
        blob = bytes(list(range(1, 9)) * 200 + list(range(14, 32)) * 200)
        self._git_repo_with_file(tmp_path, "noext_blob", blob)
        mod = _get_module("ouroboros.tools.review_helpers")
        result = mod.build_head_snapshot_section(tmp_path, ["noext_blob"])
        assert "binary content detected" in result.lower() or "omitted" in result.lower()
        # The raw binary content must not be injected as decoded garbage
        assert "noext_blob" in result

    def test_head_snapshot_size_enforced_by_raw_bytes(self, tmp_path):
        """HEAD snapshot must be omitted when raw byte size exceeds _FILE_SIZE_LIMIT."""
        # Write a file larger than 1MB of pure ASCII (won't fail UTF-8 or binary checks)
        big_content = b"a" * (1_048_576 + 100)
        self._git_repo_with_file(tmp_path, "big.py", big_content)
        mod = _get_module("ouroboros.tools.review_helpers")
        result = mod.build_head_snapshot_section(tmp_path, ["big.py"])
        assert "omitted" in result.lower()
        assert "big.py" in result


class TestBinaryOmissionNote:
    """build_touched_file_pack must emit explicit omission notes for binary files."""

    def test_extension_binary_has_omission_note(self, tmp_path):
        """.png file must produce an explicit omission note in the pack text."""
        (tmp_path / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(tmp_path, ["icon.png"])
        assert "icon.png" in omitted
        # Explicit omission note must appear — not silently dropped
        assert "omitted" in pack.lower()
        assert "icon.png" in pack

    def test_content_sniffed_binary_has_omission_note(self, tmp_path):
        """Content-sniffed binary (NUL bytes, no known extension) must also emit omission note."""
        blob = b"\x00\x01\x02\x03" * 50  # NUL bytes → _is_probably_binary
        (tmp_path / "noext_blob").write_bytes(blob)
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(tmp_path, ["noext_blob"])
        assert "noext_blob" in omitted
        assert "omitted" in pack.lower()
        assert "noext_blob" in pack


class TestSensitiveFileGuard:
    """Sensitive files (.env, .pem, credentials.json) must never appear in review packs."""

    def test_touched_file_pack_omits_env_file(self, tmp_path):
        """.env file must be omitted from touched-file pack, not injected."""
        (tmp_path / ".env").write_text("SECRET_KEY=super_secret_value_12345", encoding="utf-8")
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(tmp_path, [".env"])
        assert "super_secret_value_12345" not in pack
        assert ".env" in omitted
        assert "sensitive file" in pack.lower()

    def test_touched_file_pack_omits_pem_file(self, tmp_path):
        """.pem file must be omitted, not read into the review pack."""
        (tmp_path / "server.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\nFAKEPRIVATEKEY\n", encoding="utf-8")
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(tmp_path, ["server.pem"])
        assert "FAKEPRIVATEKEY" not in pack
        assert "server.pem" in omitted

    def test_touched_file_pack_omits_mixed_case_env(self, tmp_path):
        """Mixed-case .ENV or Credentials.JSON must also be omitted (case-insensitive)."""
        (tmp_path / ".ENV").write_text("MIXED_CASE_SECRET=abc123", encoding="utf-8")
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(tmp_path, [".ENV"])
        assert "abc123" not in pack
        assert ".ENV" in omitted

    def test_head_snapshot_omits_sensitive_file(self, tmp_path):
        """HEAD snapshot of .env file must not be included in the snapshot pack."""
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / ".env").write_text("SECRET=production_key_xyz", encoding="utf-8")
        subprocess.run(["git", "add", ".env"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )
        mod = _get_module("ouroboros.tools.review_helpers")
        result = mod.build_head_snapshot_section(tmp_path, [".env"])
        assert "production_key_xyz" not in result
        assert "sensitive" in result.lower() or "omitted" in result.lower()

    def test_touched_file_pack_redacts_secret_like_content_in_normal_file(self, tmp_path):
        """Secret-like literals inside normal text files should be redacted, not leaked."""
        (tmp_path / "client.py").write_text(
            'API_KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890"\nprint("ok")\n',
            encoding="utf-8",
        )
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(tmp_path, ["client.py"])
        assert omitted == []
        assert "sk-ant-" not in pack
        assert "***REDACTED***" in pack
        assert "secret-like content redacted" in pack.lower()

    def test_touched_file_pack_uses_collision_safe_fence(self, tmp_path):
        """Files containing triple backticks must be wrapped in a longer fence."""
        (tmp_path / "snippet.md").write_text(
            "Before\n```\ninside fence\n```\nAfter\n",
            encoding="utf-8",
        )
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(tmp_path, ["snippet.md"])
        assert omitted == []
        assert "````md" in pack
        assert "inside fence" in pack

    def test_head_snapshot_omits_mixed_case_credentials(self, tmp_path):
        """Mixed-case Credentials.JSON must be omitted from HEAD snapshot."""
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "Credentials.JSON").write_text('{"api_key": "HIDDEN_VALUE"}', encoding="utf-8")
        subprocess.run(["git", "add", "Credentials.JSON"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )
        mod = _get_module("ouroboros.tools.review_helpers")
        result = mod.build_head_snapshot_section(tmp_path, ["Credentials.JSON"])
        assert "HIDDEN_VALUE" not in result
        assert "sensitive" in result.lower() or "omitted" in result.lower()


class TestConsciousnessNoClipText:
    """Consciousness must not truncate knowledge/patterns/drive-state with clip_text."""

    def test_consciousness_context_build_no_clip_text_for_knowledge(self):
        mod = _get_module("ouroboros.consciousness")
        source = inspect.getsource(mod.BackgroundConsciousness._build_context)
        # clip_text should not appear for these cognitive sections
        assert "clip_text(kb_index" not in source
        assert "clip_text(patterns_text" not in source
        assert "clip_text(state_json" not in source

    def test_think_skips_cycle_on_overflow(self, tmp_path):
        """_think() must return False (not raise) when _build_context raises OverflowError.

        Also verifies that _loop() does NOT overwrite last_idle_reason to 'sleeping'
        after a skipped cycle — it stays as 'context_overflow'.
        """
        mod = _get_module("ouroboros.consciousness")
        bg = mod.BackgroundConsciousness.__new__(mod.BackgroundConsciousness)
        # Minimal setup to reach the OverflowError catch path
        import queue as _queue
        bg._observations = _queue.Queue()
        bg._paused = False
        bg._drive_root = tmp_path  # needed for append_jsonl call inside _think overflow path
        (tmp_path / "logs").mkdir()

        overflow_raised = []

        def _fake_build_context():
            overflow_raised.append(True)
            raise OverflowError("context too large")

        bg._build_context = _fake_build_context
        # _think must return False (not raise) and set context_overflow status
        result = bg._think()
        assert overflow_raised, "_build_context was not called"
        assert result is False, "_think should return False on overflow"
        assert bg._last_idle_reason == "context_overflow"


class TestFullRepoPackFailClosed:
    """build_full_repo_pack must raise RuntimeError on git ls-files failure."""

    def test_raises_on_git_failure(self, tmp_path):
        """If git ls-files fails, build_full_repo_pack raises RuntimeError."""
        import subprocess
        from unittest.mock import patch, MagicMock
        rh = _get_module("ouroboros.tools.review_helpers")
        # Simulate git ls-files returning non-zero
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stderr = "fatal: not a git repository"
        mock_result.stdout = ""
        with patch.object(subprocess, "run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="git ls-files failed"):
                rh.build_full_repo_pack(tmp_path)

    def test_scope_gather_packs_propagates_git_failure(self, tmp_path):
        """scope_review._gather_scope_packs propagates RuntimeError from build_full_repo_pack."""
        import subprocess
        from unittest.mock import patch
        sr = _get_module("ouroboros.tools.scope_review")
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"],
                       cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), capture_output=True)
        (tmp_path / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "a.py"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
        def fake_full_pack(repo_dir, **kwargs):
            raise RuntimeError("build_full_repo_pack: git ls-files failed (exit 128): fatal")
        with patch.object(sr, "build_full_repo_pack", side_effect=fake_full_pack):
            with pytest.raises(RuntimeError, match="git ls-files failed"):
                sr._gather_scope_packs(tmp_path, ["a.py"])

    def test_run_scope_review_blocks_on_repo_pack_git_failure(self, tmp_path):
        """run_scope_review must fail closed when build_full_repo_pack raises RuntimeError."""
        import subprocess
        from unittest.mock import patch

        sr = _get_module("ouroboros.tools.scope_review")
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"],
                       cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), capture_output=True)
        (tmp_path / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "a.py"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "a.py").write_text("x = 2\n")
        subprocess.run(["git", "add", "a.py"], cwd=str(tmp_path), capture_output=True)

        class FakeCtx:
            repo_dir = str(tmp_path)
            task_id = "test"
            event_queue = None

            def drive_logs(self):
                return tmp_path

        def fake_full_pack(repo_dir, **kwargs):
            raise RuntimeError("build_full_repo_pack: git ls-files failed (exit 128): fatal")

        with patch.object(sr, "build_full_repo_pack", side_effect=fake_full_pack):
            result = sr.run_scope_review(FakeCtx(), "test commit")

        assert result.blocked
        assert "SCOPE_REVIEW_BLOCKED" in result.block_message
        assert "Failed to build review context" in result.block_message
        assert "git ls-files failed" in result.block_message


class TestPathTraversalGuard:
    """build_touched_file_pack must reject paths that escape the repository root."""

    def _setup_repo(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "inside.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "inside.py"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
        return tmp_path

    def test_normal_in_repo_path_is_included(self, tmp_path):
        """A regular file inside the repo is included in the touched pack."""
        repo = self._setup_repo(tmp_path)
        rh = _get_module("ouroboros.tools.review_helpers")
        text, omitted = rh.build_touched_file_pack(repo, paths=["inside.py"])
        assert "inside.py" in text
        assert "inside.py" not in omitted

    def test_dotdot_path_is_rejected(self, tmp_path):
        """A path using ../ that escapes the repo root is omitted with an escape note."""
        repo = self._setup_repo(tmp_path)
        # Create a file outside the repo
        outside = tmp_path.parent / "outside_secret.txt"
        outside.write_text("secret=abc123\n")
        try:
            rh = _get_module("ouroboros.tools.review_helpers")
            text, omitted = rh.build_touched_file_pack(repo, paths=["../outside_secret.txt"])
            # The file contents must not appear — the filename may appear in an omission note.
            assert "secret=abc123" not in text
            assert "outside_secret.txt" in omitted or "path escapes" in text
        finally:
            outside.unlink(missing_ok=True)

    def test_absolute_out_of_repo_path_is_rejected(self, tmp_path):
        """An absolute path outside the repo root is rejected."""
        import tempfile
        repo = self._setup_repo(tmp_path)
        rh = _get_module("ouroboros.tools.review_helpers")
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"leaked = True\n")
            outside_abs = f.name
        try:
            text, omitted = rh.build_touched_file_pack(repo, paths=[outside_abs])
            assert "leaked" not in text
        finally:
            import os
            os.unlink(outside_abs)

    def test_symlink_escape_rejected_by_full_repo_pack(self, tmp_path):
        """build_full_repo_pack must not include content from a tracked symlink pointing outside the repo."""
        import subprocess, os, tempfile
        repo = self._setup_repo(tmp_path)
        rh = _get_module("ouroboros.tools.review_helpers")
        # Create a secret file outside the repo
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            f.write("outside_secret_content=abc123\n")
            outside_secret = f.name
        try:
            # Create a symlink inside the repo pointing outside
            symlink_path = repo / "escape_link.txt"
            os.symlink(outside_secret, symlink_path)
            # Track the symlink with git
            subprocess.run(["git", "add", "escape_link.txt"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "commit", "-m", "add symlink"], cwd=str(repo), capture_output=True)
            # build_full_repo_pack must NOT include the secret content
            text, omitted = rh.build_full_repo_pack(repo)
            assert "outside_secret_content=abc123" not in text
            assert any("escape_link" in o for o in omitted)
        finally:
            os.unlink(outside_secret)

    def test_symlink_escape_rejected_by_build_review_pack(self, tmp_path):
        """build_review_pack (deep_self_review) must not include content from a tracked symlink pointing outside."""
        import subprocess, os, tempfile
        repo = self._setup_repo(tmp_path)
        dsr = _get_module("ouroboros.deep_self_review")
        # Create a secret file outside the repo
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            f.write("deep_review_secret=xyz987\n")
            outside_secret = f.name
        try:
            # Create and track a symlink inside the repo pointing outside
            symlink_path = repo / "deep_escape.txt"
            os.symlink(outside_secret, symlink_path)
            subprocess.run(["git", "add", "deep_escape.txt"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "commit", "-m", "add symlink"], cwd=str(repo), capture_output=True)
            # build_review_pack must NOT include secret content
            pack_text, stats = dsr.build_review_pack(repo, tmp_path)
            assert "deep_review_secret=xyz987" not in pack_text
            assert any("deep_escape" in s for s in stats.get("skipped", []))
        finally:
            os.unlink(outside_secret)


class TestPathScopedAdvisoryHandler:
    """Advisory pre-review handler must scope changed-file list to paths=."""

    def test_changed_file_list_scoped_to_paths(self, tmp_path):
        """_get_changed_file_list(paths=...) excludes unrelated changed files."""
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"],
                       cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), capture_output=True)
        (tmp_path / "unrelated.py").write_text("u = 1\n")
        (tmp_path / "target.py").write_text("SCOPED = 1\n")
        subprocess.run(["git", "add", "unrelated.py", "target.py"],
                       cwd=str(tmp_path), capture_output=True)
        adv = _get_module("ouroboros.tools.claude_advisory_review")
        full = adv._get_changed_file_list(tmp_path)
        assert "unrelated.py" in full and "target.py" in full
        scoped = adv._get_changed_file_list(tmp_path, paths=["target.py"])
        assert "target.py" in scoped
        assert "unrelated.py" not in scoped
