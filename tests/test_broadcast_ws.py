from __future__ import annotations

import asyncio
import json


class _DeadWebSocket:
    async def send_text(self, _text):
        raise RuntimeError("dead client")


def test_broadcast_partial_failure_uses_module_data_dir(tmp_path, monkeypatch):
    import server

    monkeypatch.delenv("OUROBOROS_DATA_DIR", raising=False)
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    with server._ws_lock:
        original_clients = list(server._ws_clients)
        server._ws_clients.clear()
        server._ws_clients.append(_DeadWebSocket())
    try:
        asyncio.run(server.broadcast_ws({"type": "unit_test"}))
    finally:
        with server._ws_lock:
            server._ws_clients.clear()
            server._ws_clients.extend(original_clients)

    events_path = tmp_path / "logs" / "events.jsonl"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[-1]["type"] == "broadcast_partial_failure"
    assert rows[-1]["msg_type"] == "unit_test"
    assert rows[-1]["dead_clients"] == 1
