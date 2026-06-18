"""v6.37.0 guard (C4.5 + C4.4): converting a task into a project re-homes the
conversation to the project thread.

- the owner's ORIGINAL request is copied into the project chat as its first
  message (so the project reads from the request, not a mid-flight bubble);
- a subagent's progress classifies into the root project's thread by lineage;
- the main chat keeps NEITHER the raw owner mirror NOR raw subagent chat —
  only a sanitized pointer/progress (the card becomes a chip on the client).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace


def _request(tmp_path, body):
    async def _json():
        return body

    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(drive_root=tmp_path)),
        json=_json,
    )


def _history(tmp_path, chat_id):
    from ouroboros.gateway.history import make_chat_history_endpoint

    endpoint = make_chat_history_endpoint(tmp_path)
    resp = asyncio.run(endpoint(SimpleNamespace(query_params={"chat_id": str(chat_id), "limit": "50"})))
    return json.loads(resp.body.decode("utf-8"))["messages"]


def test_conversion_mirrors_owner_request_and_rehomes_subagents(tmp_path):
    from ouroboros.gateway.projects import api_project_from_task
    from ouroboros.projects_registry import project_chat_for_task

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "chat.jsonl").write_text(
        # The owner's original request lives in the MAIN chat (chat_id 1). It is
        # logged at receive time (server.py log_chat "in") with NO task_id — so it
        # is not auto-reclassified and stays in main; the mirror copy is what makes
        # the project thread show it.
        json.dumps({"ts": "2026-06-18T00:00:00Z", "direction": "in", "chat_id": 1,
                    "text": "build cyberpunk racing"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "logs" / "progress.jsonl").write_text("", encoding="utf-8")

    resp = asyncio.run(api_project_from_task(_request(
        tmp_path,
        {"task_id": "root", "id": "task-root", "objective_hint": "build cyberpunk racing"},
    )))
    payload = json.loads(resp.body.decode("utf-8"))
    proj_chat = int(payload["project"]["chat_id"])
    assert proj_chat == project_chat_for_task(tmp_path, "root") > 0

    # A subagent emits progress; its rows carry main chat_id but lineage roots at "root".
    with (tmp_path / "logs" / "progress.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-06-18T00:01:00Z", "content": "child working",
                             "chat_id": 1, "task_id": "child", "parent_task_id": "root",
                             "root_task_id": "root", "subagent_event": "progress"}) + "\n")

    project_view = _history(tmp_path, proj_chat)
    # owner request seeded as the first project message...
    assert any(m.get("role") == "user" and "cyberpunk racing" in m.get("text", "") for m in project_view)
    # ...and the subagent progress re-homes into the project thread by lineage (C4.4).
    assert any(m.get("task_id") == "child" for m in project_view)

    main_view = _history(tmp_path, 1)
    # The mirrored owner row is project-owned: it must NOT leak into the main chat
    # (the original main-chat owner row stays; the mirror does not duplicate there).
    owner_user_rows = [m for m in main_view if m.get("role") == "user" and "cyberpunk racing" in m.get("text", "")]
    assert len(owner_user_rows) == 1
    # The subagent's raw chat stays out of main; only sanitized progress may mirror.
    assert all(not (m.get("task_id") == "child" and m.get("role") == "user") for m in main_view)


def test_repeat_conversion_does_not_duplicate_owner_message(tmp_path):
    """A second from-task call (double broadcast / retry) must not append the
    owner's request to the project thread again (C4.5 idempotency)."""
    from ouroboros.gateway.projects import api_project_from_task

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "chat.jsonl").write_text("", encoding="utf-8")

    body = {"task_id": "root", "id": "task-root", "objective_hint": "build cyberpunk racing"}
    asyncio.run(api_project_from_task(_request(tmp_path, body)))
    asyncio.run(api_project_from_task(_request(tmp_path, body)))

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    mirrored = [r for r in rows if r.get("direction") == "in" and "cyberpunk racing" in r.get("text", "")]
    assert len(mirrored) == 1
