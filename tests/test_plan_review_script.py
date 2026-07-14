from __future__ import annotations

import asyncio
import importlib.util
import json
import types


def _load_script_module(repo_root):
    path = repo_root / "scripts" / "run_plan_review.py"
    spec = importlib.util.spec_from_file_location("run_plan_review_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_plan_review_script_assembles_governance_context(monkeypatch, tmp_path):
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    script = _load_script_module(repo)
    captured = {}

    async def fake_run_slots(ctx, models, system_prompt, user_content):
        captured["models"] = list(models)
        captured["system_prompt"] = system_prompt
        captured["user_content"] = user_content
        return [
            {
                "model": "fake/reviewer",
                "request_model": "fake/reviewer",
                "text": "## PROPOSALS\n\nNo changes.\n\nAGGREGATE: GREEN",
                "error": None,
                "tokens_in": 1,
                "tokens_out": 1,
                "cost": 0.0,
            }
        ]

    from ouroboros.tools import plan_review

    monkeypatch.setattr(plan_review, "_get_review_models", lambda: ["fake/reviewer"])
    monkeypatch.setattr(plan_review, "_run_plan_review_slots", fake_run_slots)

    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n\nImplement the accepted phase.\n", encoding="utf-8")
    handoff_path = tmp_path / "plan_task_handoffs.json"
    handoff_path.write_text(
        '{"schema_version": 1, "task_ids": ["scout-1"], '
        '"wait": {"tasks": {"scout-1": {"status": "completed", '
        '"role": "planning-scout-1", "result": "inspect the existing SSOT"}}}}',
        encoding="utf-8",
    )
    args = types.SimpleNamespace(
        plan=str(plan_path),
        goal="Test plan-review script",
        context_level="minimal",
        files_to_touch=[],
        context_notes="unit-test context",
        extra_context=[],
        scout_handoff=[str(handoff_path)],
        include_tests=False,
        drive_root=str(tmp_path / "drive"),
        output="",
    )

    output = asyncio.run(script._run(args))

    assert "RESOLVED PLAN REVIEW CONFIG" in output
    assert captured["models"] == ["fake/reviewer"]
    for marker in (
        "## BIBLE.md",
        "## DEVELOPMENT.md",
        "## ARCHITECTURE.md",
        "## CHECKLISTS.md",
    ):
        assert marker in captured["system_prompt"]
    assert "Implement the accepted phase." in captured["user_content"]
    assert "**Context level:** minimal" in captured["user_content"]
    assert "inspect the existing SSOT" in captured["user_content"]
    # The path is embedded in a JSON forensic ref, so Windows backslashes are
    # escaped in the prompt text.
    assert json.dumps(str(handoff_path))[1:-1] in captured["user_content"]
    assert "scout_handoff_refs" in output


def test_run_plan_review_script_has_no_personal_key_fallback():
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "run_plan_review.py").read_text(encoding="utf-8")
    assert "file1.txt" not in text


def test_run_plan_review_script_defaults_external_subject_to_external_framing(tmp_path):
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    script = _load_script_module(repo)
    external = (tmp_path / "external").resolve()
    external.mkdir()

    assert script._plan_class_for_subject(external) == "external"
    assert script._plan_class_for_subject(repo.resolve()) == "self_mod"
    assert script._plan_class_for_subject(external, "research") == "research"
