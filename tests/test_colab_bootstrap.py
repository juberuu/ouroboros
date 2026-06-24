from __future__ import annotations
import subprocess

def test_build_colab_settings_defaults_auto_grant_and_runtime():
    from ouroboros.colab_bootstrap import build_colab_settings, masked_secret_status
    settings = build_colab_settings({"OPENROUTER_API_KEY": "or-key", "TELEGRAM_BOT_TOKEN": "tg-token", "GITHUB_TOKEN": "gh-token"}, github_repo="anton/ouroboros", total_budget=25, runtime_mode="pro", max_workers=2)
    assert settings["GITHUB_REPO"] == "anton/ouroboros"
    assert settings["OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS"] == "true"
    assert masked_secret_status(settings)["TELEGRAM_BOT_TOKEN"] is True

def test_build_colab_settings_merges_existing_owner_choices():
    # A Colab re-run must preserve prior owner choices not set by the launch knobs
    # (pinned chat, tweaked model) and drop private sentinel keys.
    from ouroboros.colab_bootstrap import build_colab_settings
    existing = {"TELEGRAM_CHAT_ID": "12345", "OUROBOROS_MODEL": "custom/model", "_settings_file_exists": True}
    out = build_colab_settings({"OPENROUTER_API_KEY": "k"}, existing=existing)
    assert out["TELEGRAM_CHAT_ID"] == "12345"
    assert out["OUROBOROS_MODEL"] == "custom/model"
    assert "_settings_file_exists" not in out
    assert out["OPENROUTER_API_KEY"] == "k"


def test_build_colab_settings_accepts_vision_model_override():
    from ouroboros.colab_bootstrap import build_colab_settings

    out = build_colab_settings(
        {"OPENROUTER_API_KEY": "k"},
        models={"OUROBOROS_MODEL_VISION": "google/gemini-2.5-pro"},
    )
    assert out["OUROBOROS_MODEL_VISION"] == "google/gemini-2.5-pro"

def test_quickstart_uses_clone_or_update_repo_helper():
    import pathlib
    source = pathlib.Path(__file__).resolve().parents[1].joinpath("notebooks", "colab_quickstart.py").read_text(encoding="utf-8")
    assert "clone_or_update_repo" in source
    assert source.index("clone_or_update_repo(REPO_DIR)") < source.index("pip", source.index("clone_or_update_repo(REPO_DIR)"))

def test_clone_or_update_repo_fast_forwards_existing_checkout(tmp_path):
    from ouroboros.colab_bootstrap import clone_or_update_repo
    upstream = tmp_path / "upstream"; upstream.mkdir()
    subprocess.run(["git", "init"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=upstream, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=upstream, check=True)
    subprocess.run(["git", "checkout", "-b", "ouroboros"], cwd=upstream, check=True, capture_output=True)
    (upstream / "marker.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "marker.txt"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=upstream, check=True, capture_output=True)
    checkout = tmp_path / "checkout"
    clone_or_update_repo(checkout, source_url=str(upstream), branch="ouroboros")
    (upstream / "marker.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "marker.txt"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=upstream, check=True, capture_output=True)
    clone_or_update_repo(checkout, source_url=str(upstream), branch="ouroboros")
    assert (checkout / "marker.txt").read_text(encoding="utf-8") == "v2\n"

def test_get_colab_secret_optional_returns_empty_without_prompt(monkeypatch):
    from ouroboros.colab_bootstrap import get_colab_secret
    monkeypatch.delenv("OUROBOROS_TEST_ABSENT_KEY", raising=False)
    # required=False must never block on getpass when the secret is absent.
    assert get_colab_secret("OUROBOROS_TEST_ABSENT_KEY", required=False) == ""

def test_ensure_telegram_bridge_live_installs_enables_and_sets_full_access():
    from ouroboros.colab_bootstrap import ensure_telegram_bridge_live
    calls = []
    def fake_request(method, path, body=None, timeout=None):
        calls.append((method, path, body, timeout))
        if path == "/api/health":
            return 200, {"ok": True}
        if path.endswith("/toggle"):
            return 200, {"ok": True, "enabled": True}
        return 200, {"ok": True}
    status = ensure_telegram_bridge_live(settings={"TELEGRAM_BOT_TOKEN": "x"}, request=fake_request, timeout=5)
    assert status["ok"] is True and status["command_mode_ok"] is True
    assert status["steps"] == ["ready", "installed", "enabled", "command_mode:full_access"]
    triples = [(m, p, b) for (m, p, b, t) in calls]
    assert ("POST", "/api/skills/telegram-bridge/toggle", {"enabled": True}) in triples
    assert ("POST", "/api/extensions/telegram-bridge/settings/save", {"TELEGRAM_COMMAND_MODE": "full_access"}) in triples
    # Auto-grant must NOT be force-POSTed; it is governed by the persisted setting.
    assert all(p != "/api/owner/auto-grant" for (m, p, b) in triples)
    # Install uses a review-scale timeout, not the default 60s (synchronous tri-model review).
    install_timeout = next(t for (m, p, b, t) in calls if p == "/api/marketplace/ouroboroshub/install")
    assert install_timeout is not None and install_timeout >= 600

def test_ensure_telegram_bridge_live_command_mode_failure_is_not_silent():
    from ouroboros.colab_bootstrap import ensure_telegram_bridge_live
    def fake_request(method, path, body=None, timeout=None):
        if path == "/api/health":
            return 200, {}
        if path.endswith("/toggle"):
            return 200, {"enabled": True}
        if path.endswith("/settings/save"):
            return 404, {"error": "route not found"}
        return 200, {}
    status = ensure_telegram_bridge_live(settings={"TELEGRAM_BOT_TOKEN": "x"}, request=fake_request, timeout=5)
    # Bridge installed+enabled, but command mode not applied — must not claim it silently.
    assert status["ok"] is True
    assert status.get("command_mode_ok") is False
    assert status.get("warning")
    assert "command_mode:full_access" not in status["steps"]

def test_ensure_telegram_bridge_live_handles_already_installed_and_warns_missing_token():
    from ouroboros.colab_bootstrap import ensure_telegram_bridge_live
    def fake_request(method, path, body=None, timeout=None):
        if path == "/api/health":
            return 200, {}
        if path == "/api/marketplace/ouroboroshub/install":
            return 409, {"error": "telegram-bridge is already installed"}
        if path.endswith("/toggle"):
            return 200, {"enabled": True}
        return 200, {}
    status = ensure_telegram_bridge_live(request=fake_request, timeout=5)
    assert status["ok"] is True and "already_installed" in status["steps"]
    assert status.get("warning")  # empty TELEGRAM_BOT_TOKEN warning

def test_ensure_telegram_bridge_live_reports_server_not_ready():
    from ouroboros.colab_bootstrap import ensure_telegram_bridge_live
    status = ensure_telegram_bridge_live(request=lambda *a, **k: (503, {}), timeout=0.2)
    assert status["ok"] is False and "ready" in status["error"]

def test_ensure_telegram_bridge_live_stops_on_enable_error():
    from ouroboros.colab_bootstrap import ensure_telegram_bridge_live
    def fake_request(method, path, body=None, timeout=None):
        if path == "/api/health":
            return 200, {}
        if path.endswith("/toggle"):
            return 409, {"error": "cannot enable until requested key and permission grants are approved"}
        return 200, {}
    status = ensure_telegram_bridge_live(settings={"TELEGRAM_BOT_TOKEN": "x"}, request=fake_request, timeout=5)
    assert status["ok"] is False and "enable failed" in status["error"]
