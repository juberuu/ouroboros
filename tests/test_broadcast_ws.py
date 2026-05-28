from __future__ import annotations

import asyncio
import json


class _DeadWebSocket:
    async def send_text(self, _text):
        raise RuntimeError("dead client")


def test_broadcast_partial_failure_uses_module_data_dir(tmp_path, monkeypatch):
    from ouroboros.gateway import ws

    monkeypatch.delenv("OUROBOROS_DATA_DIR", raising=False)
    monkeypatch.setattr(ws, "DATA_DIR", tmp_path)

    with ws._ws_lock:
        original_clients = list(ws._ws_clients)
        ws._ws_clients.clear()
        ws._ws_clients.append(_DeadWebSocket())
    try:
        asyncio.run(ws.broadcast_ws({"type": "unit_test"}))
    finally:
        with ws._ws_lock:
            ws._ws_clients.clear()
            ws._ws_clients.extend(original_clients)

    events_path = tmp_path / "logs" / "events.jsonl"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[-1]["type"] == "broadcast_partial_failure"
    assert rows[-1]["msg_type"] == "unit_test"
    assert rows[-1]["dead_clients"] == 1
