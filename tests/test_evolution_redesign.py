from __future__ import annotations

import pathlib
import json
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def test_consciousness_model_defaults_to_main(monkeypatch):
    from ouroboros.config import get_consciousness_model

    monkeypatch.setenv("OUROBOROS_MODEL", "openai/gpt-5.5")
    monkeypatch.delenv("OUROBOROS_MODEL_CONSCIOUSNESS", raising=False)
    assert get_consciousness_model() == "openai/gpt-5.5"
    monkeypatch.setenv("OUROBOROS_MODEL_CONSCIOUSNESS", "anthropic/claude-opus-4.8")
    assert get_consciousness_model() == "anthropic/claude-opus-4.8"


def test_evolution_campaign_text_includes_objective(tmp_path, monkeypatch):
    from supervisor import queue
    from supervisor import state as supervisor_state

    supervisor_state.init(tmp_path)
    queue.init(tmp_path, 600, 1800)
    queue.start_evolution_campaign("Improve scheduler observability", source="test")

    text = queue.build_evolution_task_text(3)

    assert "EVOLUTION CAMPAIGN" in text
    assert "Improve scheduler observability" in text
    assert "normal advisory + triad + scope review flow" in text


def test_evolution_campaign_pause_resume_preserves_history(tmp_path):
    from supervisor import queue

    queue.init(tmp_path, 600, 1800)
    first = queue.start_evolution_campaign("Improve scheduler observability", source="test")
    queue.update_evolution_campaign_after_task("task1", cost_usd=0.5, result_status="succeeded", rounds=3)
    queue.pause_evolution_campaign("pause")
    resumed = queue.start_evolution_campaign("", source="test")

    assert resumed["id"] == first["id"]
    assert resumed["cycles_done"] == 1
    assert resumed["history"][0]["task_id"] == "task1"


def test_evolution_auto_stop_pauses_campaign(tmp_path, monkeypatch):
    from supervisor import queue
    from supervisor import state as supervisor_state

    supervisor_state.init(tmp_path)
    monkeypatch.setattr(queue, "send_with_budget", lambda *args, **kwargs: None)
    queue.init(tmp_path, 600, 1800)
    queue.init_queue_refs([], {}, {"value": 0})
    queue.start_evolution_campaign("Improve", source="test")
    st = supervisor_state.load_state()
    st["evolution_mode_enabled"] = True
    st["owner_chat_id"] = 1
    st["evolution_consecutive_failures"] = 3
    supervisor_state.save_state(st)

    queue.enqueue_evolution_task_if_needed()

    assert queue.get_evolution_status_snapshot()["campaign"]["status"] == "paused"


def test_evolution_enqueue_attaches_lightweight_transaction(tmp_path, monkeypatch):
    from supervisor import queue
    from supervisor import state as supervisor_state

    supervisor_state.init(tmp_path)
    monkeypatch.setattr(queue, "send_with_budget", lambda *args, **kwargs: None)
    queue.init(tmp_path, 600, 1800)
    pending = []
    queue.init_queue_refs(pending, {}, {"value": 0})
    st = supervisor_state.load_state()
    st["evolution_mode_enabled"] = True
    st["owner_chat_id"] = 1
    supervisor_state.save_state(st)

    queue.enqueue_evolution_task_if_needed()

    assert len(pending) == 1
    tx = pending[0]["metadata"]["evolution_transaction"]
    assert tx["transaction_id"]
    assert tx["task_id"] == pending[0]["id"]
    assert tx["base_head"] or tx["base_head"] == ""
    campaign = queue.get_evolution_status_snapshot()["campaign"]
    assert campaign["active_transaction"]["transaction_id"] == tx["transaction_id"]


def test_evolution_task_completion_preserves_live_transaction_updates(tmp_path):
    from supervisor import queue

    queue.init(tmp_path, 600, 1800)
    campaign = queue.start_evolution_campaign("Improve", source="test")
    stale = queue.begin_evolution_transaction("task1", cycle=1, campaign=campaign)
    queue.update_evolution_transaction(
        "task1",
        preflight_status="passed",
        triad_scope_status="passed",
        commit_sha="abc123",
        push_status="skipped_or_failed",
    )

    recorded = queue.update_evolution_campaign_after_task(
        "task1",
        cost_usd=1.0,
        result_status="succeeded",
        rounds=3,
        transaction=stale,
    )
    campaign = queue.get_evolution_status_snapshot()["campaign"]

    assert recorded["commit_sha"] == "abc123"
    assert campaign["history"][0]["transaction"]["commit_sha"] == "abc123"
    assert campaign["transaction_history"][0]["commit_sha"] == "abc123"
    assert "active_transaction" not in campaign

    campaign = queue.start_evolution_campaign("Improve", source="test")
    no_durability = queue.begin_evolution_transaction("task2", cycle=2, campaign=campaign)
    queue.update_evolution_campaign_after_task(
        "task2",
        cost_usd=0.0,
        result_status="infra_failed",
        rounds=0,
        transaction=no_durability,
    )
    campaign = queue.get_evolution_status_snapshot()["campaign"]
    assert campaign["active_transaction"]["task_id"] == "task2"
    assert "without commit or rescue evidence" in campaign["active_transaction"]["recovery_hint"]


def test_terminal_evolution_event_without_running_metadata_updates_transaction(tmp_path):
    from supervisor import queue
    from supervisor import state as supervisor_state
    from supervisor.events import _handle_task_done

    supervisor_state.init(tmp_path)
    queue.init(tmp_path, 600, 1800)
    campaign = queue.start_evolution_campaign("Improve", source="test")
    tx = queue.begin_evolution_transaction("task-cancel", cycle=1, campaign=campaign)

    ctx = SimpleNamespace(
        RUNNING={},
        WORKERS={},
        DRIVE_ROOT=tmp_path,
        REPO_DIR=tmp_path,
        load_state=supervisor_state.load_state,
        save_state=supervisor_state.save_state,
        append_jsonl=supervisor_state.append_jsonl,
        persist_queue_snapshot=lambda reason="": None,
        bridge=SimpleNamespace(push_log=lambda event: None),
    )

    _handle_task_done(
        {
            "type": "task_done",
            "task_id": "task-cancel",
            "task_type": "evolution",
            "result_status": "cancelled",
            "metadata": {"evolution_transaction": tx},
        },
        ctx,
    )

    campaign = queue.get_evolution_status_snapshot()["campaign"]
    assert campaign["history"][0]["task_id"] == "task-cancel"
    assert campaign["history"][0]["transaction"]["transaction_id"] == tx["transaction_id"]
    assert campaign["active_transaction"]["task_id"] == "task-cancel"


def test_cron_schedule_enqueues_once_when_due(tmp_path, monkeypatch):
    from supervisor import queue

    queue.init(tmp_path, 600, 1800)
    pending = []
    running = {}
    seq = {"value": 0}
    queue.init_queue_refs(pending, running, seq)
    queue.upsert_scheduled_task({
        "id": "hourly",
        "name": "Hourly",
        "enabled": True,
        "trigger": {"type": "cron", "expr": "* * * * *"},
        "next_run_at": "2000-01-01T00:00:00+00:00",
        "task": {"type": "task", "text": "scheduled work"},
    })

    queue.check_scheduled_tasks()
    queue.check_scheduled_tasks()

    assert len(pending) == 1
    assert pending[0]["text"] == "scheduled work"
    assert pending[0]["type"] == "task"
    assert pending[0]["root_task_id"] == pending[0]["id"]
    assert pending[0]["actor_id"] == "scheduler"
    assert pending[0]["delegation_role"] == "root"
    assert pending[0]["metadata"]["schedule_id"] == "hourly"


def test_scheduled_task_without_owner_chat_is_headless_safe(tmp_path):
    from supervisor import queue
    from supervisor import state as supervisor_state
    from ouroboros.task_results import load_task_result

    supervisor_state.init(tmp_path)
    queue.init(tmp_path, 600, 1800)
    pending = []
    queue.init_queue_refs(pending, {}, {"value": 0})
    queue.upsert_scheduled_task({
        "id": "headless",
        "name": "Headless",
        "enabled": True,
        "trigger": {"type": "cron", "expr": "* * * * *"},
        "next_run_at": "2000-01-01T00:00:00+00:00",
        "task": {"type": "task", "text": "scheduled work"},
    })

    queue.check_scheduled_tasks()

    assert pending[0]["chat_id"] == 0
    assert load_task_result(tmp_path, pending[0]["id"])["status"] == "scheduled"


def test_schedules_api_validates_five_field_cron(tmp_path):
    from ouroboros.gateway.schedules import api_schedules_delete, api_schedules_list, api_schedules_upsert
    from supervisor import queue

    queue.init(tmp_path, 600, 1800)
    app = Starlette(routes=[
        Route("/api/schedules", endpoint=api_schedules_list, methods=["GET"]),
        Route("/api/schedules", endpoint=api_schedules_upsert, methods=["POST"]),
        Route("/api/schedules/{schedule_id}", endpoint=api_schedules_delete, methods=["DELETE"]),
    ])
    app.state.drive_root = tmp_path
    client = TestClient(app)

    bad = client.post("/api/schedules", json={"name": "bad", "trigger": {"type": "cron", "expr": "* * * *"}})
    assert bad.status_code == 400
    bad_semantic = client.post("/api/schedules", json={"name": "bad-semantic", "trigger": {"type": "cron", "expr": "61 * * * *"}})
    assert bad_semantic.status_code == 400
    good = client.post("/api/schedules", json={"id": "ok", "name": "ok", "trigger": {"type": "cron", "expr": "* * * * *"}})
    assert good.status_code == 200
    assert client.get("/api/schedules").json()["tasks"][0]["name"] == "ok"
    disabled = client.post("/api/schedules", json={"name": "bad-bool", "enabled": "false", "trigger": {"type": "cron", "expr": "* * * * *"}})
    assert disabled.status_code == 400
    workspace = client.post("/api/schedules", json={
        "name": "bad-workspace",
        "trigger": {"type": "cron", "expr": "* * * * *"},
        "task": {"workspace_root": "/tmp/project", "text": "nope"},
    })
    assert workspace.status_code == 400
    nested_reserved = client.post("/api/schedules", json={
        "name": "bad-metadata",
        "trigger": {"type": "cron", "expr": "* * * * *"},
        "task": {"text": "nope", "metadata": {"delegation_role": "subagent"}},
    })
    assert nested_reserved.status_code == 400
    nested_actor = client.post("/api/schedules", json={
        "name": "bad-actor",
        "trigger": {"type": "cron", "expr": "* * * * *"},
        "task": {"text": "nope", "metadata": {"actor_id": "forged"}},
    })
    assert nested_actor.status_code == 400
    bad_metadata_type = client.post("/api/schedules", json={
        "name": "bad-metadata-type",
        "trigger": {"type": "cron", "expr": "* * * * *"},
        "task": {"text": "nope", "metadata": "forged"},
    })
    assert bad_metadata_type.status_code == 400
    internal_type = client.post("/api/schedules", json={
        "name": "bad-type",
        "trigger": {"type": "cron", "expr": "* * * * *"},
        "task": {"type": "evolution", "text": "nope"},
    })
    assert internal_type.status_code == 400
    bad_id = client.post("/api/schedules", json={
        "id": "bad/id",
        "name": "bad-id",
        "trigger": {"type": "cron", "expr": "* * * * *"},
    })
    assert bad_id.status_code == 400
    assert client.delete("/api/schedules/ok").json()["ok"] is True
    assert client.get("/api/schedules").json()["tasks"] == []


def test_skill_manifest_parses_scheduled_tasks():
    from ouroboros.contracts.skill_manifest import parse_skill_manifest_text

    manifest = parse_skill_manifest_text("""---
name: cron-demo
description: Cron demo
version: 0.1.0
type: extension
entry: plugin.py
permissions: [supervised_task]
scheduled_tasks:
  - name: refresh
    cron: "0 * * * *"
    timezone: Europe/Moscow
---
body
""")

    assert manifest.scheduled_tasks[0]["name"] == "refresh"
    assert manifest.validate() == []


def test_skill_manifest_rejects_invalid_scheduled_task_cron():
    from ouroboros.contracts.skill_manifest import SkillManifestError, parse_skill_manifest_text
    import pytest

    with pytest.raises(SkillManifestError):
        parse_skill_manifest_text("""---
name: cron-demo
description: Cron demo
version: 0.1.0
type: extension
entry: plugin.py
permissions: [supervised_task]
scheduled_tasks:
  - name: refresh
    cron: "61 * * * *"
---
body
""")


def test_skill_manifest_rejects_unsafe_scheduled_task_name():
    from ouroboros.contracts.skill_manifest import SkillManifestError, parse_skill_manifest_text
    import pytest

    with pytest.raises(SkillManifestError):
        parse_skill_manifest_text("""---
name: cron-demo
description: Cron demo
version: 0.1.0
type: extension
entry: plugin.py
permissions: [supervised_task]
scheduled_tasks:
  - name: "bad`name"
    cron: "0 * * * *"
---
body
""")


def test_skill_schedules_sync_into_core_scheduler(tmp_path):
    from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
    from supervisor import queue

    queue.init(tmp_path, 600, 1800)
    manifest = parse_skill_manifest_text("""---
name: cron-demo
description: Cron demo
version: 0.1.0
type: extension
entry: plugin.py
permissions: [supervised_task]
scheduled_tasks:
  - name: refresh
    cron: "0 * * * *"
---
body
""")
    skill = SimpleNamespace(
        name="cron-demo",
        manifest=manifest,
        enabled=True,
        load_error="",
        content_hash="abc",
        review=SimpleNamespace(status="pass", is_stale_for=lambda _hash: False),
    )

    report = queue.sync_skill_schedules([skill])
    schedules = queue.list_scheduled_tasks()["tasks"]

    assert report["changed"] is True
    assert schedules[0]["id"] == "skill-cron-demo-refresh"
    assert schedules[0]["enabled"] is True
    assert schedules[0]["trigger"]["expr"] == "0 * * * *"


def test_skill_schedule_sync_refreshes_next_run_on_cron_change(tmp_path):
    from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
    from supervisor import queue

    queue.init(tmp_path, 600, 1800)

    def make_skill(cron: str, content_hash: str):
        manifest = parse_skill_manifest_text(f"""---
name: cron-demo
description: Cron demo
version: 0.1.0
type: extension
entry: plugin.py
permissions: [supervised_task]
scheduled_tasks:
  - name: refresh
    cron: "{cron}"
---
body
""")
        return SimpleNamespace(
            name="cron-demo",
            manifest=manifest,
            enabled=True,
            load_error="",
            content_hash=content_hash,
            review=SimpleNamespace(status="pass", is_stale_for=lambda _hash: False),
        )

    queue.sync_skill_schedules([make_skill("0 * * * *", "a")])
    first = queue.list_scheduled_tasks()["tasks"][0]["next_run_at"]
    queue.sync_skill_schedules([make_skill("30 * * * *", "b")])
    second = queue.list_scheduled_tasks()["tasks"][0]["next_run_at"]

    assert first != second
    assert queue.list_scheduled_tasks()["tasks"][0]["trigger"]["expr"] == "30 * * * *"


def test_schedule_slug_avoids_long_name_collisions():
    from ouroboros.schedule_contract import schedule_slug

    a = schedule_slug("skill", "x" * 100, "a")
    b = schedule_slug("skill", "x" * 100, "b")
    assert a != b
    assert len(a) <= 81


def test_frontend_evolution_and_consciousness_controls_are_present():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    evolution = (root / "web" / "modules" / "evolution.js").read_text(encoding="utf-8")
    settings_ui = (root / "web" / "modules" / "settings_ui.js").read_text(encoding="utf-8")
    settings = (root / "web" / "modules" / "settings.js").read_text(encoding="utf-8")

    assert "evo-start" in evolution
    assert "evo-stop" in evolution
    assert "/evolve on" in evolution
    assert "/evolve off" in evolution
    # Start button is hard-disabled in light mode (self-modification gate).
    assert "runtime.runtime_mode" in evolution
    assert "startBtn.disabled = isLightMode" in evolution
    assert "s-model-consciousness" in settings_ui
    assert "s-local-consciousness" in settings_ui
    assert "OUROBOROS_EFFORT_CONSCIOUSNESS', 'high'" in settings


def test_evolution_checkpoint_records_and_reads(tmp_path):
    from ouroboros.evolution_checkpoints import CHECKPOINTS_REL, append_evolution_checkpoint
    from ouroboros.utils import iter_jsonl_objects

    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "identity.md").write_text("id", encoding="utf-8")

    append_evolution_checkpoint(
        tmp_path,
        repo,
        task_id="evo1",
        campaign={"id": "camp", "objective": "Improve"},
        result_status="succeeded",
        cost_usd=1.25,
        rounds=3,
        transaction={"transaction_id": "tx1", "commit_sha": "abc"},
    )

    rows = list(iter_jsonl_objects(tmp_path / CHECKPOINTS_REL))
    assert rows[0]["task_id"] == "evo1"
    assert rows[0]["campaign_id"] == "camp"
    assert rows[0]["rounds"] == 3
    assert rows[0]["transaction"]["transaction_id"] == "tx1"


def test_evolution_restart_uses_local_commit_not_origin_and_blocks_dirty_tree(tmp_path):
    from ouroboros.tools.control import _request_restart
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    repo.mkdir()
    _git = lambda *args: __import__("subprocess").run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)
    _git("init")
    _git("checkout", "-b", "ouroboros")
    (repo / "file.txt").write_text("ok\n", encoding="utf-8")
    _git("add", ".")
    __import__("subprocess").run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    head = __import__("subprocess").run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()
    ctx = ToolContext(repo_dir=repo, drive_root=tmp_path, current_task_type="evolution")
    ctx.last_reviewed_commit_sha = head

    result = _request_restart(ctx, "after local commit")

    assert "Restart requested" in result
    (repo / "file.txt").write_text("dirty\n", encoding="utf-8")
    ctx = ToolContext(repo_dir=repo, drive_root=tmp_path, current_task_type="evolution")

    result = _request_restart(ctx, "dirty")

    assert "RESTART_BLOCKED" in result
    assert "local reviewed commit" in result


def test_toggle_evolution_tool_accepts_objective(tmp_path, monkeypatch):
    from ouroboros.tools.control import _toggle_evolution

    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "advanced")
    pending = []
    result = _toggle_evolution(SimpleNamespace(pending_events=pending), True, objective="Improve cron")

    assert "ON" in result
    assert pending[0]["objective"] == "Improve cron"


def test_toggle_evolution_tool_refuses_in_light_mode(monkeypatch):
    from ouroboros.tools.control import _toggle_evolution

    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "light")
    pending = []
    result = _toggle_evolution(SimpleNamespace(pending_events=pending), True)

    # The tool's own result reflects the block; no event is queued.
    assert "light" in result.lower()
    assert pending == []


def test_memory_provenance_records_old_and_new_content(tmp_path):
    from ouroboros.tools.control import _update_identity
    from ouroboros.tools.knowledge import _knowledge_write
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    _knowledge_write(ctx, "facts", "old", mode="overwrite")
    _knowledge_write(ctx, "facts", "new", mode="overwrite")
    history = [
        json.loads(line)
        for line in (tmp_path / "memory" / "knowledge_history.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert history[-1]["old_content"] == "old"
    assert history[-1]["new_content"] == "new"

    _update_identity(ctx, "I am v1 with enough detail to satisfy the identity update length gate.")
    _update_identity(ctx, "I am v2 with enough detail to satisfy the identity update length gate.")
    identity_history = [
        json.loads(line)
        for line in (tmp_path / "memory" / "identity_journal.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert identity_history[-1]["old_content"].startswith("I am v1")
    assert identity_history[-1]["new_content"].startswith("I am v2")


def test_evolution_block_reason_depends_on_runtime_mode(monkeypatch):
    from supervisor import queue

    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "light")
    blocked = queue.evolution_block_reason()
    assert blocked
    assert "advanced" in blocked and "pro" in blocked

    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "advanced")
    assert queue.evolution_block_reason() == ""
    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "pro")
    assert queue.evolution_block_reason() == ""


def test_enqueue_evolution_blocked_in_light_mode(tmp_path, monkeypatch):
    from supervisor import queue
    from supervisor import state as supervisor_state

    supervisor_state.init(tmp_path)
    sent = []
    monkeypatch.setattr(queue, "send_with_budget", lambda chat_id, text, *a, **k: sent.append(text))
    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "light")
    queue.init(tmp_path, 600, 1800)
    pending = []
    queue.init_queue_refs(pending, {}, {"value": 0})
    queue.start_evolution_campaign("Improve", source="test")
    st = supervisor_state.load_state()
    st["evolution_mode_enabled"] = True
    st["owner_chat_id"] = 1
    supervisor_state.save_state(st)

    queue.enqueue_evolution_task_if_needed()

    assert pending == []
    assert supervisor_state.load_state()["evolution_mode_enabled"] is False
    assert queue.get_evolution_status_snapshot()["campaign"]["status"] == "paused"
    assert any("light" in m.lower() for m in sent)


def test_enqueue_evolution_omits_duplicate_cycle_message(tmp_path, monkeypatch):
    from supervisor import queue
    from supervisor import state as supervisor_state

    supervisor_state.init(tmp_path)
    monkeypatch.setattr(supervisor_state, "TOTAL_BUDGET_LIMIT", 100.0)
    sent = []
    monkeypatch.setattr(queue, "send_with_budget", lambda chat_id, text, *a, **k: sent.append(text))
    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "advanced")
    queue.init(tmp_path, 600, 1800)
    pending = []
    queue.init_queue_refs(pending, {}, {"value": 0})
    st = supervisor_state.load_state()
    st["evolution_mode_enabled"] = True
    st["owner_chat_id"] = 1
    st["spent_usd"] = 0.0
    supervisor_state.save_state(st)

    queue.enqueue_evolution_task_if_needed()

    assert len(pending) == 1
    assert pending[0]["type"] == "evolution"
    # The generic "... task started." lifecycle message is the only start bubble;
    # the redundant "Evolution #N: <id>" enqueue bubble is gone.
    assert not any("Evolution #" in m for m in sent)


def test_marketplace_helper_resyncs_skill_schedules(tmp_path, monkeypatch):
    from ouroboros.gateway import marketplace

    calls = []
    monkeypatch.setattr("supervisor.queue.resync_skill_schedules", lambda dr: calls.append(dr))

    marketplace._resync_skill_schedules_quiet(tmp_path)

    assert calls == [tmp_path]


def test_skill_schedule_sync_removes_vanished_skill_schedule(tmp_path):
    from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
    from supervisor import queue

    queue.init(tmp_path, 600, 1800)
    manifest = parse_skill_manifest_text("""---
name: cron-demo
description: Cron demo
version: 0.1.0
type: extension
entry: plugin.py
permissions: [supervised_task]
scheduled_tasks:
  - name: refresh
    cron: "0 * * * *"
---
body
""")
    skill = SimpleNamespace(
        name="cron-demo", manifest=manifest, enabled=True, load_error="",
        content_hash="abc",
        review=SimpleNamespace(status="pass", is_stale_for=lambda _h: False),
    )

    queue.sync_skill_schedules([skill])
    assert any(t.get("source") == "skill_manifest" for t in queue.list_scheduled_tasks()["tasks"])

    # Skill (or its scheduled_task) vanished → the record is removed entirely,
    # not left as a disabled tombstone.
    queue.sync_skill_schedules([])
    assert queue.list_scheduled_tasks()["tasks"] == []


def test_reflection_extract_trailing_json_parses_memory_and_backlog():
    from ouroboros.reflection import _extract_trailing_json

    text = (
        "Reflection body here.\n"
        'MEMORY_ACTIONS_JSON: [{"type": "scratchpad_append", "content": "note"}]\n'
        'BACKLOG_CANDIDATES_JSON: [{"summary": "s", "evidence": "e"}]'
    )
    body_after_backlog, backlog = _extract_trailing_json(text, "BACKLOG_CANDIDATES_JSON:")
    reflection_text, memory = _extract_trailing_json(body_after_backlog, "MEMORY_ACTIONS_JSON:")

    assert backlog == [{"summary": "s", "evidence": "e"}]
    assert memory == [{"type": "scratchpad_append", "content": "note"}]
    assert reflection_text.strip() == "Reflection body here."


def test_reflection_extract_trailing_json_is_order_independent():
    from ouroboros.reflection import _extract_trailing_json

    # Markers emitted in the reverse of the documented order must still both parse
    # (no silent memory-action loss).
    text = (
        "Reflection body.\n"
        'BACKLOG_CANDIDATES_JSON: [{"summary": "s", "evidence": "e"}]\n'
        'MEMORY_ACTIONS_JSON: [{"type": "knowledge_write", "content": "c", "topic": "t"}]'
    )
    after_backlog, backlog = _extract_trailing_json(text, "BACKLOG_CANDIDATES_JSON:")
    reflection_text, memory = _extract_trailing_json(after_backlog, "MEMORY_ACTIONS_JSON:")

    assert backlog == [{"summary": "s", "evidence": "e"}]
    assert memory == [{"type": "knowledge_write", "content": "c", "topic": "t"}]
    assert reflection_text.strip() == "Reflection body."


def test_reflection_validate_memory_actions_filters_types():
    from ouroboros.reflection import _validate_memory_actions

    raw = [
        {"type": "scratchpad_append", "content": "note"},
        {"type": "knowledge_write", "content": "fact", "topic": "facts"},
        {"type": "knowledge_write", "content": "no topic"},
        {"type": "identity_update_candidate", "content": "refine"},
        {"type": "delete_everything", "content": "nope"},
        {"type": "scratchpad_append", "content": "   "},
    ]
    actions = _validate_memory_actions(raw, "task1")

    assert [a["type"] for a in actions] == [
        "scratchpad_append", "knowledge_write", "identity_update_candidate",
    ]
    assert actions[1]["topic"] == "facts"
    assert all(a["task_id"] == "task1" for a in actions)


def test_apply_memory_actions_writes_to_parent_drive(tmp_path):
    from ouroboros.reflection import apply_memory_actions

    env = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path)
    actions = [
        {"type": "scratchpad_append", "content": "durable note", "task_id": "t1"},
        {"type": "knowledge_write", "content": "reusable fact", "topic": "review_process", "task_id": "t1"},
        {"type": "identity_update_candidate", "content": "I value rigor", "task_id": "t1"},
    ]

    assert apply_memory_actions(env, actions) == 3

    knowledge = (tmp_path / "memory" / "knowledge" / "review_process.md").read_text(encoding="utf-8")
    assert "reusable fact" in knowledge
    assert (tmp_path / "memory" / "knowledge_history.jsonl").exists()

    scratchpad = (tmp_path / "memory" / "scratchpad.md").read_text(encoding="utf-8")
    assert "durable note" in scratchpad
    assert "IDENTITY UPDATE CANDIDATE" in scratchpad
    # Identity is never auto-written; the candidate stays in the scratchpad only.
    identity_path = tmp_path / "memory" / "identity.md"
    assert not identity_path.exists() or "I value rigor" not in identity_path.read_text(encoding="utf-8")


def test_runtime_context_includes_schedule_digest(tmp_path):
    from ouroboros.context import build_runtime_section

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "scheduled_tasks.json").write_text(json.dumps({
        "tasks": [{
            "id": "hourly", "name": "Hourly", "enabled": True,
            "trigger": {"type": "cron", "expr": "0 * * * *"},
            "timezone": "Europe/Moscow", "next_run_at": "2030-01-01T00:00:00+00:00",
        }]
    }), encoding="utf-8")
    env = SimpleNamespace(
        repo_dir=tmp_path, drive_root=tmp_path,
        drive_path=lambda rel: tmp_path / rel,
    )

    section = build_runtime_section(env, {"id": "t1", "type": "task"})

    assert "scheduled_tasks" in section
    assert "hourly" in section
    assert "0 * * * *" in section


def test_cli_evolve_start_refused_in_light_mode(monkeypatch):
    from ouroboros import cli

    class FakeClient:
        def __init__(self):
            self.posts = []

        def request(self, method, path, body=None):
            if method == "GET" and path == "/api/state":
                return {"runtime_mode": "light"}
            self.posts.append((method, path, body))
            return {"status": "ok"}

    fake = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda args: fake)
    monkeypatch.setattr(cli, "_print_json", lambda *a, **k: None)

    rc = cli._evolve_command(SimpleNamespace(evolve_command="start", objective=[]))

    assert rc == 1
    # Never POSTed the /evolve on command in light mode.
    assert fake.posts == []


def test_assign_tasks_cancels_pending_evolution_in_light_mode(tmp_path, monkeypatch):
    import supervisor.workers as workers
    import supervisor.queue as queue
    from supervisor import state as supervisor_state
    from ouroboros.task_results import load_task_result

    supervisor_state.init(tmp_path)
    monkeypatch.setattr(supervisor_state, "TOTAL_BUDGET_LIMIT", 100.0)
    st = supervisor_state.load_state()
    st["spent_usd"] = 0.0
    supervisor_state.save_state(st)

    orig_drive = workers.DRIVE_ROOT
    orig_q_drive = queue.DRIVE_ROOT
    workers.DRIVE_ROOT = tmp_path
    queue.DRIVE_ROOT = tmp_path
    workers.WORKERS.clear()
    workers.RUNNING.clear()
    workers.PENDING[:] = [{"id": "evo1", "type": "evolution", "text": "x"}]
    queue.PENDING = workers.PENDING
    monkeypatch.setattr(queue, "evolution_block_reason", lambda: "light blocked")
    monkeypatch.setattr(workers, "send_with_budget", lambda *a, **k: None)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)

    try:
        workers.assign_tasks()
    finally:
        workers.DRIVE_ROOT = orig_drive
        queue.DRIVE_ROOT = orig_q_drive
        workers.PENDING[:] = []

    assert all(t.get("type") != "evolution" for t in workers.PENDING)
    assert load_task_result(tmp_path, "evo1")["status"] == "cancelled"


def test_toggle_evolution_tool_blocked_in_light_mode(monkeypatch):
    from supervisor import events

    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "light")
    state = {"owner_chat_id": 7}
    sent = []
    ctx = SimpleNamespace(
        load_state=lambda: state,
        save_state=lambda s: state.update(s),
        send_with_budget=lambda chat_id, text, *a, **k: sent.append((chat_id, text)),
        PENDING=[],
        sort_pending=lambda: None,
        persist_queue_snapshot=lambda **k: None,
    )

    events._handle_toggle_evolution({"enabled": True, "objective": "x"}, ctx)

    assert state.get("evolution_mode_enabled") is not True
    assert sent and "light" in sent[0][1].lower()
