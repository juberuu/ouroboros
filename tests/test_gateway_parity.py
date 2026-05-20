from __future__ import annotations

import pathlib
import re

from ouroboros.gateway.contracts import HTTP_ENDPOINTS, WS_MESSAGE_TYPES
from ouroboros.gateway.router import collect_routes


def test_gateway_contract_endpoint_index_matches_router_and_types(tmp_path):
    tokens: set[str] = set()
    for route in collect_routes(data_dir=tmp_path):
        path = getattr(route, "path", "")
        if not path:
            continue
        methods = getattr(route, "methods", None)
        if methods is None:
            tokens.add(f"WS {path}")
            continue
        normalized = sorted(m for m in methods if m not in {"HEAD", "OPTIONS"})
        if set(normalized) == {"DELETE", "GET", "PATCH", "POST", "PUT"}:
            tokens.add(f"ANY {path}")
        else:
            for method in normalized:
                tokens.add(f"{method} {path}")
    contract_tokens = set(HTTP_ENDPOINTS)
    missing = contract_tokens - tokens
    extra = tokens - contract_tokens
    assert not missing, f"HTTP_ENDPOINTS includes routes not mounted by gateway.router: {sorted(missing)}"
    assert not extra, f"gateway.router mounts routes missing from HTTP_ENDPOINTS: {sorted(extra)}"
    text = (pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "api_types.js").read_text(
        encoding="utf-8"
    )
    assert "GATEWAY_CONTRACT_VERSION" in text
    for name in ("StateResponse", "HealthResponse", "SettingsMeta", "ChatInbound", "ChatOutbound", "UploadResponse"):
        assert re.search(rf"@typedef \{{Object\}} {name}\b", text), f"api_types.js missing {name}"
    assert "setup_contract" in text
    assert {"chat", "command", "log", "heartbeat"} <= set(WS_MESSAGE_TYPES)
