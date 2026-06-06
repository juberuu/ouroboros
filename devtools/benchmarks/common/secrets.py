"""Secret-loading helpers that never print credential values."""

from __future__ import annotations

import json
import os
import pathlib


SECRET_KEYS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
)


def settings_path(default_home: pathlib.Path | None = None) -> pathlib.Path:
    home = default_home or pathlib.Path("/Users/anton/Ouroboros")
    return pathlib.Path(os.environ.get("OUROBOROS_SETTINGS_PATH") or home / "data" / "settings.json")


def load_secret_env(path: pathlib.Path | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in SECRET_KEYS:
        value = os.environ.get(key)
        if value:
            values[key] = value
    settings_file = path or settings_path()
    try:
        loaded = json.loads(settings_file.read_text(encoding="utf-8"))
    except Exception:
        loaded = {}
    if isinstance(loaded, dict):
        for key in SECRET_KEYS:
            value = loaded.get(key)
            if value and key not in values:
                values[key] = str(value)
    return values


def redacted_env_summary(env: dict[str, str]) -> dict[str, bool]:
    return {key: bool(env.get(key)) for key in SECRET_KEYS}
