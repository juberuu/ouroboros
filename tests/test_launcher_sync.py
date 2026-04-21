import importlib
import types

import ouroboros.launcher_bootstrap as bootstrap_module


def _reload_bootstrap():
    return importlib.reload(bootstrap_module)


def _make_context(bundle_dir, repo_dir):
    return bootstrap_module.BootstrapContext(
        bundle_dir=bundle_dir,
        repo_dir=repo_dir,
        data_dir=repo_dir.parent / "data",
        settings_path=repo_dir.parent / "settings.json",
        embedded_python="python3",
        app_version="4.7.0",
        hidden_run=lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        save_settings=lambda settings: None,
        log=types.SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None),
    )


def test_sync_bundle_managed_paths_copies_only_whitelisted_entries(monkeypatch, tmp_path):
    bootstrap = _reload_bootstrap()
    bundle_dir = tmp_path / "bundle"
    repo_dir = tmp_path / "repo"
    bundle_dir.mkdir()
    repo_dir.mkdir()

    (bundle_dir / "server.py").write_text("new-server\n", encoding="utf-8")
    (bundle_dir / "web").mkdir()
    (bundle_dir / "web" / "index.html").write_text("web\n", encoding="utf-8")
    (bundle_dir / "Foundation").mkdir()
    (bundle_dir / "Foundation" / "ignored.txt").write_text("native\n", encoding="utf-8")
    (repo_dir / "server.py").write_text("old-server\n", encoding="utf-8")

    ctx = _make_context(bundle_dir, repo_dir)

    bootstrap.sync_bundle_managed_paths(ctx, overwrite_existing=False)

    assert (repo_dir / "server.py").read_text(encoding="utf-8") == "old-server\n"
    assert (repo_dir / "web" / "index.html").read_text(encoding="utf-8") == "web\n"
    assert not (repo_dir / "Foundation").exists()


def test_sync_bundle_managed_paths_overwrites_existing_managed_files(monkeypatch, tmp_path):
    bootstrap = _reload_bootstrap()
    bundle_dir = tmp_path / "bundle"
    repo_dir = tmp_path / "repo"
    bundle_dir.mkdir()
    repo_dir.mkdir()

    (bundle_dir / "server.py").write_text("new-server\n", encoding="utf-8")
    (repo_dir / "server.py").write_text("old-server\n", encoding="utf-8")

    ctx = _make_context(bundle_dir, repo_dir)

    bootstrap.sync_bundle_managed_paths(ctx, overwrite_existing=True)

    assert (repo_dir / "server.py").read_text(encoding="utf-8") == "new-server\n"


def test_sync_existing_repo_calls_core_and_commit(monkeypatch, tmp_path):
    """sync_existing_repo_from_bundle only syncs core files and commits them.
    Full bundle overwrite paths (managed paths, dirty-check, backup, version sync)
    must NOT be invoked — agent self-modifications must not be clobbered."""
    bootstrap = _reload_bootstrap()
    bundle_dir = tmp_path / "bundle"
    repo_dir = tmp_path / "repo"
    bundle_dir.mkdir()
    repo_dir.mkdir()

    calls = []
    ctx = _make_context(bundle_dir, repo_dir)

    monkeypatch.setattr(bootstrap, "sync_core_files", lambda context: calls.append("core"))
    monkeypatch.setattr(bootstrap, "commit_synced_files", lambda context: calls.append("commit-safety"))

    # These must NOT be called — patch them to fail loudly if invoked.
    def _should_not_be_called(name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"{name} must not be called by sync_existing_repo_from_bundle")
        return _raise

    for fn in ("sync_bundle_managed_paths", "repo_has_pending_changes",
               "create_bundle_backup_branch", "commit_bundle_sync"):
        if hasattr(bootstrap, fn):
            monkeypatch.setattr(bootstrap, fn, _should_not_be_called(fn))

    bootstrap.sync_existing_repo_from_bundle(ctx)

    assert calls == ["core", "commit-safety"]
