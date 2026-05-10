"""Shared helpers for the Ouroboros test suite.

These functions are reused across multiple ``tests/test_*.py`` modules to
avoid duplicated boilerplate (extension-loader cleanup, claude_agent_sdk
mock installation, lazy module getters). They are intentionally plain
module-level callables, not fixtures — many callers need them at module
import time.
"""
from __future__ import annotations

import importlib
import sys
import types


def clean_extension_runtime_state() -> None:
    """Reset every extension_loader namespace to a pristine state.

    Superset of cleanup logic that previously lived (with minor variations)
    in ``test_skill_exec.py``, ``test_extensions_api.py`` and
    ``test_extension_loader.py``. Extra clears are inert when the namespace
    is already empty, so the superset is safe for every caller.
    """
    from ouroboros import extension_loader

    with extension_loader._lock:
        extension_loader._extensions.clear()
        extension_loader._extension_modules.clear()
        extension_loader._load_failures.clear()
        extension_loader._unloading.clear()
        extension_loader._lifecycle_locks.clear()
        extension_loader._tools.clear()
        extension_loader._routes.clear()
        extension_loader._ws_handlers.clear()
        extension_loader._ui_tabs.clear()
        extension_loader._settings_sections.clear()
        extension_loader.set_ws_broadcaster(None)


def ensure_claude_agent_sdk_mock() -> None:
    """Install a lightweight ``claude_agent_sdk`` mock when truly absent.

    Uses ``importlib.util.find_spec`` so an installed-but-not-yet-imported
    SDK is never masked. Idempotent — safe to call from multiple modules at
    import time.
    """
    import importlib.util as _ilu
    try:
        spec = _ilu.find_spec("claude_agent_sdk")
        sdk_available = spec is not None
    except (ValueError, ModuleNotFoundError):
        sdk_available = "claude_agent_sdk" in sys.modules
    if sdk_available:
        return
    mock_sdk = types.ModuleType("claude_agent_sdk")
    mock_sdk.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {})
    mock_sdk.ClaudeSDKClient = type("ClaudeSDKClient", (), {})
    mock_sdk.HookMatcher = type("HookMatcher", (), {"__init__": lambda self, **kw: None})
    mock_sdk.AssistantMessage = type("AssistantMessage", (), {})
    mock_sdk.ResultMessage = type("ResultMessage", (), {})
    mock_sdk.query = lambda **kw: None
    sys.modules["claude_agent_sdk"] = mock_sdk


def lazy_import(module_path: str):
    """Return ``importlib.import_module(module_path)``.

    Tiny wrapper kept for tests that previously used ``_get_*_module``
    helpers (``ouroboros.tools.git``/``review``/``registry``,
    ``supervisor.git_ops``, ``ouroboros.tools.claude_advisory_review``).
    Centralising the call lets us drop the per-file ``sys.path.insert(0, REPO)``
    boilerplate once and only once.
    """
    return importlib.import_module(module_path)
