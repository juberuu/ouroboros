"""Phase 4 extension loader — PluginAPI-backed ``type: extension`` support.

Extensions are full Python modules that run IN-PROCESS inside the
Ouroboros runtime, unlike ``type: script`` skills which spawn a
sandboxed subprocess via ``skill_exec``. An extension's ``plugin.py``
exports a single ``register(api: PluginAPI)`` function that calls the
narrow PluginAPI surface to attach tools, HTTP routes, and WebSocket
handlers.

Because extensions share the Ouroboros process address space the
review gate is stricter than for ``type: script``:

- Every registration is namespaced to ``ext.<skill>.…`` so a plugin
  cannot shadow a built-in tool / route / WS message type.
- The manifest MUST declare a permission for every capability the
  extension actually uses; the runtime enforces the denylist side of
  that contract even if review missed the declaration (mirrors the
  ``_FORBIDDEN_ENV_FORWARD_KEYS`` defense-in-depth pattern from Phase 3).
- The same skill-review tri-model pipeline vets the plugin source.
  When ``review.status`` is not ``pass`` the loader refuses to import
  the plugin, so the process never touches the extension's module
  namespace.

An extension that is later disabled via ``toggle_skill`` is
"unregistered" — every tool/route/ws handler it attached gets torn
down (tracked per-skill in the loader) and the module is purged from
``sys.modules`` so a subsequent re-enable re-imports cleanly.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pathlib
import sys
import threading
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, Sequence

from ouroboros.contracts.plugin_api import (
    ExtensionRegistrationError,
    FORBIDDEN_EXTENSION_SETTINGS,
    VALID_EXTENSION_PERMISSIONS,
)
from ouroboros.skill_loader import (
    LoadedSkill,
    SkillPayloadUnreadable,
    compute_content_hash,
    discover_skills,
    skill_state_dir,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registration bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _ExtensionRegistrations:
    """Per-extension registry of attached surfaces, for clean unload."""

    tools: List[str] = field(default_factory=list)
    routes: List[str] = field(default_factory=list)
    ws_handlers: List[str] = field(default_factory=list)
    ui_tabs: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.tools or self.routes or self.ws_handlers or self.ui_tabs)


# Module-global, lock-guarded registries. Separate dicts make unload
# O(names_attached_by_extension) rather than O(total_registrations).
_lock = threading.RLock()
_extensions: Dict[str, _ExtensionRegistrations] = {}
_extension_modules: Dict[str, ModuleType] = {}
_tools: Dict[str, Any] = {}            # {"ext.<skill>.<name>": ToolEntry-like}
_routes: Dict[str, Any] = {}           # {"/api/extensions/<skill>/<path>": handler_spec}
_ws_handlers: Dict[str, Any] = {}      # {"ext.<skill>.<message_type>": handler}
_ui_tabs: Dict[str, Any] = {}          # {"<skill>:<tab_id>": tab_spec}


# ---------------------------------------------------------------------------
# PluginAPI implementation
# ---------------------------------------------------------------------------


def _assert_namespace_path(path: str) -> str:
    """Return a normalised relative path for route registration or raise."""
    rel = str(path or "").strip()
    if not rel:
        raise ExtensionRegistrationError("path must be non-empty")
    if rel.startswith("/"):
        raise ExtensionRegistrationError(
            f"path must be relative, not absolute: {rel!r}"
        )
    if ".." in pathlib.PurePosixPath(rel).parts:
        raise ExtensionRegistrationError(
            f"path must not contain '..' segments: {rel!r}"
        )
    return rel


def _assert_tool_name(name: str) -> str:
    candidate = str(name or "").strip()
    if not candidate:
        raise ExtensionRegistrationError("tool name must be non-empty")
    if not candidate.replace("_", "").isalnum():
        raise ExtensionRegistrationError(
            f"tool name must be alnum/underscore only: {candidate!r}"
        )
    return candidate


def _assert_ws_message_type(message_type: str) -> str:
    candidate = str(message_type or "").strip()
    if not candidate:
        raise ExtensionRegistrationError("ws message_type must be non-empty")
    # WS message types are dot-separated; extensions may use sub-types
    # freely under their ``ext.<skill>.`` prefix.
    for part in candidate.split("."):
        if not part or not part.replace("_", "").isalnum():
            raise ExtensionRegistrationError(
                f"ws message_type must be dot-separated alnum/underscore: {candidate!r}"
            )
    return candidate


class PluginAPIImpl:
    """Concrete ``PluginAPI`` the loader hands to ``register(api)``.

    Each instance is bound to exactly one skill name + manifest
    permission set + state dir so the bindings cannot escape the
    calling extension's scope.
    """

    def __init__(
        self,
        *,
        skill_name: str,
        permissions: Sequence[str],
        env_allowlist: Sequence[str],
        state_dir: pathlib.Path,
        settings_reader: Callable[[], Dict[str, Any]],
    ) -> None:
        self._skill = skill_name
        self._permissions = frozenset(str(p).strip() for p in (permissions or []))
        self._env_allow = frozenset(str(k).strip() for k in (env_allowlist or []))
        self._state_dir = pathlib.Path(state_dir)
        self._settings_reader = settings_reader

    # --- internal helpers ---

    def _require(self, perm: str) -> None:
        if perm not in VALID_EXTENSION_PERMISSIONS:
            raise ExtensionRegistrationError(
                f"unknown extension permission {perm!r}"
            )
        if perm not in self._permissions:
            raise ExtensionRegistrationError(
                f"skill {self._skill!r} cannot {perm!r} "
                f"— manifest permissions={sorted(self._permissions)}"
            )

    # --- registration ---

    def register_tool(
        self,
        name: str,
        handler: Callable[..., str],
        *,
        description: str,
        schema: Dict[str, Any],
        timeout_sec: int = 60,
    ) -> None:
        self._require("tool")
        short = _assert_tool_name(name)
        full = f"ext.{self._skill}.{short}"
        with _lock:
            if full in _tools:
                raise ExtensionRegistrationError(
                    f"tool {full!r} already registered"
                )
            _tools[full] = {
                "name": full,
                "handler": handler,
                "description": str(description or ""),
                "schema": dict(schema or {}),
                "timeout_sec": max(1, int(timeout_sec)),
                "skill": self._skill,
            }
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).tools.append(full)

    def register_route(
        self,
        path: str,
        handler: Callable[..., Any],
        *,
        methods: Sequence[str] = ("GET",),
    ) -> None:
        self._require("route")
        rel = _assert_namespace_path(path)
        mount = f"/api/extensions/{self._skill}/{rel}"
        norm_methods = tuple(str(m).upper() for m in methods)
        with _lock:
            if mount in _routes:
                raise ExtensionRegistrationError(
                    f"route {mount!r} already registered"
                )
            _routes[mount] = {
                "path": mount,
                "handler": handler,
                "methods": norm_methods,
                "skill": self._skill,
            }
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).routes.append(mount)

    def register_ws_handler(
        self,
        message_type: str,
        handler: Callable[..., Any],
    ) -> None:
        self._require("ws_handler")
        short = _assert_ws_message_type(message_type)
        full = f"ext.{self._skill}.{short}"
        with _lock:
            if full in _ws_handlers:
                raise ExtensionRegistrationError(
                    f"ws handler {full!r} already registered"
                )
            _ws_handlers[full] = {
                "type": full,
                "handler": handler,
                "skill": self._skill,
            }
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).ws_handlers.append(full)

    def register_ui_tab(
        self,
        tab_id: str,
        title: str,
        *,
        icon: str = "extension",
        render: Dict[str, Any] | None = None,
    ) -> None:
        self._require("widget")
        clean_tab = _assert_tool_name(tab_id)  # same syntax rules
        key = f"{self._skill}:{clean_tab}"
        with _lock:
            if key in _ui_tabs:
                raise ExtensionRegistrationError(
                    f"ui tab {key!r} already registered"
                )
            _ui_tabs[key] = {
                "skill": self._skill,
                "tab_id": clean_tab,
                "title": str(title or clean_tab),
                "icon": str(icon or "extension"),
                "render": dict(render or {}),
                "phase5_pending": True,
            }
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).ui_tabs.append(key)

    # --- runtime access ---

    def log(self, level: str, message: str, **fields: Any) -> None:
        lvl = str(level or "info").lower()
        levels = {"debug": 10, "info": 20, "warning": 30, "error": 40}
        log.log(
            levels.get(lvl, 20),
            "[ext %s] %s %s",
            self._skill,
            message,
            fields if fields else "",
        )

    def get_settings(self, keys: Sequence[str]) -> Dict[str, Any]:
        if "read_settings" not in self._permissions:
            # Read without the permission → empty dict (fail silent for
            # forward-compat, but never leak). Reviewer catches the
            # missing permission.
            return {}
        settings = self._settings_reader() or {}
        out: Dict[str, Any] = {}
        for raw_key in keys or ():
            key = str(raw_key).strip()
            if not key or key in FORBIDDEN_EXTENSION_SETTINGS:
                continue
            if key not in self._env_allow:
                continue
            if key in settings:
                out[key] = settings[key]
        return out

    def get_state_dir(self) -> str:
        return str(self._state_dir)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _plugin_entry_path(skill: LoadedSkill) -> Optional[pathlib.Path]:
    """Return the plugin.py path the manifest declared, or None."""
    entry = str(skill.manifest.entry or "").strip()
    if not entry:
        return None
    candidate = (skill.skill_dir / entry).resolve()
    try:
        candidate.relative_to(skill.skill_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _module_key(skill_name: str) -> str:
    return f"ouroboros._extensions.{skill_name}"


def load_extension(
    skill: LoadedSkill,
    settings_reader: Callable[[], Dict[str, Any]],
    *,
    drive_root: Optional[pathlib.Path] = None,
) -> Optional[str]:
    """Load one ``type: extension`` skill into the runtime.

    Returns ``None`` on success, or an error string suitable for
    surfacing to the operator via the Skills UI on failure. The skill
    must be (a) ``type: extension``, (b) ``enabled=True``, (c) review
    status ``pass`` with fresh content hash — otherwise the loader
    refuses silently.

    ``drive_root`` is the Ouroboros data-plane root (the same path the
    loader / ``find_skill`` are keyed against). When omitted, we fall
    back to the default user-home ``~/Ouroboros/data`` — but callers
    that already know the drive root (e.g. ``reload_all``) should pass
    it explicitly so the extension's state directory lines up with the
    rest of the durable-state plane.
    """
    if not skill.manifest.is_extension():
        return f"skill {skill.name!r} is not type=extension"
    if skill.load_error:
        return f"skill {skill.name!r} has load_error: {skill.load_error}"
    if not skill.enabled:
        return f"skill {skill.name!r} is disabled"
    try:
        current_hash = compute_content_hash(
            skill.skill_dir,
            manifest_entry=skill.manifest.entry,
            manifest_scripts=skill.manifest.scripts,
        )
    except SkillPayloadUnreadable as exc:
        return (
            f"skill {skill.name!r} payload unreadable at load time: "
            f"{exc}. Fix filesystem state and re-enable."
        )
    if skill.review.status != "pass" or skill.review.content_hash != current_hash:
        return (
            f"skill {skill.name!r} must carry a fresh PASS review "
            f"(status={skill.review.status!r}, "
            f"stale={skill.review.content_hash != current_hash})"
        )

    entry_path = _plugin_entry_path(skill)
    if entry_path is None:
        return (
            f"skill {skill.name!r} manifest.entry does not resolve to a "
            "file inside the skill directory"
        )

    if drive_root is None:
        drive_root = pathlib.Path.home() / "Ouroboros" / "data"
    state_dir = skill_state_dir(drive_root, skill.name)

    module_key = _module_key(skill.name)
    try:
        # Build a package-style spec so a multi-file extension can use
        # intra-skill imports (``from .helper import X``) without
        # manual ``sys.path`` wiring. ``submodule_search_locations``
        # is the ``__path__`` value the loader installs on the module.
        spec = importlib.util.spec_from_file_location(
            module_key,
            entry_path,
            submodule_search_locations=[str(skill.skill_dir)],
        )
        if spec is None or spec.loader is None:
            return f"skill {skill.name!r}: importlib could not build spec"
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_key] = module
        spec.loader.exec_module(module)
        register = getattr(module, "register", None)
        if not callable(register):
            # ``plugin.py`` may have imported sibling modules during
            # ``exec_module`` — use ``unload_extension`` so every
            # ``ouroboros._extensions.<skill>.*`` entry is purged, not
            # just the top-level module.
            unload_extension(skill.name)
            return (
                f"skill {skill.name!r} plugin.py does not export a "
                "register(api) callable"
            )
        api = PluginAPIImpl(
            skill_name=skill.name,
            permissions=list(skill.manifest.permissions or []),
            env_allowlist=list(skill.manifest.env_from_settings or []),
            state_dir=state_dir,
            settings_reader=settings_reader,
        )
        with _lock:
            _extension_modules[skill.name] = module
        register(api)
    except ExtensionRegistrationError as exc:
        # Tear down any partial registrations the plugin managed before
        # the error.
        unload_extension(skill.name)
        return f"skill {skill.name!r} registration error: {exc}"
    except Exception as exc:
        unload_extension(skill.name)
        log.exception("extension %s failed to load", skill.name)
        return f"skill {skill.name!r} load failure: {type(exc).__name__}: {exc}"
    return None


def unload_extension(skill_name: str) -> None:
    """Remove every registration attached by ``skill_name`` + drop the
    module (and every submodule) from ``sys.modules`` so a subsequent
    load re-imports cleanly.

    Phase 4 uses ``submodule_search_locations`` so an extension can do
    ``from .helper import X``. Those child modules live in
    ``sys.modules`` under the same ``ouroboros._extensions.<skill>.*``
    prefix — we purge every key matching the prefix, not just the
    top-level entry.
    """
    with _lock:
        bundle = _extensions.pop(skill_name, None)
        _extension_modules.pop(skill_name, None)
        if bundle:
            for key in bundle.tools:
                _tools.pop(key, None)
            for key in bundle.routes:
                _routes.pop(key, None)
            for key in bundle.ws_handlers:
                _ws_handlers.pop(key, None)
            for key in bundle.ui_tabs:
                _ui_tabs.pop(key, None)
    prefix = _module_key(skill_name)
    # Iterate over a copy so we can mutate ``sys.modules`` safely.
    for mod_name in list(sys.modules.keys()):
        if mod_name == prefix or mod_name.startswith(prefix + "."):
            sys.modules.pop(mod_name, None)


def reload_all(
    drive_root: pathlib.Path,
    settings_reader: Callable[[], Dict[str, Any]],
    *,
    repo_path: str | None = None,
) -> Dict[str, Any]:
    """Discover skills, tear down stale extensions, load any that qualify.

    Returns a ``{skill_name: error_or_None}`` map; ``None`` means the
    extension is now active.
    """
    skills = discover_skills(drive_root, repo_path=repo_path)
    skill_names = {s.name for s in skills if s.manifest.is_extension()}
    # Tear down extensions that disappeared or were disabled.
    with _lock:
        loaded_names = set(_extensions.keys())
    for gone in loaded_names - skill_names:
        unload_extension(gone)
    results: Dict[str, Any] = {}
    for skill in skills:
        if not skill.manifest.is_extension():
            continue
        unload_extension(skill.name)
        error = load_extension(skill, settings_reader, drive_root=drive_root)
        results[skill.name] = error
    return results


def snapshot() -> Dict[str, Any]:
    """Return a read-only snapshot of currently-registered surfaces.

    Used by ``/api/state`` and the Skills UI to surface what's live.
    """
    with _lock:
        return {
            "extensions": sorted(_extensions.keys()),
            "tools": sorted(_tools.keys()),
            "routes": sorted(_routes.keys()),
            "ws_handlers": sorted(_ws_handlers.keys()),
            "ui_tabs": sorted(_ui_tabs.keys()),
        }


def get_tool(name: str) -> Optional[Dict[str, Any]]:
    """Return the tool dict registered for ``name``, or None."""
    with _lock:
        return dict(_tools.get(name) or {}) or None


def list_ws_handlers() -> Dict[str, Any]:
    with _lock:
        return {k: dict(v) for k, v in _ws_handlers.items()}


def list_routes() -> Dict[str, Any]:
    with _lock:
        return {k: dict(v) for k, v in _routes.items()}


__all__ = [
    "PluginAPIImpl",
    "load_extension",
    "unload_extension",
    "reload_all",
    "snapshot",
    "get_tool",
    "list_ws_handlers",
    "list_routes",
]
