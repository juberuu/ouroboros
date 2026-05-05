from __future__ import annotations

import json
import urllib.request

from tests.fixtures_mock_llm import MockLLMServer


def test_mock_llm_serves_openai_compatible_chat():
    with MockLLMServer() as server:
        req = urllib.request.Request(
            f"{server.base_url}/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - local fixture
            payload = json.loads(resp.read().decode("utf-8"))
    assert payload["choices"][0]["message"]["content"] == "OK"
