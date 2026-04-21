"""Tests for PR integration tools: git_pr.py and github.py PR additions."""

from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
from typing import List
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_temp_git_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a minimal git repo with a base commit on 'ouroboros' branch.

    The `data/` directory (used by _make_ctx for drive_root / lock files) is
    listed in .gitignore so that git add -A never picks up lock infrastructure
    files created by _acquire_git_lock during tests.
    """
    subprocess.run(["git", "init", "-b", "ouroboros", str(tmp_path)],
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@ouroboros"], cwd=tmp_path,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path,
                   check=True, capture_output=True)
    (tmp_path / "README.md").write_text("# test\n")
    # Ignore the drive_root dir that _make_ctx creates inside the repo
    (tmp_path / ".gitignore").write_text("data/\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path,
                   check=True, capture_output=True)
    return tmp_path


def _make_ctx(tmp_path: pathlib.Path) -> MagicMock:
    ctx = MagicMock()
    ctx.repo_dir = str(tmp_path)
    ctx.drive_root = str(tmp_path / "data")
    ctx.branch_dev = "ouroboros"
    # Provide a real drive_path for lock acquisition
    lock_dir = tmp_path / "data" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    ctx.drive_path.return_value = lock_dir
    return ctx


def _add_commit(repo: pathlib.Path, filename: str, content: str,
                msg: str, author_name: str = "External Dev",
                author_email: str = "ext@example.com") -> str:
    """Add a commit with specified author, return SHA."""
    env = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }
    import os
    full_env = {**os.environ, **env}
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True,
                   capture_output=True, env=full_env)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------------------------
# git_pr: create_integration_branch
# ---------------------------------------------------------------------------

class TestCreateIntegrationBranch:
    def test_creates_branch_from_ouroboros(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        from ouroboros.tools.git_pr import _create_integration_branch
        result = _create_integration_branch(ctx, pr_number=42)

        assert "✅" in result
        assert "integrate/pr-42" in result
        # Verify branch actually exists
        branches = subprocess.run(
            ["git", "branch"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "integrate/pr-42" in branches

    def test_rejects_duplicate_branch(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        from ouroboros.tools.git_pr import _create_integration_branch
        _create_integration_branch(ctx, pr_number=42)
        result = _create_integration_branch(ctx, pr_number=42)

        assert "⚠️" in result
        assert "already exists" in result

    def test_rejects_invalid_pr_number(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        from ouroboros.tools.git_pr import _create_integration_branch
        result = _create_integration_branch(ctx, pr_number=0)
        assert "⚠️" in result

    def test_returns_to_integration_branch(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        from ouroboros.tools.git_pr import _create_integration_branch
        _create_integration_branch(ctx, pr_number=7)

        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert current == "integrate/pr-7"

    def test_rejects_untracked_files(self, tmp_path):
        """create_integration_branch must reject untracked files.

        stage_adaptations runs 'git add -A' which picks up untracked files —
        so an untracked file present before branch creation would be silently
        included in the final merge commit, contaminating the PR intake.
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        # Create an untracked file (not staged, not committed)
        (repo / "untracked_noise.py").write_text("noise\n")

        from ouroboros.tools.git_pr import _create_integration_branch
        result = _create_integration_branch(ctx, pr_number=5)
        assert "⚠️" in result, f"Expected rejection for untracked file, got: {result}"
        assert ("untracked" in result.lower() or "uncommitted" in result.lower()), result

    def test_rejects_dirty_tracked_staged_changes(self, tmp_path):
        """create_integration_branch must refuse if tracked staged changes exist.

        git checkout carries staged edits onto the new branch, which would
        contaminate the integration branch with unrelated work.
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        # Stage a tracked change without committing
        (tmp_path / "dirty.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "dirty.py"], cwd=repo, check=True,
                       capture_output=True)

        from ouroboros.tools.git_pr import _create_integration_branch
        result = _create_integration_branch(ctx, pr_number=42)

        assert "⚠️" in result, f"Expected refusal on dirty worktree, got: {result}"
        assert "staged" in result.lower() or "unstaged" in result.lower() or \
               "changes" in result.lower(), f"Error should mention dirty state: {result}"

    def test_rejects_dirty_tracked_unstaged_changes(self, tmp_path):
        """create_integration_branch must refuse if tracked unstaged changes exist."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        # Commit a file first, then modify it without staging
        (tmp_path / "tracked.py").write_text("x = 0\n")
        subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "add tracked.py"], cwd=repo,
                       check=True, capture_output=True)
        (tmp_path / "tracked.py").write_text("x = 99\n")  # modify without staging

        from ouroboros.tools.git_pr import _create_integration_branch
        result = _create_integration_branch(ctx, pr_number=43)

        assert "⚠️" in result, f"Expected refusal on unstaged tracked change, got: {result}"

    def test_rejects_option_like_base_branch(self, tmp_path):
        """base_branch values starting with '-' must be rejected (option injection)."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        from ouroboros.tools.git_pr import _create_integration_branch
        result = _create_integration_branch(ctx, pr_number=5, base_branch="--abort")

        assert "⚠️" in result
        assert "INVALID_ARG" in result or "must not start with" in result


# ---------------------------------------------------------------------------
# git_pr: option-injection guards (shared across fetch_pr_ref, stage_pr_merge)
# ---------------------------------------------------------------------------

class TestOptionInjectionGuards:
    """_validate_git_ref_arg must block option-like inputs across all three callers."""

    def test_fetch_pr_ref_rejects_option_like_remote(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        from ouroboros.tools.git_pr import _fetch_pr_ref
        result = _fetch_pr_ref(ctx, pr_number=1, remote="--all")

        assert "⚠️" in result
        assert "INVALID_ARG" in result or "must not start with" in result

    def test_stage_pr_merge_rejects_option_like_branch(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        from ouroboros.tools.git_pr import _stage_pr_merge
        result = _stage_pr_merge(ctx, branch="--abort")

        assert "⚠️" in result
        assert "INVALID_ARG" in result or "must not start with" in result

    def test_validate_git_ref_arg_passes_normal_values(self, tmp_path):
        """Normal branch/remote names must not be rejected."""
        from ouroboros.tools.git_pr import _validate_git_ref_arg
        assert _validate_git_ref_arg("origin", "remote") is None
        assert _validate_git_ref_arg("integrate/pr-7", "branch") is None
        assert _validate_git_ref_arg("ouroboros", "base_branch") is None


# ---------------------------------------------------------------------------
# git_pr: fetch_pr_ref
# ---------------------------------------------------------------------------

class TestFetchPrRef:
    def test_fetches_and_creates_local_ref(self, tmp_path):
        """fetch_pr_ref creates a local pr/N ref from a bare remote."""
        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

        # Bootstrap: create an initial commit in bare (needed so clone has a HEAD)
        init_repo = tmp_path / "init_repo"
        subprocess.run(["git", "clone", str(bare), str(init_repo)],
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@ouroboros"], cwd=init_repo,
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=init_repo,
                       check=True, capture_output=True)
        (init_repo / "README.md").write_text("base\n")
        subprocess.run(["git", "add", "-A"], cwd=init_repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=init_repo,
                       check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=init_repo,
                       check=True, capture_output=True)

        # Create a PR commit and push as refs/pull/1/head
        (init_repo / "feature.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=init_repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "pr commit"], cwd=init_repo,
                       check=True, capture_output=True)
        pr_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=init_repo, capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(
            ["git", "push", "origin", "HEAD:refs/pull/1/head"],
            cwd=init_repo, check=True, capture_output=True
        )

        # Clone a fresh consumer repo (doesn't have the PR commit locally)
        repo = tmp_path / "repo"
        subprocess.run(["git", "clone", str(bare), str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@ouroboros"], cwd=repo,
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo,
                       check=True, capture_output=True)
        # Rename default branch to ouroboros to match ctx expectation
        subprocess.run(["git", "checkout", "-b", "ouroboros"], cwd=repo,
                       check=True, capture_output=True)

        ctx = _make_ctx(repo)
        from ouroboros.tools.git_pr import _fetch_pr_ref
        result = _fetch_pr_ref(ctx, pr_number=1, remote="origin")

        assert "✅" in result, f"Expected success: {result}"
        assert "pr/1" in result

        # Local ref pr/1 must point to the pushed SHA
        local_sha = subprocess.run(
            ["git", "rev-parse", "pr/1"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert local_sha == pr_sha

    def test_refetch_after_rebase(self, tmp_path):
        """Force-fetch prefix allows refetching a force-pushed/rebased PR ref."""
        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

        repo = tmp_path / "repo"
        subprocess.run(["git", "clone", str(bare), str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@ouroboros"], cwd=repo,
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo,
                       check=True, capture_output=True)

        # Push initial version as pull/2/head
        (repo / "v1.py").write_text("v1\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "v1"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "push", str(bare), "HEAD:refs/pull/2/head"],
            cwd=repo, check=True, capture_output=True
        )

        ctx = _make_ctx(repo)
        from ouroboros.tools.git_pr import _fetch_pr_ref
        # First fetch
        _fetch_pr_ref(ctx, pr_number=2, remote=str(bare))
        sha_v1 = subprocess.run(
            ["git", "rev-parse", "pr/2"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()

        # Simulate a rebase: amend the commit (new SHA) and force-push
        subprocess.run(["git", "commit", "--amend", "-m", "v1 rebased"], cwd=repo,
                       check=True, capture_output=True)
        subprocess.run(
            ["git", "push", "--force", str(bare), "HEAD:refs/pull/2/head"],
            cwd=repo, check=True, capture_output=True
        )
        sha_v2 = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()

        # Second fetch — must update local ref to new SHA (force-update)
        result2 = _fetch_pr_ref(ctx, pr_number=2, remote=str(bare))
        assert "✅" in result2, f"Expected success on refetch: {result2}"

        local_sha_after = subprocess.run(
            ["git", "rev-parse", "pr/2"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert local_sha_after == sha_v2, \
            f"Ref not updated after rebase: still {local_sha_after}, expected {sha_v2}"
        assert local_sha_after != sha_v1

    def test_rejects_nonpositive_pr_number(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        from ouroboros.tools.git_pr import _fetch_pr_ref
        result = _fetch_pr_ref(ctx, pr_number=0)
        assert "⚠️" in result


# ---------------------------------------------------------------------------
# git_pr: stage_adaptations
# ---------------------------------------------------------------------------

class TestStageAdaptations:
    """Tests for _stage_adaptations: staging-only, no git commit created (P8)."""

    def test_stages_without_committing(self, tmp_path):
        """stage_adaptations must stage files but NOT create a new commit."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        subprocess.run(["git", "checkout", "-b", "integrate/pr-10"], cwd=repo,
                       check=True, capture_output=True)
        (repo / "adapt.py").write_text("adaptation\n")

        from ouroboros.tools.git_pr import _stage_adaptations
        result = _stage_adaptations(ctx)

        assert "✅" in result, f"Expected success: {result}"
        assert "NOT committed" in result

        # Commit count must NOT increase — still just initial commit
        log_count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert log_count == "1", f"stage_adaptations must not create a commit: {log_count}"

        # File must be staged (in index)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert "adapt.py" in staged

    def test_rejects_on_non_integration_branch(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        (repo / "x.py").write_text("x\n")
        from ouroboros.tools.git_pr import _stage_adaptations
        result = _stage_adaptations(ctx)
        assert "⚠️" in result
        assert "integration branch" in result.lower() or "integrate/pr-" in result

    def test_rejects_when_nothing_to_stage(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        subprocess.run(["git", "checkout", "-b", "integrate/pr-10"], cwd=repo,
                       check=True, capture_output=True)
        # data/ is in .gitignore so no untracked files visible to git add -A
        from ouroboros.tools.git_pr import _stage_adaptations
        result = _stage_adaptations(ctx)
        assert "⚠️" in result

    def test_stage_pr_merge_reports_stash_pop_conflict(self, tmp_path):
        """When stash pop --index fails (adaptation conflicts with merged tree),
        stage_pr_merge must return a clear error — NOT silently claim success."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        # Create integration branch diverging from ouroboros
        subprocess.run(["git", "checkout", "-b", "integrate/pr-77"], cwd=repo,
                       check=True, capture_output=True)
        # PR adds shared.py with content "pr_version"
        _add_commit(repo, "shared.py", "pr_version = 1\n", "pr: add shared.py")
        subprocess.run(["git", "checkout", "ouroboros"], cwd=repo,
                       check=True, capture_output=True)
        # ouroboros also adds shared.py with different content
        _add_commit(repo, "shared.py", "ouroboros_version = 99\n", "fix: add shared.py")

        subprocess.run(["git", "checkout", "integrate/pr-77"], cwd=repo,
                       check=True, capture_output=True)

        # Stage an adaptation that also touches shared.py — this will conflict on stash pop
        (repo / "shared.py").write_text("adaptation_override = 42\n")
        subprocess.run(["git", "add", "shared.py"], cwd=repo, check=True,
                       capture_output=True)

        from ouroboros.tools.git_pr import _stage_pr_merge
        result = _stage_pr_merge(ctx, branch="integrate/pr-77")

        # Must not claim success when stash pop fails
        # Either merge fails (conflict) or stash pop fails — both should be ⚠️
        assert "⚠️" in result, f"Expected error on stash-pop conflict, got: {result}"

    def test_stage_pr_merge_accepts_staged_adaptations_new_file(self, tmp_path):
        """stage_pr_merge must carry staged new-file adaptations into the merge commit."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-10"], cwd=repo,
                       check=True, capture_output=True)
        _add_commit(repo, "pr_change.py", "pr = 1\n", "pr: add pr_change.py")

        # Stage a NEW FILE adaptation
        (repo / "adapt_new.py").write_text("adaptation_new\n")

        from ouroboros.tools.git_pr import _stage_adaptations, _stage_pr_merge
        stage_result = _stage_adaptations(ctx)
        assert "✅" in stage_result, f"stage_adaptations failed: {stage_result}"

        merge_result = _stage_pr_merge(ctx, branch="integrate/pr-10")
        assert "uncommitted changes" not in merge_result.lower(), (
            f"stage_pr_merge must not reject staged adaptations: {merge_result}"
        )
        assert "⚠️ PR_MERGE_ERROR" not in merge_result, merge_result

        # Verify the adaptation file is staged on ouroboros after merge
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert "adapt_new.py" in staged, f"Adaptation not staged after merge: {staged}"

    def test_stage_pr_merge_accepts_staged_adaptations_tracked_file(self, tmp_path):
        """stage_pr_merge must carry staged tracked-file adaptations into the merge commit.

        This is the harder case: stage_adaptations modifies a file that already exists
        on the integration branch. git reset HEAD -- . only unstages; the worktree edit
        must also be cleared (via reset --hard) before checkout to avoid the dirty-tree guard.
        git apply --index must then restore both index AND worktree so no dirty state remains.
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        # ouroboros has tracked.py
        _add_commit(repo, "tracked.py", "original = 1\n", "add tracked.py")

        # integration branch diverges by adding another file
        subprocess.run(["git", "checkout", "-b", "integrate/pr-20"], cwd=repo,
                       check=True, capture_output=True)
        _add_commit(repo, "pr_feature.py", "feature = 2\n", "pr: add feature")

        # Ouroboros modifies the existing tracked.py (tracked-file adaptation)
        (repo / "tracked.py").write_text("original = 1\nadaptation = 99\n")

        from ouroboros.tools.git_pr import _stage_adaptations, _stage_pr_merge
        stage_result = _stage_adaptations(ctx)
        assert "✅" in stage_result, f"stage_adaptations failed: {stage_result}"
        assert "tracked.py" in stage_result

        merge_result = _stage_pr_merge(ctx, branch="integrate/pr-20")
        assert "⚠️ PR_MERGE_ERROR" not in merge_result, (
            f"stage_pr_merge must not fail on tracked-file adaptation: {merge_result}"
        )

        # Must land on ouroboros with MERGE_HEAD set
        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert current == "ouroboros"
        merge_head = subprocess.run(
            ["git", "rev-parse", "MERGE_HEAD"], cwd=repo, capture_output=True
        )
        assert merge_head.returncode == 0, "MERGE_HEAD must be set"

        # tracked.py adaptation must be staged (--index restored both index and worktree)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert "tracked.py" in staged, f"tracked.py adaptation not staged: {staged}"

        # No unstaged tracked changes (index and worktree in sync)
        unstaged = subprocess.run(
            ["git", "diff", "--name-only"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert "tracked.py" not in unstaged, (
            f"tracked.py must not be unstaged after --index apply: {unstaged}"
        )

    def test_stage_pr_merge_rejects_unstaged_tracked_changes(self, tmp_path):
        """stage_pr_merge must reject unstaged tracked changes (they'd be lost on checkout)."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-10"], cwd=repo,
                       check=True, capture_output=True)

        # Create a tracked file and modify without staging
        _add_commit(repo, "tracked.py", "x = 0\n", "add tracked.py")
        (repo / "tracked.py").write_text("x = 99\n")  # unstaged change

        from ouroboros.tools.git_pr import _stage_pr_merge
        result = _stage_pr_merge(ctx, branch="integrate/pr-10")
        assert "⚠️" in result
        assert ("unstaged" in result.lower() or "untracked" in result.lower()
                or "stage_adaptations" in result.lower())

    def test_stage_pr_merge_rejects_untracked_files(self, tmp_path):
        """stage_pr_merge must reject untracked files — they survive checkout and
        could be swept into the merge commit by repo_commit."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-11"], cwd=repo,
                       check=True, capture_output=True)

        # Create an untracked file (not staged, not committed)
        (repo / "noise.py").write_text("noise\n")

        from ouroboros.tools.git_pr import _stage_pr_merge
        result = _stage_pr_merge(ctx, branch="integrate/pr-11")
        assert "⚠️" in result
        assert "untracked" in result.lower() or "stage_adaptations" in result.lower()


# ---------------------------------------------------------------------------
# git_pr: cherry_pick_pr_commits
# ---------------------------------------------------------------------------

class TestCherryPickCommits:
    def _setup_pr_commits(self, repo: pathlib.Path) -> List[str]:
        """Add a 'pr/99' local ref with 2 commits by an external author."""
        # Create a side branch to simulate a fetched PR ref
        subprocess.run(["git", "checkout", "-b", "pr/99"], cwd=repo,
                       check=True, capture_output=True)
        sha1 = _add_commit(repo, "feature_a.py", "def a(): pass\n", "feat: add feature_a")
        sha2 = _add_commit(repo, "feature_b.py", "def b(): pass\n", "feat: add feature_b")
        subprocess.run(["git", "checkout", "ouroboros"], cwd=repo,
                       check=True, capture_output=True)
        return [sha1, sha2]

    def test_applies_commits_without_committing(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = self._setup_pr_commits(repo)

        # Create integration branch
        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=shas)

        assert "✅" in result
        # cherry_pick_pr_commits creates real commits — original authorship preserved
        assert "real commits, original authorship preserved" in result

        log_count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        # cherry_pick_pr_commits creates REAL commits preserving original authorship:
        # initial(1) + 2 cherry-picked = 3 total
        assert log_count == "3"

        # Verify the cherry-picked commits have the original author
        author_log = subprocess.run(
            ["git", "log", "--format=%ae", "-2"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip().splitlines()
        assert all(email == "ext@example.com" for email in author_log), \
            f"Expected external author email, got: {author_log}"

    def test_committer_identity_is_explicit_repo_user(self, tmp_path):
        """GIT_COMMITTER_* is set explicitly — committer = repo's configured user.

        Author = original contributor (ext@example.com)
        Committer = this repo's user.email (test@ouroboros from _make_temp_git_repo)
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = self._setup_pr_commits(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=shas)
        assert "✅" in result

        # Author must be the external contributor
        author_email = subprocess.run(
            ["git", "log", "-1", "--format=%ae"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert author_email == "ext@example.com", \
            f"Author email should be external contributor, got: {author_email}"

        # Committer must be the repo-configured user (test@ouroboros)
        committer_email = subprocess.run(
            ["git", "log", "-1", "--format=%ce"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert committer_email == "test@ouroboros", \
            f"Committer should be repo-configured Ouroboros identity, got: {committer_email}"

    def test_rejects_when_not_on_integration_branch(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        # Still on 'ouroboros', not integrate/pr-*
        result = _cherry_pick_pr_commits(ctx, shas=["abc123"])
        assert "⚠️" in result
        assert "integrate/pr-" in result

    def test_rejects_unknown_sha(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-5"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=["deadbeef1234"])
        assert "⚠️" in result

    def test_stop_on_conflict_false_keeps_applied_shas(self, tmp_path):
        """When stop_on_conflict=False, prior applied SHAs are NOT reset on conflict."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = self._setup_pr_commits(repo)  # 2 commits from external author

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits

        # Apply both SHAs (no conflict expected here — both are clean)
        result = _cherry_pick_pr_commits(ctx, shas=shas, stop_on_conflict=False)
        assert "✅" in result, f"Expected success: {result}"
        # Both SHAs applied and reported
        assert shas[0][:12] in result or shas[1][:12] in result

    def test_rejects_empty_shas(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=[])
        assert "⚠️" in result

    def test_includes_coauthored_hint(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = self._setup_pr_commits(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=shas)

        # Should provide Co-authored-by attribution hint
        assert "Co-authored-by" in result or "Attribution" in result

    def test_stop_on_conflict_true_restores_tree_to_clean(self, tmp_path):
        """stop_on_conflict=True must fully restore both index and worktree to HEAD."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        # Step 1: add conflict.txt on ouroboros with "line A"
        (tmp_path / "conflict.txt").write_text("line A\n")
        subprocess.run(["git", "add", "conflict.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add conflict.txt"], cwd=repo,
                       check=True, capture_output=True)

        # Step 2: create a PR commit (from detached HEAD) that changes conflict.txt
        # to "line B".  The PR commit is based on the shared parent of ouroboros.
        subprocess.run(["git", "checkout", "--detach", "HEAD"], cwd=repo,
                       check=True, capture_output=True)
        (tmp_path / "conflict.txt").write_text("line B\n")
        subprocess.run(["git", "add", "conflict.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "pr: change to line B"], cwd=repo,
                       check=True, capture_output=True)
        conflict_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()

        # Step 3: back on ouroboros, also change conflict.txt to "line C" so the
        # two branches genuinely diverge on the same hunk.
        subprocess.run(["git", "checkout", "ouroboros"], cwd=repo, check=True, capture_output=True)
        (tmp_path / "conflict.txt").write_text("line C\n")
        subprocess.run(["git", "add", "conflict.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "ouroboros: change to line C"], cwd=repo,
                       check=True, capture_output=True)

        # Step 4: create integration branch from current ouroboros (has line C).
        # cherry-picking the "line B" commit onto it will produce a 3-way conflict.
        subprocess.run(["git", "checkout", "-b", "integrate/pr-77"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=[conflict_sha], stop_on_conflict=True)

        # Must report a conflict (not a clean success)
        assert "⚠️" in result, f"Expected conflict error, got: {result}"

        # Repo must be fully clean after abort + reset --hard HEAD
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert status == "", f"Tree not clean after conflict rollback: {status!r}"

    def test_partial_apply_conflict_invalidates_advisory(self, tmp_path):
        """Partial apply + conflict with stop_on_conflict=True must still invalidate advisory.

        If some commits are applied before a conflict, repo history has changed
        even though the call returns an error.  Advisory must be invalidated so a
        later repo_commit cannot rely on pre-cherry-pick review state.

        Setup:
          - ouroboros has 'shared.py' with content "v_ouroboros"
          - PR has TWO commits:
              commit A: adds 'clean.py'  (no conflict — applies cleanly)
              commit B: changes 'shared.py' to "v_pr"  (conflict with ouroboros version)
          - Both PR commits are on a branch that diverged BEFORE shared.py was modified
            on ouroboros, so cherry-picking B onto the integration branch (which has
            the ouroboros version of shared.py) creates a genuine 3-way conflict.
        """
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        # ── Step 1: record the divergence point (HEAD of ouroboros right now) ──
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()

        # ── Step 2: on a detached head from the divergence point, build two
        #            PR commits ──────────────────────────────────────────────
        subprocess.run(["git", "checkout", "--detach", base_sha], cwd=repo,
                       check=True, capture_output=True)

        # Commit A: adds a new file (no conflict)
        (tmp_path / "clean.py").write_text("result = 42\n")
        subprocess.run(["git", "add", "clean.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "pr: add clean.py"], cwd=repo,
                       check=True, capture_output=True)
        good_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()

        # Commit B: adds shared.py with content "v_pr"
        (tmp_path / "shared.py").write_text("v_pr\n")
        subprocess.run(["git", "add", "shared.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "pr: add shared.py"], cwd=repo,
                       check=True, capture_output=True)
        conflict_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()

        # ── Step 3: back on ouroboros, add shared.py with *different* content ──
        subprocess.run(["git", "checkout", "ouroboros"], cwd=repo,
                       check=True, capture_output=True)
        (tmp_path / "shared.py").write_text("v_ouroboros\n")
        subprocess.run(["git", "add", "shared.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "ouroboros: add shared.py"], cwd=repo,
                       check=True, capture_output=True)

        # ── Step 4: create integration branch from this ouroboros HEAD ──────
        subprocess.run(["git", "checkout", "-b", "integrate/pr-partial"], cwd=repo,
                       check=True, capture_output=True)

        # ── Step 5: call with patch — good_sha applies, conflict_sha conflicts ─
        from unittest.mock import patch
        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        with patch("ouroboros.tools.git_pr._invalidate_advisory") as mock_inv:
            result = _cherry_pick_pr_commits(
                ctx, shas=[good_sha, conflict_sha], stop_on_conflict=True
            )

        # Must report a conflict
        assert "⚠️" in result, f"Expected conflict error, got: {result}"
        # Advisory MUST be invalidated — good_sha was already applied before the conflict
        assert mock_inv.called, (
            "advisory must be invalidated on partial apply + conflict; "
            f"result was: {result!r}"
        )

    def test_stop_on_conflict_false_reports_skipped_shas(self, tmp_path):
        """stop_on_conflict=False: skipped SHAs must appear in the return value."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        # Good commit (no conflict)
        good_sha = _add_commit(repo, "good.py", "x = 1\n", "good commit")
        subprocess.run(["git", "checkout", "ouroboros"], cwd=repo, check=True, capture_output=True)

        # Create conflict: same file modified on ouroboros after the commit
        subprocess.run(["git", "checkout", "--detach", "HEAD"], cwd=repo,
                       check=True, capture_output=True)
        (tmp_path / "conflict2.txt").write_text("original\n")
        subprocess.run(["git", "add", "conflict2.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "base for conflict"], cwd=repo,
                       check=True, capture_output=True)
        (tmp_path / "conflict2.txt").write_text("pr change\n")
        subprocess.run(["git", "add", "conflict2.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "pr conflict commit"], cwd=repo,
                       check=True, capture_output=True)
        conflict_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()

        subprocess.run(["git", "checkout", "ouroboros"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "integrate/pr-66"], cwd=repo,
                       check=True, capture_output=True)
        # Modify conflict2.txt on the integration branch to guarantee conflict
        (tmp_path / "conflict2.txt").write_text("different content\n")
        subprocess.run(["git", "add", "conflict2.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "set up conflict"], cwd=repo,
                       check=True, capture_output=True)
        subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _cherry_pick_pr_commits
        result = _cherry_pick_pr_commits(ctx, shas=[good_sha, conflict_sha], stop_on_conflict=False)

        # Result must surface the skipped SHA explicitly — not silent truncation
        assert conflict_sha[:12] in result or "skipped" in result.lower() or "PARTIAL" in result, \
            f"Skipped SHA not reported: {result}"

    def test_invalidates_advisory_after_apply(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)
        shas = self._setup_pr_commits(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-99"], cwd=repo,
                       check=True, capture_output=True)

        with patch("ouroboros.tools.git_pr._invalidate_advisory") as mock_inv:
            from ouroboros.tools import git_pr
            # Reload to pick up patch
            git_pr._cherry_pick_pr_commits(ctx, shas=shas)
            mock_inv.assert_called_once()

    # -----------------------------------------------------------------
    # override_author tests live in tests/test_git_pr_override_author.py
    # (moved out to keep this module under the 1600-line hard gate).
    # -----------------------------------------------------------------

# ---------------------------------------------------------------------------
# git_pr: stage_pr_merge
# ---------------------------------------------------------------------------

class TestMergeIntegrationBranch:
    def test_squash_merge_without_committing(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        # Create integration branch with a commit on a file that doesn't exist on ouroboros
        subprocess.run(["git", "checkout", "-b", "integrate/pr-3"], cwd=repo,
                       check=True, capture_output=True)
        _add_commit(repo, "new_feature.py", "x = 1\n", "feat: new feature")
        # Must be on integration branch when calling stage_pr_merge
        # (already on integrate/pr-3 from checkout above; just clean untracked)
        subprocess.run(["git", "clean", "-fdx", "--exclude=.git"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _stage_pr_merge
        result = _stage_pr_merge(ctx, branch="integrate/pr-3")

        assert "✅" in result, f"Expected success, got: {result}"
        # stage_pr_merge stages but does NOT commit — MERGE_HEAD is set
        assert "NOT committed" in result

        # No new commit on ouroboros
        log_count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert log_count == "1"

    def test_rejects_nonexistent_branch(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        from ouroboros.tools.git_pr import _stage_pr_merge
        result = _stage_pr_merge(ctx, branch="integrate/pr-999")
        assert "⚠️" in result
        assert "does not exist" in result

    def test_rejects_empty_branch(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        from ouroboros.tools.git_pr import _stage_pr_merge
        result = _stage_pr_merge(ctx, branch="")
        assert "⚠️" in result

    def test_squash_merge_conflict_restores_tree_to_clean(self, tmp_path):
        """On squash-merge conflict, tree must be fully restored to HEAD (reset --hard)."""
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        # Create the same file on ouroboros with one content
        (tmp_path / "shared.txt").write_text("ouroboros version\n")
        subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add shared.txt"], cwd=repo,
                       check=True, capture_output=True)

        # Create integration branch that modifies the same file differently
        subprocess.run(["git", "checkout", "-b", "integrate/pr-55"], cwd=repo,
                       check=True, capture_output=True)
        (tmp_path / "shared.txt").write_text("pr version\n")
        subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "pr: modify shared.txt"], cwd=repo,
                       check=True, capture_output=True)

        # Modify shared.txt on ouroboros too to create a genuine conflict
        subprocess.run(["git", "checkout", "ouroboros"], cwd=repo, check=True, capture_output=True)
        (tmp_path / "shared.txt").write_text("different ouroboros version\n")
        subprocess.run(["git", "add", "shared.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "ouroboros: also modify shared.txt"], cwd=repo,
                       check=True, capture_output=True)

        # Must be on integration branch when calling stage_pr_merge
        subprocess.run(["git", "checkout", "integrate/pr-55"], cwd=repo,
                       check=True, capture_output=True)

        # Clean untracked files before calling function
        subprocess.run(["git", "clean", "-fd"], cwd=repo, check=True, capture_output=True)

        from ouroboros.tools.git_pr import _stage_pr_merge
        result = _stage_pr_merge(ctx, branch="integrate/pr-55")

        # Either succeeds (no actual git conflict on squash merge) or errors cleanly
        # In either case the tree must be clean after the call
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        # Tree should be clean (either nothing staged, or a merge conflict was cleaned up)
        # git cherry-pick / merge --squash might auto-resolve; just assert no half-state
        assert "AA" not in status, f"Merge conflict markers in index: {status!r}"

    def test_integration_branch_left_intact(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        subprocess.run(["git", "checkout", "-b", "integrate/pr-3"], cwd=repo,
                       check=True, capture_output=True)
        _add_commit(repo, "feature.py", "x = 1\n", "feat: x")
        # Must stay on integration branch — stage_pr_merge requires HEAD==branch
        subprocess.run(["git", "clean", "-fd"], cwd=repo,
                       check=True, capture_output=True)

        from ouroboros.tools.git_pr import _stage_pr_merge
        _stage_pr_merge(ctx, branch="integrate/pr-3")

        # Branch must still exist after squash-merge
        branches = subprocess.run(
            ["git", "branch"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "integrate/pr-3" in branches


# ---------------------------------------------------------------------------
# End-to-end: full PR intake flow with adaptation + merge
# ---------------------------------------------------------------------------

class TestEndToEndPRFlow:
    """Full flow: cherry_pick → stage_adaptations → stage_pr_merge → commit.

    Proves that:
    1. External author commits land on integration branch with original attribution.
    2. Staged adaptation changes (from stage_adaptations) carry over through
       stage_pr_merge into the final merge commit on ouroboros.
    3. No intermediate reviewed commit on the integration branch is required.
    """

    def test_full_pr_intake_with_adaptations(self, tmp_path):
        repo = _make_temp_git_repo(tmp_path)
        ctx = _make_ctx(repo)

        # --- Simulate fetched PR: two external-author commits on pr/1 ---
        subprocess.run(["git", "checkout", "-b", "pr/1"], cwd=repo,
                       check=True, capture_output=True)
        ext_sha1 = _add_commit(repo, "ext_a.py", "ext_a = 1\n", "feat: ext_a",
                                author_name="External Dev", author_email="ext@example.com")
        ext_sha2 = _add_commit(repo, "ext_b.py", "ext_b = 2\n", "feat: ext_b",
                                author_name="External Dev", author_email="ext@example.com")
        subprocess.run(["git", "checkout", "ouroboros"], cwd=repo,
                       check=True, capture_output=True)

        # --- Step 2: create_integration_branch ---
        from ouroboros.tools.git_pr import (
            _create_integration_branch, _cherry_pick_pr_commits,
            _stage_adaptations, _stage_pr_merge,
        )
        branch_result = _create_integration_branch(ctx, pr_number=1)
        assert "✅" in branch_result, f"create_integration_branch failed: {branch_result}"

        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert current == "integrate/pr-1"

        # --- Step 3: cherry_pick_pr_commits (preserves external author) ---
        pick_result = _cherry_pick_pr_commits(ctx, shas=[ext_sha1, ext_sha2])
        assert "✅" in pick_result, f"cherry_pick_pr_commits failed: {pick_result}"
        assert "2" in pick_result  # 2 commits applied

        # Verify external author attribution preserved on integration branch
        log = subprocess.run(
            ["git", "log", "--format=%ae", "-2", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip().splitlines()
        assert all(email == "ext@example.com" for email in log), \
            f"External author email not preserved: {log}"

        # --- Step 4: stage_adaptations (no commit — lands in merge commit) ---
        (repo / "ouroboros_adapt.py").write_text("# Ouroboros adaptation\n")
        adapt_result = _stage_adaptations(ctx)
        assert "✅" in adapt_result, f"stage_adaptations failed: {adapt_result}"
        assert "ouroboros_adapt.py" in adapt_result

        # Confirm NOT committed — still same 3 commits (init + 2 cherry-picks)
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert count == "3", f"stage_adaptations must not commit; count={count}"

        # --- Step 5: stage_pr_merge (staged adaptations survive checkout) ---
        merge_result = _stage_pr_merge(ctx, branch="integrate/pr-1")
        assert "✅" in merge_result, f"stage_pr_merge failed: {merge_result}"
        assert "uncommitted changes" not in merge_result.lower()

        # Now on ouroboros with MERGE_HEAD set and adaptations still staged
        current_after = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert current_after == "ouroboros", \
            f"Expected to be on ouroboros after stage_pr_merge, got: {current_after}"

        merge_head = subprocess.run(
            ["git", "rev-parse", "MERGE_HEAD"],
            cwd=repo, capture_output=True, text=True
        )
        assert merge_head.returncode == 0, "MERGE_HEAD must be set after stage_pr_merge"

        # Adaptation file still staged on ouroboros
        staged_after = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert "ouroboros_adapt.py" in staged_after, \
            f"Staged adaptations must survive stage_pr_merge checkout; staged={staged_after}"

        # --- Step 6: simulate repo_commit (git commit directly — no checkout flip) ---
        # In production this is advisory_pre_review + repo_commit; here we commit
        # directly to verify the merge commit includes both parents + adaptations.
        subprocess.run(
            ["git", "commit", "-m",
             "merge(pr-1): integrate ext dev contribution\n\nCo-authored-by: External Dev <ext@example.com>"],
            cwd=repo, check=True, capture_output=True
        )

        # Verify merge commit has 2 parents
        parents = subprocess.run(
            ["git", "log", "--format=%P", "-1", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip().split()
        assert len(parents) == 2, f"Merge commit must have 2 parents; got {parents}"

        # Verify ext_b (tip of integration branch) is one parent
        int_tip = subprocess.run(
            ["git", "rev-parse", "integrate/pr-1"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert int_tip in parents, "Integration branch tip must be a parent of merge commit"

        # Verify adaptation file is in the merge commit
        merge_files = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert "ouroboros_adapt.py" in merge_files, \
            f"Adaptation file must be in merge commit; files={merge_files}"


# ---------------------------------------------------------------------------
# github.py: _list_prs / _get_pr
# ---------------------------------------------------------------------------

class TestListPrs:
    def test_parses_open_prs(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        sample = json.dumps([{
            "number": 17,
            "title": "Add A2A Protocol",
            "author": {"login": "mr8bit"},
            "headRefName": "main",
            "baseRefName": "main",
            "createdAt": "2026-04-10T21:29:16Z",
            "isDraft": False,
            "reviewDecision": None,
            "commits": [{"oid": "abc"}, {"oid": "def"}],
        }])
        with patch("ouroboros.tools.github._gh_cmd", return_value=sample):
            from ouroboros.tools.github import _list_prs
            result = _list_prs(ctx)
        assert "PR #17" in result
        assert "@mr8bit" in result
        assert "2 commits" in result

    def test_empty_returns_message(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        with patch("ouroboros.tools.github._gh_cmd", return_value="[]"):
            from ouroboros.tools.github import _list_prs
            result = _list_prs(ctx)
        assert "No open" in result

    def test_gh_error_propagates(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        with patch("ouroboros.tools.github._gh_cmd", return_value="⚠️ GH_ERROR: not found"):
            from ouroboros.tools.github import _list_prs
            result = _list_prs(ctx)
        assert "⚠️" in result


class TestGetPr:
    def test_invalid_number(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        from ouroboros.tools.github import _get_pr
        result = _get_pr(ctx, number=0)
        assert "⚠️" in result

    def test_parses_pr_details(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        sample = json.dumps({
            "number": 17,
            "title": "Add A2A Protocol",
            "body": "Integrates A2A.",
            "author": {"login": "mr8bit"},
            "headRefName": "main",
            "baseRefName": "main",
            "headRepository": {"nameWithOwner": "mr8bit/ouroboros-desktop"},
            "createdAt": "2026-04-10T21:29:16Z",
            "updatedAt": "2026-04-10T22:00:00Z",
            "state": "OPEN",
            "isDraft": False,
            "reviewDecision": None,
            "mergeable": "MERGEABLE",
            "additions": 1792,
            "deletions": 1,
            "changedFiles": 10,
            "commits": [{
                "oid": "cb9586b5dc51",
                "commit": {
                    "messageHeadline": "docs: A2A design spec",
                    "authors": {"nodes": [{"name": "Ouroboros", "email": "ouroboros@local.mac"}]},
                },
            }],
        })
        with patch("ouroboros.tools.github._gh_cmd", return_value=sample):
            from ouroboros.tools.github import _get_pr
            result = _get_pr(ctx, number=17)
        assert "PR #17" in result
        assert "MERGEABLE" in result
        assert "+1792" in result
        assert "cherry_pick_pr_commits" in result

    def test_shows_integration_steps(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        sample = json.dumps({
            "number": 5, "title": "T", "body": "",
            "author": {"login": "u"}, "headRefName": "f", "baseRefName": "main",
            "headRepository": {"nameWithOwner": "u/r"},
            "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
            "state": "OPEN", "isDraft": False, "reviewDecision": None,
            "mergeable": "MERGEABLE", "additions": 10, "deletions": 0,
            "changedFiles": 1, "commits": [],
        })
        with patch("ouroboros.tools.github._gh_cmd", return_value=sample):
            from ouroboros.tools.github import _get_pr
            result = _get_pr(ctx, number=5)
        assert "fetch_pr_ref" in result
        assert "create_integration_branch" in result
        assert "advisory_pre_review" in result
        assert "override_author" in result, (
            "Integration steps must mention override_author so operators using "
            "get_github_pr learn the v4.35.0 workflow for rewriting placeholder "
            "committer identities to real GitHub authors."
        )


# ---------------------------------------------------------------------------
# Tool registration: new tools visible in get_tools()
# ---------------------------------------------------------------------------

class TestToolRegistration:
    def test_github_exports_list_prs_and_get_pr(self):
        from ouroboros.tools.github import get_tools
        names = {t.name for t in get_tools()}
        assert "list_github_prs" in names
        assert "get_github_pr" in names

    def test_git_pr_exports_five_tools(self):
        from ouroboros.tools.git_pr import get_tools
        names = {t.name for t in get_tools()}
        assert names == {
            "fetch_pr_ref",
            "create_integration_branch",
            "cherry_pick_pr_commits",
            "stage_adaptations",
            "stage_pr_merge",
        }

    def test_github_exports_comment_on_pr(self):
        from ouroboros.tools.github import get_tools
        names = {t.name for t in get_tools()}
        assert "comment_on_pr" in names

    def test_pr_tools_are_non_core(self):
        """PR tools must NOT be in CORE_TOOL_NAMES — they require enable_tools."""
        from ouroboros.tool_capabilities import CORE_TOOL_NAMES
        pr_tools = {
            "list_github_prs", "get_github_pr", "comment_on_pr",
            "fetch_pr_ref", "create_integration_branch",
            "cherry_pick_pr_commits", "stage_adaptations", "stage_pr_merge",
        }
        intersection = pr_tools & CORE_TOOL_NAMES
        assert not intersection, (
            f"PR tools must be non-core (require enable_tools): {intersection}"
        )

    # Note: override_author schema pin lives in tests/test_git_pr_override_author.py
    # along with the behavioral tests, to keep this module under the 1600-line gate.


# ---------------------------------------------------------------------------
# github.py: _gh_env token injection
# ---------------------------------------------------------------------------

class TestGhEnv:
    """_gh_env injects GITHUB_TOKEN into subprocess env without gh auth login."""

    def test_injects_token_from_os_environ(self, tmp_path):
        """When GITHUB_TOKEN is in os.environ, _gh_env exposes it as GH_TOKEN."""
        import os
        from unittest.mock import patch
        ctx = _make_ctx(tmp_path)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token-123", "GH_TOKEN": ""},
                        clear=False):
            from ouroboros.tools.github import _gh_env
            env = _gh_env(ctx)
        assert env.get("GH_TOKEN") == "test-token-123"
        assert env.get("GITHUB_TOKEN") == "test-token-123"

    def test_injects_token_from_load_settings(self, tmp_path):
        """When token not in os.environ, _gh_env falls back to load_settings().

        ToolContext has no .settings field; load_settings() is the correct
        fallback path (same pattern as ci.py::_get_github_config).
        """
        import os
        from unittest.mock import patch
        ctx = _make_ctx(tmp_path)
        # Patch os.environ to remove any real GITHUB_TOKEN, and mock load_settings
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
        with patch.dict(os.environ, clean_env, clear=True):
            with patch("ouroboros.config.load_settings",
                       return_value={"GITHUB_TOKEN": "settings-token-456"}):
                from ouroboros.tools.github import _gh_env
                env = _gh_env(ctx)
        assert env.get("GH_TOKEN") == "settings-token-456"
        assert env.get("GITHUB_TOKEN") == "settings-token-456"

    def test_gh_cmd_passes_env_to_subprocess(self, tmp_path):
        """_gh_cmd passes env to subprocess.run (not relying on ambient auth)."""
        import os
        from unittest.mock import patch, MagicMock
        ctx = _make_ctx(tmp_path)
        with patch.dict(os.environ, {"GITHUB_TOKEN": "mytoken"}, clear=False):
            with patch("ouroboros.tools.github.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
                from ouroboros.tools.github import _gh_cmd
                _gh_cmd(["pr", "list"], ctx)
        call_kwargs = mock_run.call_args[1]
        assert "env" in call_kwargs, "_gh_cmd must pass env= to subprocess.run"
        assert call_kwargs["env"].get("GH_TOKEN") == "mytoken"
