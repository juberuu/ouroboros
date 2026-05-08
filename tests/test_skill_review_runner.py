from __future__ import annotations

import pathlib
from types import SimpleNamespace

import ouroboros.skill_lifecycle_queue as lifecycle_queue
from ouroboros.skill_loader import (
    SkillReviewState,
    compute_content_hash,
    load_enabled,
    load_review_state,
    load_skill_grants,
    save_review_state,
)
from ouroboros.skill_review import SkillReviewOutcome
from ouroboros.skill_review_runner import _review_result_message, run_skill_review_lifecycle_blocking


def _reset_queue() -> None:
    lifecycle_queue._events.clear()
    lifecycle_queue._active = None
    lifecycle_queue._lock = None
    lifecycle_queue._dedupe_jobs.clear()


def _build_extension(skills_root: pathlib.Path, name: str) -> pathlib.Path:
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            "description: Review runner test.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            "permissions: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text("def register(api):\n    return None\n", encoding="utf-8")
    return skill_dir


def _build_keyed_extension(skills_root: pathlib.Path, name: str) -> pathlib.Path:
    skill_dir = _build_extension(skills_root, name)
    manifest = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    manifest = manifest.replace("permissions: []\n", "permissions: [read_settings]\nenv_from_settings: [OPENROUTER_API_KEY]\n")
    (skill_dir / "SKILL.md").write_text(manifest, encoding="utf-8")
    return skill_dir


def _mark_self_authored(skill_dir: pathlib.Path) -> None:
    payload = {
        "schema_version": 1,
        "origin": "self_authored",
        "task_id": "task-1",
        "created_at": "2026-05-07T00:00:00+00:00",
    }
    (skill_dir / ".self_authored.json").write_text(
        __import__("json").dumps(payload) + "\n",
        encoding="utf-8",
    )
    state = skill_dir.parents[2] / "state" / "skills" / skill_dir.name
    state.mkdir(parents=True, exist_ok=True)
    (state / "self_authored.json").write_text(__import__("json").dumps(payload) + "\n", encoding="utf-8")


def test_blocking_review_lifecycle_uses_single_progress_card(tmp_path, monkeypatch):
    _reset_queue()
    sent = []
    reconcile_calls = []
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = tmp_path / "skills"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir()
    skill_dir = _build_extension(skills_root, "alpha")
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])

    def fake_send(*args, **kwargs):
        sent.append((args, kwargs))

    def fake_review(_ctx, skill_name):
        return SkillReviewOutcome(
            skill_name=skill_name,
            status="pass",
            content_hash=content_hash,
            reviewer_models=["fake/reviewer"],
            findings=[{"item": "manifest_schema", "verdict": "PASS"}],
            error="",
        )

    def fake_reconcile(_ctx, skill_name, **_kwargs):
        reconcile_calls.append(lifecycle_queue.queue_snapshot()["active"]["target"])
        return "extension_loaded", "review_passed"

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", fake_send)
    monkeypatch.setattr("ouroboros.skill_review_runner._reconcile_deps_after_pass_review", lambda *_a, **_k: ("installed", ""))
    monkeypatch.setattr("ouroboros.skill_review_runner._reconcile_extension_payload", fake_reconcile)

    payload = run_skill_review_lifecycle_blocking(
        ctx,
        "alpha",
        source="test",
        review_impl=fake_review,
        repo_path=str(skills_root),
    )

    assert payload["status"] == "pass"
    assert payload["deps_status"] == "installed"
    assert payload["extension_action"] == "extension_loaded"
    assert reconcile_calls == ["alpha"]

    progress_messages = [
        args[1]
        for args, kwargs in sent
        if kwargs.get("is_progress")
        and str(kwargs.get("task_id") or "").startswith("skill_lifecycle_review_alpha_")
    ]
    assert any("Running tri-model review" in message for message in progress_messages)
    assert any("Installing dependencies" in message for message in progress_messages)
    assert any("Reloading extension" in message for message in progress_messages)
    assert any("completed" in message and "Review pass: PASS manifest_schema" in message for message in progress_messages)
    assert not any(kwargs.get("task_id") in {"skill_lifecycle_review", "api_skill_review"} for _args, kwargs in sent)


def test_review_result_message_prefers_non_pass_findings_and_marks_omissions():
    long_reason = "x" * 400
    outcome = SkillReviewOutcome(
        skill_name="alpha",
        status="fail",
        findings=[
            {"item": "manifest_schema", "verdict": "PASS", "reason": "ok"},
            {"item": "extension_namespace_discipline", "verdict": "FAIL", "reason": long_reason},
        ],
    )

    message = _review_result_message(outcome)

    assert message.startswith("Review fail: FAIL extension_namespace_discipline")
    assert "manifest_schema" not in message
    assert "[omitted " in message
    assert "full findings in Skills page" in message


def test_self_authored_review_lifecycle_uses_triad(tmp_path, monkeypatch):
    _reset_queue()
    sent = []
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = drive_root / "skills" / "external"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir(parents=True)
    skill_dir = _build_keyed_extension(skills_root, "alpha")
    _mark_self_authored(skill_dir)
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **kw: sent.append((a, kw)))
    monkeypatch.setattr(
        "ouroboros.skill_review_runner.load_settings",
        lambda: {"OPENROUTER_API_KEY": "sk-test"},
    )
    monkeypatch.setattr(
        "ouroboros.skill_review_runner._reconcile_deps_after_pass_review",
        lambda *_a, **_k: ("not_required", ""),
    )
    monkeypatch.setattr(
        "ouroboros.skill_review_runner._reconcile_extension_payload",
        lambda *_a, **_k: ("extension_loaded", "ready"),
    )

    def fake_review(_ctx, _skill_name):
        outcome = SkillReviewOutcome(
            skill_name="alpha",
            status="pass",
            content_hash=content_hash,
            reviewer_models=["reviewer-a", "reviewer-b", "reviewer-c"],
            findings=[],
        )
        save_review_state(
            drive_root,
            "alpha",
            SkillReviewState(
                status=outcome.status,
                content_hash=outcome.content_hash,
                findings=outcome.findings,
                reviewer_models=outcome.reviewer_models,
            ),
        )
        return outcome

    payload = run_skill_review_lifecycle_blocking(
        ctx,
        "alpha",
        source="test",
        review_impl=fake_review,
        repo_path=str(drive_root / "skills"),
    )

    assert payload["status"] == "pass"
    assert payload["auto_flow"] is False
    assert load_enabled(drive_root, "alpha") is False
    review = load_review_state(drive_root, "alpha")
    assert review.status == "pass"
    assert review.content_hash == content_hash
    assert review.reviewer_models == ["reviewer-a", "reviewer-b", "reviewer-c"]
    grants = load_skill_grants(drive_root, "alpha")
    assert grants["granted_keys"] == []


def test_self_authored_review_does_not_enable_when_deps_fail(tmp_path, monkeypatch):
    _reset_queue()
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = drive_root / "skills" / "external"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir(parents=True)
    skill_dir = _build_extension(skills_root, "alpha")
    _mark_self_authored(skill_dir)
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **kw: None)
    monkeypatch.setattr("ouroboros.skill_review_runner._reconcile_deps_after_pass_review", lambda *_a, **_k: ("failed", "pip exploded"))

    payload = run_skill_review_lifecycle_blocking(
        ctx,
        "alpha",
        source="test",
        review_impl=lambda _ctx, _skill: SkillReviewOutcome(
            skill_name="alpha",
            status="pass",
            content_hash=compute_content_hash(skill_dir, manifest_entry="plugin.py"),
            reviewer_models=["reviewer"],
        ),
        repo_path=str(drive_root / "skills"),
    )

    assert payload["status"] == "pass"
    assert payload["deps_status"] == "failed"
    assert "pip exploded" in payload["deps_error"]
    assert load_enabled(drive_root, "alpha") is False


def test_self_authored_review_requires_configured_requested_keys(tmp_path, monkeypatch):
    _reset_queue()
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = drive_root / "skills" / "external"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir(parents=True)
    skill_dir = _build_keyed_extension(skills_root, "alpha")
    _mark_self_authored(skill_dir)
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **kw: None)
    monkeypatch.setattr("ouroboros.skill_review_runner.load_settings", lambda: {})

    payload = run_skill_review_lifecycle_blocking(
        ctx,
        "alpha",
        source="test",
        review_impl=lambda _ctx, _skill: SkillReviewOutcome(
            skill_name="alpha",
            status="pass",
            content_hash=compute_content_hash(skill_dir, manifest_entry="plugin.py"),
            reviewer_models=["reviewer"],
        ),
        repo_path=str(drive_root / "skills"),
    )

    assert payload["status"] == "pass"
    assert load_enabled(drive_root, "alpha") is False
