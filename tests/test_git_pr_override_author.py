"""Tests for cherry_pick_pr_commits override_author parameter (v4.35.0).

Split out of tests/test_pr_tools.py to keep both modules under the 1600-line
hard gate (DEVELOPMENT.md). Covers:
  - author name/email rewrite + original author date preservation
  - committer identity preservation (stays Ouroboros)
  - default behavior regression guard (override_author=None)
  - batch uniformity across multi-author source commits
  - Co-authored-by hint references the ORIGINAL upstream author in override mode
  - table-driven validation rejects invalid shapes
  - validation runs before any git subprocess
  - commit --amend failure triggers HEAD~1 rollback
  - tool schema exposes override_author with correct type shape
"""

from __future__ import annotations

import pathlib
import subprocess
from unittest.mock import MagicMock, patch

# Re-use fixture helpers from the main PR test module. This avoids duplicating
# _make_temp_git_repo / _make_ctx / _add_commit across two files (DRY).
from tests.test_pr_tools import (  # noqa: F401 — imported for side-effect of path bootstrap
    _make_temp_git_repo,
    _make_ctx,
    _add_commit,
)


_OVERRIDE = {
    "name": "mr8bit",
    "email": "17060976+mr8bit@users.noreply.github.com",
}


def _setup_pr_commits(repo: pathlib.Path) -> list:
    """Add a 'pr/99' local ref with 2 commits by an external author.

    Mirror of TestCherryPickCommits._setup_pr_commits in test_pr_tools.py —
    kept here so this module is independently runnable.
    """
    subprocess.run(["git", "checkout", "-b", "pr/99"], cwd=repo,
                   check=True, capture_output=True)
    sha1 = _add_commit(repo, "feature_a.py", "def a(): pass\n", "feat: add feature_a")
    sha2 = _add_commit(repo, "feature_b.py", "def b(): pass\n", "feat: add feature_b")
    subprocess.run(["git", "checkout", "ouroboros"], cwd=repo,
                   check=True, capture_output=True)
    return [sha1, sha2]


# ---------------------------------------------------------------------------
# Behavioral tests
# ---------------------------------------------------------------------------

class TestOverrideAuthor:

    def test_rewrites_author_and_email(self, tmp_path):
        """override_author rewrites %an and %ae on every cherry-picked commit."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = _setup_pr_commits(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=shas, override_author=_OVERRIDE)
        assert "✅" in result, f"Expected success: {result}"

        # Both cherry-picked commits must have the override author
        log_names = subprocess.run(
            ["git", "log", "--format=%an", "-2"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip().splitlines()
        log_emails = subprocess.run(
            ["git", "log", "--format=%ae", "-2"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip().splitlines()

        assert all(n == _OVERRIDE["name"] for n in log_names), \
            f"Expected all authors to be override name, got: {log_names}"
        assert all(e == _OVERRIDE["email"] for e in log_emails), \
            f"Expected all authors to be override email, got: {log_emails}"

    def test_preserves_original_author_date(self, tmp_path):
        """Critical: override_author preserves the original author DATE.

        git commit --amend --author=... resets the author date to now by default.
        We pass --date=<original> explicitly so timestamps survive the override.
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = _setup_pr_commits(repo)

        # Capture original author dates from the source commits (on pr/99)
        original_dates = {}
        for sha in shas:
            d = subprocess.run(
                ["git", "log", "-1", "--format=%aI", sha],
                cwd=repo, capture_output=True, text=True,
            ).stdout.strip()
            original_dates[sha] = d
            assert d, f"Test setup issue: no date for {sha}"

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=shas, override_author=_OVERRIDE)
        assert "✅" in result, f"Expected success: {result}"

        # After cherry-pick + amend, new HEAD commits must share original author dates.
        # shas was [sha1, sha2], so HEAD is sha2's replay, HEAD~1 is sha1's replay.
        new_dates = subprocess.run(
            ["git", "log", "--format=%aI", "-2"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip().splitlines()
        assert new_dates[0] == original_dates[shas[1]], \
            f"HEAD date {new_dates[0]} != original {original_dates[shas[1]]}"
        assert new_dates[1] == original_dates[shas[0]], \
            f"HEAD~1 date {new_dates[1]} != original {original_dates[shas[0]]}"

    def test_preserves_committer_identity(self, tmp_path):
        """override_author must NOT change the committer identity.

        Author = override (mr8bit)
        Committer = repo's user.email (test@ouroboros from _make_temp_git_repo)
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = _setup_pr_commits(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=shas, override_author=_OVERRIDE)
        assert "✅" in result

        # Committer must stay as repo-configured Ouroboros identity
        committer_emails = subprocess.run(
            ["git", "log", "--format=%ce", "-2"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip().splitlines()
        assert all(e == "test@ouroboros" for e in committer_emails), \
            f"Committer should remain repo-configured identity, got: {committer_emails}"
        # Author is override
        author_emails = subprocess.run(
            ["git", "log", "--format=%ae", "-2"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip().splitlines()
        assert all(e == _OVERRIDE["email"] for e in author_emails)

    def test_none_preserves_original_behavior(self, tmp_path):
        """override_author=None keeps byte-identical default behavior.

        Regression guard: the default path must not call commit --amend.
        Also verifies the success message uses the "original authorship preserved"
        description only on the default path.
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = _setup_pr_commits(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        # Call WITHOUT override_author (default)
        result = _cherry_pick_pr_commits(ctx, shas=shas)
        assert "✅" in result

        # Author should be the external contributor (preserved by default path)
        author_emails = subprocess.run(
            ["git", "log", "--format=%ae", "-2"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip().splitlines()
        assert all(e == "ext@example.com" for e in author_emails), \
            f"Default path must preserve original author, got: {author_emails}"
        # Result message must NOT mention override (default path)
        assert "Author override applied" not in result
        # Default-path success message carries the "original authorship preserved" tag
        assert "original authorship preserved" in result

    def test_applied_to_all_commits_in_batch(self, tmp_path):
        """Override applies uniformly to every commit in the batch, not just first."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        # Create 3 commits with different original authors to prove the override
        # rewrites ALL of them, not just one.
        subprocess.run(["git", "checkout", "-b", "pr/88"], cwd=repo,
                       check=True, capture_output=True)
        sha1 = _add_commit(repo, "a.py", "a\n", "a",
                           author_name="Dev One", author_email="one@x.com")
        sha2 = _add_commit(repo, "b.py", "b\n", "b",
                           author_name="Dev Two", author_email="two@x.com")
        sha3 = _add_commit(repo, "c.py", "c\n", "c",
                           author_name="Dev Three", author_email="three@x.com")
        subprocess.run(["git", "checkout", "ouroboros"], cwd=repo,
                       check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "integrate/pr-88"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(
            ctx, shas=[sha1, sha2, sha3], override_author=_OVERRIDE,
        )
        assert "✅" in result

        # All 3 cherry-picked commits must share the override email
        emails = subprocess.run(
            ["git", "log", "--format=%ae", "-3"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip().splitlines()
        assert emails == [_OVERRIDE["email"]] * 3, \
            f"All batch commits must have override email, got: {emails}"

    def test_override_mode_coauthored_by_references_original_author(self, tmp_path):
        """Co-authored-by in override mode references the ORIGINAL upstream author.

        Semantics: the override identity is already the commit author after
        `git commit --amend`, so putting the same identity in Co-authored-by
        would be redundant AND would erase the real upstream contributor from
        the merge-commit message. Instead, Co-authored-by cites the ORIGINAL
        author read from the source commit's `%an <%ae>` (the source SHA is
        unchanged by cherry-pick, so this still carries pre-override metadata).

        Also verifies the success message uses the override-aware description
        ("author identity rewritten") instead of "original authorship preserved".
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = _setup_pr_commits(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=shas, override_author=_OVERRIDE)

        # Extract only the Co-authored-by lines from the result. The override
        # note elsewhere in the result legitimately mentions the override
        # identity; Co-authored-by semantics are a scoped contract on the
        # attribution hint that lands in the final merge commit message.
        coauthored_lines = [
            line for line in result.splitlines()
            if line.startswith("Co-authored-by:")
        ]
        assert coauthored_lines, (
            f"Expected at least one Co-authored-by line in result: {result}"
        )
        coauthored_blob = "\n".join(coauthored_lines)

        # Co-authored-by must cite ORIGINAL upstream author (preserves credit
        # to the real contributor in the merge commit message).
        assert "ext@example.com" in coauthored_blob, (
            f"Original author email must appear in Co-authored-by lines: {coauthored_blob!r}"
        )
        # Override identity is already the commit author — it must NOT also
        # appear in Co-authored-by (would be redundant and erase upstream).
        assert _OVERRIDE["email"] not in coauthored_blob, (
            f"Override email must NOT appear in Co-authored-by lines "
            f"(it's already the commit author): {coauthored_blob!r}"
        )
        # Explicit override announcement still fires on the override path
        assert "Author override applied" in result
        # Override-aware success message (not the default-path "original authorship preserved")
        assert "author identity rewritten" in result, \
            f"Override path must use override-aware success message: {result}"
        assert "original authorship preserved" not in result, \
            f"Override path must not claim 'original authorship preserved': {result}"

    def test_validates_dict_shape_rejects_invalid(self, tmp_path):
        """Table-driven validation: all invalid shapes must produce ⚠️ CHERRY_PICK_ERROR."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = _setup_pr_commits(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits

        invalid_inputs = [
            {"name": "X"},                              # email missing
            {"email": "x@y.com"},                       # name missing
            {},                                         # both missing
            {"name": "", "email": "x@y.com"},           # empty name
            {"name": "X", "email": ""},                 # empty email
            {"name": "X", "email": "no-at-sign"},       # email without @
            {"name": 123, "email": "x@y.com"},          # non-str name
            {"name": "X", "email": 123},                # non-str email
            "not-a-dict",                               # wrong type
            42,                                         # wrong type
            ["list", "instead"],                        # wrong type
            {"name": "X\nInjection", "email": "x@y.com"},   # newline in name
            {"name": "X", "email": "x@y.com\r\nextra"},      # CR in email
            {"name": "X<script>", "email": "x@y.com"},       # '<' in name
            {"name": "X", "email": "x@y>.com"},              # '>' in email
        ]
        for bad in invalid_inputs:
            result = _cherry_pick_pr_commits(ctx, shas=shas, override_author=bad)
            assert "⚠️" in result and "CHERRY_PICK_ERROR" in result, \
                f"Expected error for {bad!r}, got: {result[:200]}"

    def test_validation_runs_before_git_subprocess(self, tmp_path):
        """Invalid override must return an error BEFORE any git cherry-pick is attempted."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = _setup_pr_commits(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools import git_pr

        with patch("ouroboros.tools.git_pr._g") as mock_g:
            # Return a harmless default so if any git call slips through
            # the function doesn't crash — we assert cherry-pick was NOT called.
            mock_g.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = git_pr._cherry_pick_pr_commits(
                ctx, shas=shas,
                override_author={"name": "X", "email": "bad-no-at"},
            )

        assert "⚠️" in result and "CHERRY_PICK_ERROR" in result
        # No git subprocess must have been invoked with "cherry-pick" argv[0]
        cherry_pick_calls = [
            call for call in mock_g.call_args_list
            if call.args and len(call.args) >= 1 and call.args[0]
            and call.args[0][0] == "cherry-pick"
        ]
        assert not cherry_pick_calls, (
            f"Invalid override must not call cherry-pick; got: {cherry_pick_calls}"
        )

    def test_rollback_on_amend_failure(self, tmp_path):
        """When git commit --amend fails, the just-created commit is rolled back.

        Uses a real temp git repo. We mock _g to succeed for every git command
        EXCEPT `commit --amend`, which fails. After the failure, the repo must
        have no new commits relative to the pre-call baseline, and the tree
        must be clean.
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = _setup_pr_commits(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        # Capture baseline: commit count on integration branch pre-call.
        base_count = int(subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip())

        from ouroboros.tools import git_pr
        real_g = git_pr._g

        def fake_g(args, cwd, env=None, timeout=60):
            # Make `commit --amend` fail; let every other git command run for real
            if args and args[0] == "commit" and "--amend" in args:
                return subprocess.CompletedProcess(
                    args=["git"] + args, returncode=1,
                    stdout="", stderr="fatal: simulated amend failure\n",
                )
            return real_g(args, cwd, env=env, timeout=timeout)

        with patch("ouroboros.tools.git_pr._g", side_effect=fake_g):
            result = git_pr._cherry_pick_pr_commits(
                ctx, shas=shas, override_author=_OVERRIDE,
            )

        # Must report the amend failure and the rollback
        assert "⚠️" in result
        assert ("amend failed" in result.lower()
                or "rolled back" in result.lower()), \
            f"Error must mention amend failure or rollback: {result}"

        # Repo must be back to the baseline — the cherry-picked commit rolled back
        final_count = int(subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip())
        assert final_count == base_count, (
            f"Rollback failed: commit count went {base_count} -> {final_count}"
        )

        # Worktree must be clean (no conflict markers, no leftover staged state)
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert status == "", f"Tree not clean after rollback: {status!r}"


# ---------------------------------------------------------------------------
# Tool-schema pin: cherry_pick_pr_commits exposes override_author
# ---------------------------------------------------------------------------

class TestToolSchema:

    def test_cherry_pick_tool_schema_exposes_override_author(self):
        """cherry_pick_pr_commits tool schema must expose override_author parameter
        with the documented shape (object with name+email string properties, not required)."""
        from ouroboros.tools.git_pr import get_tools
        entry = next(t for t in get_tools() if t.name == "cherry_pick_pr_commits")
        params = entry.schema["parameters"]
        props = params["properties"]

        assert "override_author" in props, (
            "override_author missing from cherry_pick_pr_commits schema — "
            "tool surface does not advertise the new capability"
        )
        override_schema = props["override_author"]
        assert override_schema["type"] == "object"
        assert "name" in override_schema["properties"]
        assert "email" in override_schema["properties"]
        assert override_schema["properties"]["name"]["type"] == "string"
        assert override_schema["properties"]["email"]["type"] == "string"
        # override_author is optional (not in top-level required list)
        assert "override_author" not in params.get("required", []), (
            "override_author must be optional, not required"
        )
        # Description must mention the override mechanics
        desc = entry.schema["description"].lower()
        assert "override_author" in desc
        assert "author date" in desc or "original author date" in desc


# ---------------------------------------------------------------------------
# _ouroboros_committer_env fallback regression guard
# ---------------------------------------------------------------------------

class TestShaTypeRejection:
    """git cat-file -t <sha> must return 'commit' — tags/trees/blobs rejected."""

    def test_rejects_tag_ref(self, tmp_path):
        """cherry_pick_pr_commits rejects annotated tag refs even though
        `rev-parse --verify <tag>^{commit}` would resolve them to a commit.
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = _setup_pr_commits(repo)

        # Create an annotated tag pointing to one of the cherry-pick-able commits
        subprocess.run(
            ["git", "tag", "-a", "v-for-test", "-m", "test tag", shas[0]],
            cwd=repo, check=True, capture_output=True,
        )

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        # Pass the tag name instead of the commit SHA
        result = _cherry_pick_pr_commits(ctx, shas=["v-for-test"])

        assert "⚠️" in result and "CHERRY_PICK_ERROR" in result, \
            f"Expected tag rejection, got: {result[:200]}"
        assert "not a commit" in result.lower() or "tag" in result.lower(), \
            f"Error message must identify tag as non-commit: {result[:200]}"

    def test_rejects_branch_name(self, tmp_path):
        """Branch names like 'main' or 'ouroboros' pass cat-file as 'commit'
        but are not SHAs — _SHA_PATTERN must reject them up front.
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = _setup_pr_commits(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        # Pass a branch name
        result = _cherry_pick_pr_commits(ctx, shas=["main"])
        assert "⚠️" in result and "CHERRY_PICK_ERROR" in result
        assert "not a commit SHA" in result.lower() or "hex" in result.lower(), \
            f"Branch name must be rejected as non-SHA: {result[:200]}"


    def test_rejects_lightweight_tag(self, tmp_path):
        """Lightweight tags point directly at commits (cat-file returns 'commit'),
        but they're still symbolic refs, not SHAs.
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = _setup_pr_commits(repo)

        # Lightweight tag (no -a, no -m): direct commit pointer
        subprocess.run(
            ["git", "tag", "lightweight-v1", shas[0]],
            cwd=repo, check=True, capture_output=True,
        )

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=["lightweight-v1"])
        assert "⚠️" in result and "CHERRY_PICK_ERROR" in result
        assert "not a commit SHA" in result.lower() or "hex" in result.lower(), \
            f"Lightweight tag must be rejected as non-SHA: {result[:200]}"


class TestCommitterEnvFallback:

    def test_missing_local_identity_falls_back_to_ouroboros_defaults(self, tmp_path):
        """_ouroboros_committer_env must NOT leak the developer's global git
        identity when a repo has no local user.name/user.email configured.

        Regression guard: previously read `git config user.name` without --local,
        which silently resolves to the global identity in a dev checkout.
        After the fix (v4.35.0), the function reads `--local` only and falls
        back to explicit Ouroboros defaults on miss.
        """
        repo = _make_temp_git_repo(tmp_path)
        # Remove repo-local identity so only global identity (if any) is left.
        # --unset is no-op safe if the key isn't set, so check=False.
        subprocess.run(
            ["git", "config", "--local", "--unset", "user.name"],
            cwd=repo, check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "--local", "--unset", "user.email"],
            cwd=repo, check=False, capture_output=True,
        )

        from ouroboros.tools.git_pr import _ouroboros_committer_env
        env = _ouroboros_committer_env(repo)

        assert env["GIT_COMMITTER_NAME"] == "Ouroboros", (
            f"Expected Ouroboros default, got {env['GIT_COMMITTER_NAME']!r} — "
            "function may be leaking the ambient global git identity"
        )
        assert env["GIT_COMMITTER_EMAIL"] == "ouroboros@local.mac", (
            f"Expected ouroboros@local.mac default, got {env['GIT_COMMITTER_EMAIL']!r} — "
            "function may be leaking the ambient global git identity"
        )

    def test_partial_local_identity_falls_back_atomically(self, tmp_path):
        """If ONLY user.name is set (no user.email), fall back atomically:
        both fields become the Ouroboros defaults, preventing a mixed identity
        like 'Alice <ouroboros@local.mac>'.
        """
        repo = _make_temp_git_repo(tmp_path)

        # Unset email but keep a name
        subprocess.run(["git", "config", "--local", "--unset", "user.email"],
                       cwd=repo, check=False, capture_output=True)
        subprocess.run(["git", "config", "--local", "user.name", "Alice"],
                       cwd=repo, check=True, capture_output=True)

        from ouroboros.tools.git_pr import _ouroboros_committer_env
        env = _ouroboros_committer_env(repo)

        assert env["GIT_COMMITTER_NAME"] == "Ouroboros", \
            f"Expected atomic fallback to Ouroboros, got name: {env['GIT_COMMITTER_NAME']!r}"
        assert env["GIT_COMMITTER_EMAIL"] == "ouroboros@local.mac", \
            f"Expected atomic fallback to Ouroboros, got email: {env['GIT_COMMITTER_EMAIL']!r}"
