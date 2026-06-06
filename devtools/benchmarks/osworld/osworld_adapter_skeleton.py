#!/usr/bin/env python3
"""Fail-closed OSWorld adapter skeleton.

This file intentionally does not implement OSWorld scoring. It verifies that a
runnable official OSWorld environment and Ouroboros computer-use surface exist
before a future adapter is allowed to proceed.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any


def _http_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip().startswith("{") else {"raw": raw, "status": getattr(resp, "status", None)}


def _outside(path: Path, forbidden: list[Path]) -> bool:
    resolved = path.expanduser().resolve(strict=False)
    for root in forbidden:
        try:
            resolved.relative_to(root.expanduser().resolve(strict=False))
            return False
        except ValueError:
            continue
    return True


def preflight(
    *,
    osworld_root: Path,
    ouroboros_url: str,
    osworld_server_url: str,
    computer_use_payload: Path,
    output_root: Path,
    repo_root: Path,
    data_root: Path,
) -> dict[str, Any]:
    failures: list[str] = []
    if not osworld_root.is_dir():
        failures.append(f"official OSWorld checkout not found: {osworld_root}")
    if not (osworld_root / "run.py").exists() and not (osworld_root / "evaluation_examples").exists():
        failures.append(f"OSWorld checkout shape is not recognized: {osworld_root}")
    if not computer_use_payload.exists():
        failures.append(f"computer_use payload is missing: {computer_use_payload}")
    if not _outside(output_root, [repo_root, data_root]):
        failures.append(f"output root must be outside repo and runtime data: {output_root}")
    try:
        _http_json(ouroboros_url.rstrip("/") + "/api/state")
    except Exception as exc:
        failures.append(f"Ouroboros server is not reachable: {type(exc).__name__}: {exc}")
    try:
        urllib.request.urlopen(osworld_server_url.rstrip("/") + "/", timeout=5).read(1)
    except Exception as exc:
        failures.append(f"OSWorld desktop/control server is not reachable: {type(exc).__name__}: {exc}")
    return {"ok": not failures, "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--osworld-root", required=True)
    parser.add_argument("--ouroboros-url", default="http://127.0.0.1:8765")
    parser.add_argument("--osworld-server-url", required=True)
    parser.add_argument("--computer-use-payload", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--repo-root", default="/Users/anton/Ouroboros/repo")
    parser.add_argument("--data-root", default="/Users/anton/Ouroboros/data")
    args = parser.parse_args()

    result = preflight(
        osworld_root=Path(args.osworld_root).expanduser(),
        ouroboros_url=args.ouroboros_url,
        osworld_server_url=args.osworld_server_url,
        computer_use_payload=Path(args.computer_use_payload).expanduser(),
        output_root=Path(args.output_root).expanduser(),
        repo_root=Path(args.repo_root).expanduser(),
        data_root=Path(args.data_root).expanduser(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        return 2
    print("OSWorld runnable adapter is not implemented in this release; preflight passed only.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
