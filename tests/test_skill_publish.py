from __future__ import annotations

import base64
import json
import pathlib

import pytest

from ouroboros.skill_loader import compute_content_hash
from ouroboros.tools import skill_publish
from ouroboros.tools.registry import ToolContext, ToolRegistry


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    repo = tmp_path / "repo"
    drive = tmp_path / "data"
    repo.mkdir()
    drive.mkdir()
    return ToolContext(
        repo_dir=repo,
        drive_root=drive,
        messages=[{"role": "user", "content": "Submit skill demo to OuroborosHub"}],
    )


def _write_skill(ctx: ToolContext, name: str = "demo", version: str = "0.1.0", *, reviewed: bool = True) -> pathlib.Path:
    skill_dir = pathlib.Path(ctx.drive_root) / "skills" / "external" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n"
        f"name: {name}\n"
        f"description: Demo skill\n"
        f"version: {version}\n"
        f"type: extension\n"
        f"entry: plugin.py\n"
        f"when_to_use: User wants a demo.\n"
        f"---\n"
        f"# Demo\n",
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text("def register(api):\n    pass\n", encoding="utf-8")
    if reviewed:
        digest = compute_content_hash(skill_dir, manifest_entry="plugin.py", manifest_scripts=[])
        state_dir = pathlib.Path(ctx.drive_root) / "state" / "skills" / name
        state_dir.mkdir(parents=True)
        (state_dir / "review.json").write_text(
            json.dumps({"status": "pass", "content_hash": digest}),
            encoding="utf-8",
        )
    return skill_dir


def test_validation_blocks_unreviewed_skill(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _write_skill(ctx, reviewed=False)
    monkeypatch.setattr(skill_publish, "github_token_from_env_or_settings", lambda: "token")

    result = skill_publish._submit_skill_to_hub(ctx, "demo", confirm_public_submission=True, permission_statement="human asked to submit")

    assert "SUBMIT_BLOCKED" in result
    assert "fresh PASS review" in result


def test_validation_blocks_missing_token(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _write_skill(ctx)
    monkeypatch.setattr(skill_publish, "github_token_from_env_or_settings", lambda: "")

    result = skill_publish._submit_skill_to_hub(ctx, "demo", confirm_public_submission=True, permission_statement="human asked to submit")

    assert "SUBMIT_BLOCKED" in result
    assert "GITHUB_TOKEN missing" in result


def test_validation_accepts_env_token(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _write_skill(ctx)
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setattr(skill_publish, "get_ouroboroshub_catalog_url", lambda: "https://bad.invalid/catalog.json")

    result = skill_publish._submit_skill_to_hub(ctx, "demo", confirm_public_submission=True, permission_statement="human asked to submit")

    assert "GITHUB_TOKEN missing" not in result


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://raw.githubusercontent.com/joi-lab/OuroborosHub/main/catalog.json",
            ("joi-lab", "OuroborosHub", "main"),
        ),
        (
            "https://raw.githubusercontent.com/o/r/release/v1/catalog.json",
            ("o", "r", "release/v1"),
        ),
    ],
)
def test_destination_parsing(url, expected):
    assert skill_publish._parse_hub_destination(url) == expected


def test_destination_parsing_blocks_wrong_host():
    with pytest.raises(ValueError):
        skill_publish._parse_hub_destination("https://example.com/catalog.json")


def test_submit_requires_explicit_public_confirmation(tmp_path):
    ctx = _ctx(tmp_path)
    _write_skill(ctx)

    result = skill_publish._submit_skill_to_hub(ctx, "demo")

    assert "explicit public submission confirmation" in result


def test_add_mode_payload(tmp_path):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx)
    # Avoid coupling this unit to validation internals; construct the entry from payload files.
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")
    payload = skill_publish._skill_payload_files(skill_dir, loaded.manifest)
    entry = skill_publish._catalog_entry("demo", loaded.manifest, payload)
    mode, catalog = skill_publish._update_catalog({"skills": []}, entry)

    assert mode == "add"
    assert catalog["skills"][0]["slug"] == "demo"
    plugin_meta = next(item for item in catalog["skills"][0]["files"] if item["path"] == "plugin.py")
    assert plugin_meta["sha256"] == __import__("hashlib").sha256((skill_dir / "plugin.py").read_bytes()).hexdigest()


def test_payload_excludes_control_plane_sidecars(tmp_path):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx)
    (skill_dir / ".self_authored.json").write_text('{"chat_id":123}\n', encoding="utf-8")
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")
    payload = skill_publish._skill_payload_files(skill_dir, loaded.manifest)

    assert ".self_authored.json" not in {item["path"] for item in payload}


def test_payload_blocks_real_secret_values(tmp_path):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx)
    (skill_dir / "plugin.py").write_text(
        'OPENROUTER_API_KEY = "sk-or-' + ("A" * 40) + '"\n',
        encoding="utf-8",
    )
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")

    with pytest.raises(ValueError) as exc:
        skill_publish._skill_payload_files(skill_dir, loaded.manifest)

    assert "secret value" in str(exc.value)


def test_payload_allows_env_key_names_without_secret_values(tmp_path):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx)
    (skill_dir / "plugin.py").write_text(
        "import os\nOPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')\n",
        encoding="utf-8",
    )
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")
    payload = skill_publish._skill_payload_files(skill_dir, loaded.manifest)

    assert any(item["path"] == "plugin.py" for item in payload)


@pytest.mark.parametrize(
    "secret",
    [
        "github_pat_" + ("A" * 40),
        "gho_" + ("A" * 40),
        "ghu_" + ("A" * 40),
        "ghs_" + ("A" * 40),
        "ghr_" + ("A" * 40),
        "sk-proj-" + ("A" * 40),
        "sk-svcacct-" + ("A" * 40),
        "sk-admin-" + ("A" * 40),
        "do not publish sk-ant-api03_" + ("A" * 40),
        "Authorization: Bearer " + ("A" * 32),
        "OUROBOROS_NETWORK_PASSWORD=" + ("A" * 32),
        '{"password":"hunter2"}',
        '{"name":"demo","password":"hunter2"}',
        '{"nested":{"password":"hunter2"}}',
        '{"openRouterApiKey":"abc12345"}',
        'openRouterApiKey = "abc12345"',
        'config["password"] = "hunter2"',
        "config['api_key'] = 'abc12345'",
        'config = {"api_key": "abc12345"}',
        'config = {"password": "hunter2"}',
        'config = {"nested": {"password": "hunter2"}}',
        'requests.get(url, headers={"Authorization": "Bearer shorttoken"})',
        'const config = { apiKey: "abc12345" };',
        'const config = { nested: { password: "hunter2" } };',
        'headers = { Authorization: "Bearer shorttoken" }',
        'AWS_ACCESS_KEY_ID="AKIA' + ("A" * 16) + '"',
        'AWS_SECRET_ACCESS_KEY="' + ("A" * 40) + '"',
        'STRIPE_SECRET_KEY="sk_live_' + ("A" * 32) + '"',
        '{"password":"prod_db_password_2026"}',
        'PASSWORD="prod_db_password_2026"',
        'api_key = "prod_api_key_2026"',
        'API_KEY = os.getenv("API_KEY", "prod_api_key_2026")',
        'password = os.getenv("PASSWORD", "correct horse battery staple!")',
        'api_key = api.get_settings(["API_KEY"]).get("API_KEY", "prod_api_key_2026")',
        'const apiKey = process.env.API_KEY || "prod_api_key_2026";',
        'PASSWORD="PROD_DB_PASSWORD_2026"',
        'API_KEY="PROD_API_KEY_2026"',
        'SECRET_KEY="prod_secret_key_2026"',
        'DATABASE_URL="postgres://user:pass@example.com/db"',
        'headers["Authorization"] = "Bearer shorttoken"',
        'API_KEY="abc12345"',
        'AUTHORIZATION="Bearer shorttoken"',
        'PASSWORD="correct horse battery staple!"',
    ],
)
def test_payload_blocks_modern_secret_values(tmp_path, secret):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx)
    (skill_dir / "plugin.py").write_text(secret + "\n", encoding="utf-8")
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")

    with pytest.raises(ValueError) as exc:
        skill_publish._skill_payload_files(skill_dir, loaded.manifest)

    assert "secret value" in str(exc.value)


@pytest.mark.parametrize(
    "placeholder",
    [
        "OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '')",
        'OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")',
        "API_KEY = api_key",
        "API_KEY = get_key()",
        "API_KEY = self.api_key",
        "password = password",
        "Authorization = auth_header",
        'headers = {"Authorization": f"Bearer {token}"}',
        'headers = {"Authorization": "Bearer " + token}',
        "Authorization: Bearer <token>",
        "Authorization: Bearer {token}",
        "Password: Configure this in Settings before use.",
        "API_KEY = settings.OPENROUTER_API_KEY",
        "TOKEN = process.env.GITHUB_TOKEN",
        "LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')",
        "PORT = os.getenv('PORT', '8000')",
        "BASE_URL = os.getenv('BASE_URL', 'https://api.example.com')",
        "theme = settings.get('THEME', 'dark')",
        "NODE_ENV = process.env.NODE_ENV || 'production'",
        "token: str = ''",
        "api_key: str | None = None",
        "def auth(token: str):\n    pass",
        '{"api_key":"set_via_env"}',
        'PASSWORD="<set in settings>"',
        'TOKEN="${TOKEN}"',
    ],
)
def test_payload_allows_secret_placeholders(tmp_path, placeholder):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx)
    (skill_dir / "plugin.py").write_text(placeholder + "\n", encoding="utf-8")
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")
    payload = skill_publish._skill_payload_files(skill_dir, loaded.manifest)

    assert any(item["path"] == "plugin.py" for item in payload)


@pytest.mark.parametrize(
    "safe_config",
    [
        "max_tokens = 4096",
        "TOKENIZERS_PARALLELISM=false",
        "token_budget: 8192",
    ],
)
def test_payload_allows_non_secret_token_config_names(tmp_path, safe_config):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx)
    (skill_dir / "plugin.py").write_text(safe_config + "\n", encoding="utf-8")
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")
    payload = skill_publish._skill_payload_files(skill_dir, loaded.manifest)

    assert any(item["path"] == "plugin.py" for item in payload)


def test_update_mode_payload_replaces_existing_entry(tmp_path):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx, version="0.2.0")
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")
    payload = skill_publish._skill_payload_files(skill_dir, loaded.manifest)
    entry = skill_publish._catalog_entry("demo", loaded.manifest, payload)
    mode, catalog = skill_publish._update_catalog(
        {"skills": [{"slug": "demo", "version": "0.1.0", "files": []}]},
        entry,
    )

    assert mode == "update"
    assert len([item for item in catalog["skills"] if item["slug"] == "demo"]) == 1
    assert catalog["skills"][0]["version"] == "0.2.0"


def test_noop_same_version():
    entry = {"slug": "demo", "version": "0.1.0", "files": []}

    with pytest.raises(RuntimeError) as exc:
        skill_publish._update_catalog({"skills": [{"slug": "demo", "version": "0.1.0"}]}, entry)

    assert "SUBMIT_NOOP" in str(exc.value)


def test_idempotent_branch_collision(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(skill_publish, "_gh_cmd", lambda *args, **kwargs: "{}")

    with pytest.raises(RuntimeError) as exc:
        skill_publish._ensure_branch(ctx, "me", "OuroborosHub", "submit/demo-v0.1.0", "abc")

    assert "branch" in str(exc.value)
    assert "already exists" in str(exc.value)


def test_pr_body_fallback_when_llm_fails(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx)
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")
    payload = skill_publish._skill_payload_files(skill_dir, loaded.manifest)

    class BrokenLLM:
        def chat(self, **_kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(skill_publish, "LLMClient", lambda: BrokenLLM())
    body = skill_publish._generate_pr_body(ctx, "add", "demo", loaded.manifest, payload, "hello", skill_dir)

    assert "## Note" in body
    assert "hello" in body
    assert "Fresh PASS review" in body


def test_pr_body_allows_secret_setting_names_in_note(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx)
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")
    payload = skill_publish._skill_payload_files(skill_dir, loaded.manifest)

    class BrokenLLM:
        def chat(self, **_kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr(skill_publish, "LLMClient", lambda: BrokenLLM())

    body = skill_publish._generate_pr_body(
        ctx,
        "add",
        "demo",
        loaded.manifest,
        payload,
        "Uses OPENROUTER_API_KEY from Settings after owner grant.",
        skill_dir,
    )

    assert "OPENROUTER_API_KEY" in body


def test_pr_body_blocks_real_secret_value_in_note(tmp_path):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx)
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")
    payload = skill_publish._skill_payload_files(skill_dir, loaded.manifest)

    with pytest.raises(ValueError):
        skill_publish._generate_pr_body(
            ctx,
            "add",
            "demo",
            loaded.manifest,
            payload,
            "token sk-ant-" + ("A" * 40),
            skill_dir,
        )


def test_full_flow_happy_path(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _write_skill(ctx)
    monkeypatch.setattr(skill_publish, "github_token_from_env_or_settings", lambda: "token")
    monkeypatch.setattr(
        skill_publish,
        "get_ouroboroshub_catalog_url",
        lambda: "https://raw.githubusercontent.com/joi-lab/OuroborosHub/main/catalog.json",
    )
    calls = []

    def fake_gh(args, _ctx, timeout=30, input_data=None):
        calls.append((args, input_data))
        joined = " ".join(args)
        if args[:2] == ["api", "/user"]:
            return "octocat"
        if args[:3] == ["repo", "view", "octocat/OuroborosHub"]:
            return json.dumps({"name": "OuroborosHub"})
        if "merge-upstream" in joined:
            return json.dumps({"merged": True})
        if "/git/refs/heads/main" in joined:
            return json.dumps({"object": {"sha": "base-sha"}})
        if "contents/catalog.json" in joined:
            raw = base64.b64encode(json.dumps({"skills": []}).encode("utf-8")).decode("ascii")
            return json.dumps({"content": raw})
        if "/git/ref/heads/submit/demo-v0.1.0" in joined:
            return "⚠️ GH_ERROR: Not Found"
        if "/git/refs" in joined:
            return json.dumps({"ref": "refs/heads/submit/demo-v0.1.0", "object": {"sha": "fork-branch-sha"}})
        if args[:2] == ["api", "graphql"]:
            return json.dumps({"data": {"createCommitOnBranch": {"commit": {"url": "https://commit"}}}})
        if args[:3] == ["pr", "create", "--repo"]:
            return "https://github.com/joi-lab/OuroborosHub/pull/1"
        raise AssertionError(args)

    monkeypatch.setattr(skill_publish, "_gh_cmd", fake_gh)
    monkeypatch.setattr(skill_publish, "_generate_pr_body", lambda *args, **kwargs: "body")

    result = skill_publish._submit_skill_to_hub(ctx, "demo", confirm_public_submission=True, permission_statement="human asked to submit")

    assert "PR opened" in result
    assert "Mode: add" in result
    graphql_call = next(call for call in calls if call[0][:2] == ["api", "graphql"])
    assert json.loads(graphql_call[1])["variables"]["input"]["expectedHeadOid"] == "fork-branch-sha"


def test_payload_size_limit(tmp_path):
    ctx = _ctx(tmp_path)
    skill_dir = _write_skill(ctx)
    (skill_dir / "large.bin").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    from ouroboros.skill_loader import find_skill

    loaded = find_skill(pathlib.Path(ctx.drive_root), "demo")

    with pytest.raises(ValueError):
        skill_publish._skill_payload_files(skill_dir, loaded.manifest)


def test_submit_tool_registered_and_policy_covered(tmp_path):
    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    assert "submit_skill_to_hub" in registry.available_tools()
    core_names = {schema["function"]["name"] for schema in registry.schemas(core_only=True)}
    assert "submit_skill_to_hub" in core_names
