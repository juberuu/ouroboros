import json
import os
import pathlib
import shutil
import subprocess
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_repo_bundle.py"


# Env vars the production ``build_repo_bundle.py`` reads as release-tag
# candidates (OUROBOROS_RELEASE_TAG + the GitHub Actions GITHUB_REF_* set).
# These MUST be scrubbed from every subprocess the tests spawn — on a CI
# run triggered by a tag push, GITHUB_REF_NAME/GITHUB_REF/GITHUB_REF_TYPE
# are set globally for all jobs, which would bleed into the temp-repo
# subprocesses and confuse ``_resolve_release_tag`` into reporting two
# tags on HEAD (the temp-repo's tag + the bleed).
_BUILD_BUNDLE_ENV_SCRUB_KEYS = (
    "OUROBOROS_RELEASE_TAG",
    "GITHUB_REF",
    "GITHUB_REF_TYPE",
    "GITHUB_REF_NAME",
)


def _scrubbed_env(extra: "dict[str, str] | None" = None) -> "dict[str, str]":
    env = dict(os.environ)
    for key in _BUILD_BUNDLE_ENV_SCRUB_KEYS:
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def _run(cmd, *, cwd, check=True, env=None):
    if env is None:
        # Caller did not ask for an explicit env -> scrub release-tag
        # env bleed so temp-repo subprocesses always start from a clean
        # slate, regardless of what the outer CI sets.
        env = _scrubbed_env()
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _make_repo(tmp_path):
    repo = tmp_path / "repo-src"
    repo.mkdir()
    _run(["git", "init"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "checkout", "-b", "ouroboros"], cwd=repo)
    _run(["git", "remote", "add", "origin", "git@github.com:joi-lab/ouroboros-desktop.git"], cwd=repo)
    (repo / "VERSION").write_text("4.50.0-rc.2\n", encoding="utf-8")
    (repo / "server.py").write_text("print('ok')\n", encoding="utf-8")
    _run(["git", "add", "VERSION", "server.py"], cwd=repo)
    _run(["git", "commit", "-m", "initial"], cwd=repo)
    sha = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    _run(["git", "tag", "-a", "v4.50.0-rc.2", "-m", "Release v4.50.0-rc.2"], cwd=repo)
    return repo, sha


def test_build_repo_bundle_accepts_explicit_source_branch_on_detached_head(tmp_path):
    repo, sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"

    _run(["git", "checkout", "--detach", "HEAD"], cwd=repo)
    result = _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "ouroboros",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
    )

    assert "Generated repo.bundle and repo_bundle_manifest.json" in result.stdout
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["source_sha"] == sha
    assert payload["release_tag"] == "v4.50.0-rc.2"
    assert payload["bundle_sha256"]
    assert payload["managed_remote_branch"] == "ouroboros"
    assert payload["managed_remote_url"] == "https://github.com/joi-lab/ouroboros-desktop.git"


def test_build_repo_bundle_uses_checked_out_head_not_branch_tip(tmp_path):
    repo, first_sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"

    (repo / "server.py").write_text("print('branch-tip')\n", encoding="utf-8")
    _run(["git", "add", "server.py"], cwd=repo)
    _run(["git", "commit", "-m", "advance branch"], cwd=repo)
    _run(["git", "checkout", "--detach", first_sha], cwd=repo)

    _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "ouroboros",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
    )

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["source_sha"] == first_sha
    assert payload["release_tag"] == "v4.50.0-rc.2"


def test_build_repo_bundle_refuses_dirty_worktree(tmp_path):
    repo, _sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"

    (repo / "server.py").write_text("print('dirty')\n", encoding="utf-8")
    result = _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "ouroboros",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
        check=False,
    )

    assert result.returncode != 0
    assert "dirty working tree" in (result.stderr or "")


def test_build_repo_bundle_rejects_reserved_origin_remote_name(tmp_path):
    repo, _sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"

    result = _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "ouroboros",
            "--managed-remote-name",
            "origin",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
        check=False,
    )

    assert result.returncode != 0
    assert "reserved for user-configured remotes" in (result.stderr or "")


def test_build_repo_bundle_preserves_https_remote_ports(tmp_path):
    repo, _sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"

    _run(["git", "remote", "set-url", "origin", "https://git.example.com:8443/org/repo.git"], cwd=repo)
    _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "ouroboros",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
    )

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["managed_remote_url"] == "https://git.example.com:8443/org/repo.git"


def test_build_repo_bundle_rejects_unknown_source_branch(tmp_path):
    repo, _sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"

    _run(["git", "remote", "set-url", "origin", str(repo)], cwd=repo)
    result = _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "missing-branch",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
        check=False,
    )

    assert result.returncode != 0
    assert "not available locally" in (result.stderr or "")


def test_build_repo_bundle_rejects_release_tag_mismatch(tmp_path):
    repo, _sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"
    env = _scrubbed_env({"OUROBOROS_RELEASE_TAG": "v4.50.0-rc.3"})

    result = _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "ouroboros",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
        check=False,
        env=env,
    )

    assert result.returncode != 0
    assert "does not match VERSION" in (result.stderr or "")


def test_build_repo_bundle_requires_release_tag(tmp_path):
    repo, _sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"

    _run(["git", "tag", "-d", "v4.50.0-rc.2"], cwd=repo)
    env = _scrubbed_env()

    result = _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "ouroboros",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
        check=False,
        env=env,
    )

    assert result.returncode != 0
    assert "Could not determine release tag" in (result.stderr or "")


def test_build_repo_bundle_rejects_head_outside_source_branch(tmp_path):
    repo, _sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"
    env = _scrubbed_env({"OUROBOROS_RELEASE_TAG": "v4.50.0-rc.2"})

    _run(["git", "checkout", "--orphan", "release-detached"], cwd=repo)
    for path in repo.iterdir():
        if path.name == ".git":
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    (repo / "VERSION").write_text("4.50.0-rc.2\n", encoding="utf-8")
    (repo / "server.py").write_text("print('detached')\n", encoding="utf-8")
    _run(["git", "add", "VERSION", "server.py"], cwd=repo)
    _run(["git", "commit", "-m", "detached"], cwd=repo)
    # Retag the same release onto the orphan HEAD so we actually exercise
    # the ancestry-vs-managed-branch guard rather than the new tag-points-at-HEAD
    # guard added in ``_verify_release_tag_in_repo``.
    _run(["git", "tag", "-d", "v4.50.0-rc.2"], cwd=repo)
    _run(["git", "tag", "-a", "v4.50.0-rc.2", "-m", "Release v4.50.0-rc.2"], cwd=repo)

    result = _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "ouroboros",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
        check=False,
        env=env,
    )

    assert result.returncode != 0
    assert "not reachable from the configured managed source branch" in (result.stderr or "")


def test_build_repo_bundle_rejects_lightweight_tag(tmp_path):
    """BIBLE.md P7 requires annotated release tags. ``_verify_release_tag_in_repo``
    must refuse lightweight tags even when ``OUROBOROS_RELEASE_TAG`` matches
    ``VERSION`` and the tag points at ``HEAD``."""
    repo, _sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"

    # Replace the annotated tag created by _make_repo with a lightweight one
    # at the same commit.
    _run(["git", "tag", "-d", "v4.50.0-rc.2"], cwd=repo)
    _run(["git", "tag", "v4.50.0-rc.2"], cwd=repo)
    env = _scrubbed_env({"OUROBOROS_RELEASE_TAG": "v4.50.0-rc.2"})

    result = _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "ouroboros",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
        check=False,
        env=env,
    )

    assert result.returncode != 0
    assert "not an annotated tag" in (result.stderr or "")


def test_build_repo_bundle_rejects_remote_only_source_branch(tmp_path):
    """``_validate_source_branch`` must refuse a branch that is not in
    local refs, even if it exists on the configured remote. Otherwise
    validation passes but the build fails later with a confusing
    provenance error inside ``_ensure_source_sha_tracks_branch``."""
    repo, _sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"
    env = _scrubbed_env({"OUROBOROS_RELEASE_TAG": "v4.50.0-rc.2"})

    # Request a branch that neither exists locally nor is cached under
    # origin/. The `origin` remote URL is a non-routable dummy set up
    # in _make_repo, so an `ls-remote` call would have fabricated a hit
    # in the old behaviour; the tightened check refuses outright.
    result = _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "never-fetched-branch",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
        check=False,
        env=env,
    )

    assert result.returncode != 0
    assert "not available locally" in (result.stderr or "")


def test_build_repo_bundle_rejects_env_tag_not_on_head(tmp_path):
    """A direct caller can set ``OUROBOROS_RELEASE_TAG`` to a tag that
    exists but points at a previous commit. The bundler must refuse
    rather than embedding a misleading ``release_tag`` in the manifest."""
    repo, first_sha = _make_repo(tmp_path)
    bundle = tmp_path / "repo.bundle"
    manifest = tmp_path / "repo_bundle_manifest.json"

    # Advance HEAD past the tagged commit.
    (repo / "server.py").write_text("print('post-tag')\n", encoding="utf-8")
    _run(["git", "add", "server.py"], cwd=repo)
    _run(["git", "commit", "-m", "post tag"], cwd=repo)
    env = _scrubbed_env({"OUROBOROS_RELEASE_TAG": "v4.50.0-rc.2"})

    result = _run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(repo),
            "--source-branch",
            "ouroboros",
            "--output-bundle",
            str(bundle),
            "--output-manifest",
            str(manifest),
        ],
        cwd=repo,
        check=False,
        env=env,
    )

    assert result.returncode != 0
    assert "does not point at HEAD" in (result.stderr or "")
