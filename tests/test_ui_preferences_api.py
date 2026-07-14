from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from starlette.applications import Starlette

from ouroboros.gateway.router import collect_routes


def test_ui_preferences_round_trip_and_normalization(tmp_path):
    from starlette.testclient import TestClient

    from ouroboros.projects_registry import create_project, increment_project_visible_revision

    app = Starlette(routes=collect_routes(data_dir=tmp_path))
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        initial = client.get("/api/ui/preferences")
        assert initial.status_code == 200
        assert initial.json() == {
            "widget_order": [],
            "nested_subagents_expanded": False,
            "sidebar_width": 0,
            "project_panel_width": 0,
            "project_seen_revision": {},
            "project_last_viewed": {},
            "project_hidden": {},
        }

        create_project(tmp_path, "racer", name="Racer")
        create_project(tmp_path, "site", name="Site")
        increment_project_visible_revision(tmp_path, project_id="racer")
        increment_project_visible_revision(tmp_path, project_id="racer")
        increment_project_visible_revision(tmp_path, project_id="site")

        # Paint ACKs merge monotonically. A future value is clamped to the current
        # visible revision; stale tabs cannot move a cursor backwards.
        a = client.post("/api/ui/preferences", json={"project_seen_revision": {"racer": 1}})
        assert a.status_code == 200
        assert a.json()["project_seen_revision"] == {"racer": 1}
        b = client.post("/api/ui/preferences", json={"project_seen_revision": {"site": 999}})
        assert b.json()["project_seen_revision"] == {"racer": 1, "site": 1}
        stale = client.post("/api/ui/preferences", json={"project_seen_revision": {"racer": 0}})
        assert stale.json()["project_seen_revision"]["racer"] == 1
        future = client.post("/api/ui/preferences", json={"project_seen_revision": {"racer": 999}})
        assert future.json()["project_seen_revision"]["racer"] == 2
        unknown = client.post("/api/ui/preferences", json={"project_seen_revision": {"missing": 8}})
        assert "missing" not in unknown.json()["project_seen_revision"]
        assert client.get("/api/ui/preferences").json()["project_seen_revision"]["racer"] == 2

        # One-minor aliases remain accepted but are loud no-ops.
        legacy = client.post(
            "/api/ui/preferences",
            json={
                "project_hidden": {"racer": True},
                "project_last_viewed": {"racer": "2026-06-15T01:00:00Z"},
            },
        )
        assert legacy.status_code == 200
        assert legacy.json()["project_hidden"] == {}
        assert legacy.json()["project_last_viewed"] == {}
        assert legacy.json()["warnings"][0]["type"] == "deprecated_ui_preferences_ignored"

        # Resizable side-section widths round-trip and clamp (v6.33.0).
        widths = client.post(
            "/api/ui/preferences",
            json={"sidebar_width": 99999, "project_panel_width": 10},
        )
        assert widths.status_code == 200
        assert widths.json()["sidebar_width"] == 560  # clamped to max
        assert widths.json()["project_panel_width"] == 320  # clamped to min
        zero = client.post("/api/ui/preferences", json={"sidebar_width": 0})
        assert zero.status_code == 200
        assert zero.json()["sidebar_width"] == 0

        response = client.post(
            "/api/ui/preferences",
            json={
                "widget_order": ["skill:two", "skill:one", "skill:two", ""],
                "nested_subagents_expanded": False,
            },
        )
        assert response.status_code == 200
        assert response.json()["widget_order"] == ["skill:two", "skill:one"]
        assert response.json()["nested_subagents_expanded"] is False

        persisted = client.get("/api/ui/preferences")
        assert persisted.status_code == 200
        assert persisted.json()["widget_order"] == ["skill:two", "skill:one"]
        assert persisted.json()["nested_subagents_expanded"] is False

        partial_order = client.post(
            "/api/ui/preferences",
            json={"widget_order": ["skill:three"]},
        )
        assert partial_order.status_code == 200
        assert partial_order.json()["widget_order"] == ["skill:three"]
        assert partial_order.json()["nested_subagents_expanded"] is False

        partial_nested = client.post(
            "/api/ui/preferences",
            json={"nested_subagents_expanded": True},
        )
        assert partial_nested.status_code == 200
        assert partial_nested.json()["widget_order"] == ["skill:three"]
        assert partial_nested.json()["nested_subagents_expanded"] is True

        assert client.post("/api/ui/preferences", json=[]).status_code == 400
        assert client.post("/api/ui/preferences", json={"widget_order": "bad"}).status_code == 400
        assert client.post("/api/ui/preferences", json={"project_seen_revision": {"racer": "bad"}}).status_code == 400
        assert client.post("/api/ui/preferences", json={"unknown": True}).status_code == 400


def test_ui_preferences_concurrent_paint_acks_are_monotonic(tmp_path):
    from ouroboros.gateway.ui_preferences import api_ui_preferences_post
    from ouroboros.projects_registry import create_project, increment_project_visible_revision

    create_project(tmp_path, "race", name="Race")
    for _ in range(5):
        increment_project_visible_revision(tmp_path, project_id="race")
    barrier = threading.Barrier(2)

    def _post(revision: int) -> int:
        async def _json():
            barrier.wait(timeout=5)
            return {"project_seen_revision": {"race": revision}}

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(drive_root=tmp_path)),
            json=_json,
        )
        return asyncio.run(api_ui_preferences_post(request)).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(_post, (2, 5)))
    assert statuses == [200, 200]
    stored = json.loads((tmp_path / "state" / "ui_preferences.json").read_text(encoding="utf-8"))
    assert stored["project_seen_revision"]["race"] == 5
