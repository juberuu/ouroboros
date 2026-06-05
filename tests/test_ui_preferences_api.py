from __future__ import annotations

from starlette.applications import Starlette
from ouroboros.gateway.router import collect_routes


def test_ui_preferences_round_trip_and_normalization(tmp_path):
    from starlette.testclient import TestClient

    app = Starlette(routes=collect_routes(data_dir=tmp_path))
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        initial = client.get("/api/ui/preferences")
        assert initial.status_code == 200
        assert initial.json() == {
            "widget_order": [],
            "nested_subagents_expanded": True,
        }

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
        assert client.post("/api/ui/preferences", json={"unknown": True}).status_code == 400
