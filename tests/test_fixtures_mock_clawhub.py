from __future__ import annotations

import urllib.request
import zipfile
from io import BytesIO

from tests.fixtures_mock_clawhub import MockClawHubServer


def test_mock_clawhub_serves_skill_archive():
    with MockClawHubServer() as server:
        with urllib.request.urlopen(f"{server.base_url}/download/duck", timeout=5) as resp:  # noqa: S310 - local fixture
            data = resp.read()

    with zipfile.ZipFile(BytesIO(data)) as zf:
        names = set(zf.namelist())

    assert "duck/SKILL.md" in names
