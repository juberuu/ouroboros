"""Phase 4 extension loader — PluginAPI-backed ``type: extension`` support.

Extensions are full Python modules that run IN-PROCESS inside the
Ouroboros runtime, unlike ``type: script`` skills which spawn a
sandboxed subprocess via ``skill_exec``. An extension's ``plugin.py``
exports a single ``register(api: PluginAPI)`` function that calls the
narrow PluginAPI surface to attach tools, HTTP routes, and WebSocket
handlers.

Because extensions share the Ouroboros process address space the
review gate is stricter than for ``type: script``:

- Every registration is namespaced to provider-safe ``ext_<len>_<token>_<name>``
  identifiers so a plugin
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
import hashlib
import logging
import pathlib
import re
import shutil
import sys
import threading
import uuid
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, Sequence

from ouroboros.contracts.plugin_api import (
    ExtensionRegistrationError,
    FORBIDDEN_EXTENSION_SETTINGS,
    VALID_EXTENSION_PERMISSIONS,
    VALID_EXTENSION_ROUTE_METHODS,
)
from ouroboros.skill_loader import (
    LoadedSkill,
    SkillPayloadUnreadable,
    compute_content_hash,
    discover_skills,
    find_skill,
    load_skill_grants,
    requested_core_setting_keys,
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
    unload_callbacks: List[Callable[[], Any]] = field(default_factory=list)
    api_instances: List[Any] = field(default_factory=list)
    content_hash: Optional[str] = None
    skill_dir: Optional[str] = None
    import_root: Optional[str] = None

    def is_empty(self) -> bool:
        return not (
            self.tools
            or self.routes
            or self.ws_handlers
            or self.ui_tabs
            or self.unload_callbacks
        )


@dataclass
class _ExtensionLoadFailure:
    content_hash: str
    skill_dir: str
    error: str


# Module-global, lock-guarded registries. Separate dicts make unload
# O(names_attached_by_extension) rather than O(total_registrations).
_lock = threading.RLock()
_extensions: Dict[str, _ExtensionRegistrations] = {}
_extension_modules: Dict[str, ModuleType] = {}
_load_failures: Dict[str, _ExtensionLoadFailure] = {}
_unloading: set[str] = set()
_lifecycle_locks: Dict[str, threading.RLock] = {}
_tools: Dict[str, Any] = {}            # {"ext_<len>_<token>_<name>": ToolEntry-like}
_routes: Dict[str, Any] = {}           # {"/api/extensions/<skill>/<path>": handler_spec}
_ws_handlers: Dict[str, Any] = {}      # {"ext_<len>_<token>_<message_type>": handler}
_ui_tabs: Dict[str, Any] = {}          # {"<skill>:<tab_id>": tab_spec}
_ws_broadcaster: Optional[Callable[[dict], None]] = None
_EXTENSION_NAME_PREFIX = "ext_"
_EXTENSION_SKILL_TOKEN_MAX = 32
_EXTENSION_SHORT_MAX = 24
_EXTENSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _extension_skill_token(skill_name: str) -> str:
    """Return a short ASCII token for a skill without changing its identity."""
    text = str(skill_name or "").strip()
    safe = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "-_")) else "_" for ch in text)
    safe = re.sub(r"_+", "_", safe).strip("_-")
    raw_budget = _EXTENSION_SKILL_TOKEN_MAX - 2
    if safe and safe == text and len(safe) <= raw_budget:
        return f"r_{safe}"
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:10]
    prefix_budget = _EXTENSION_SKILL_TOKEN_MAX - len(digest) - 3
    prefix = (safe or "skill")[:prefix_budget].strip("_-") or "skill"
    return f"h_{prefix}_{digest}"


def extension_name_prefix(skill_name: str) -> str:
    """Return the provider-safe prefix for one extension skill."""
    token = _extension_skill_token(skill_name)
    return f"{_EXTENSION_NAME_PREFIX}{len(token)}_{token}_"


def extension_surface_name(skill_name: str, short_name: str) -> str:
    """Return a provider-safe canonical tool/ws registration name."""
    full = f"{extension_name_prefix(skill_name)}{short_name}"
    if not _EXTENSION_NAME_RE.match(full):
        raise ExtensionRegistrationError(
            f"extension surface name {full!r} must match provider tool-name limits"
        )
    return full


def parse_extension_surface_name(name: str) -> tuple[str, str] | None:
    """Recognise provider-safe extension names.

    The first tuple element is the encoded skill token, not the persisted
    skill identity. Runtime dispatch gets the real skill from the loader's
    handler/tool descriptor.
    """
    text = str(name or "").strip()
    if not _EXTENSION_NAME_RE.match(text) or not text.startswith(_EXTENSION_NAME_PREFIX):
        return None
    rest = text[len(_EXTENSION_NAME_PREFIX):]
    length_text, sep, remainder = rest.partition("_")
    if sep != "_" or not length_text.isdigit():
        return None
    token_len = int(length_text)
    if token_len < 1 or len(remainder) <= token_len or remainder[token_len] != "_":
        return None
    token = remainder[:token_len]
    short = remainder[token_len + 1:]
    return token, short


def _lifecycle_lock_for(skill_name: str) -> threading.RLock:
    with _lock:
        lock = _lifecycle_locks.get(skill_name)
        if lock is None:
            lock = threading.RLock()
            _lifecycle_locks[skill_name] = lock
        return lock


def _run_unload_callback(skill_name: str, callback: Callable[[], Any], timeout_sec: float = 2.0) -> None:
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            callback()
        except BaseException as exc:  # pragma: no cover - surfaced via log
            errors.append(exc)

    thread = threading.Thread(target=runner, name=f"ouroboros-ext-unload-{skill_name}", daemon=True)
    thread.start()
    thread.join(timeout=timeout_sec)
    if thread.is_alive():
        log.warning("extension %s unload callback timed out after %.1fs", skill_name, timeout_sec)
        return
    if errors:
        exc = errors[0]
        log.warning("extension %s unload callback failed", skill_name, exc_info=(type(exc), exc, exc.__traceback__))


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
    if len(candidate) > _EXTENSION_SHORT_MAX:
        raise ExtensionRegistrationError(
            f"tool name must be <= {_EXTENSION_SHORT_MAX} characters: {candidate!r}"
        )
    if not candidate.replace("_", "").isalnum():
        raise ExtensionRegistrationError(
            f"tool name must be alnum/underscore only: {candidate!r}"
        )
    return candidate


_UI_RENDER_KINDS = {"", "iframe", "inline_card", "declarative"}
_DECLARATIVE_WIDGET_COMPONENTS = {
    "action",
    "audio",
    "chart",
    "code",
    "file",
    "form",
    "gallery",
    "image",
    "json",
    "kv",
    "markdown",
    "poll",
    "progress",
    "status",
    "stream",
    "subscription",
    "tabs",
    "table",
    "video",
}


def _validate_ui_render(render: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the browser-hosted widget declaration surface."""
    if not isinstance(render, dict):
        raise ExtensionRegistrationError("ui render must be an object")
    clean = dict(render)
    kind = str(clean.get("kind") or "").strip()
    if kind not in _UI_RENDER_KINDS:
        raise ExtensionRegistrationError(
            f"ui render kind {kind!r} is unsupported; "
            f"expected one of {sorted(_UI_RENDER_KINDS - {''})}"
        )
    if kind == "declarative":
        try:
            schema_version = int(clean.get("schema_version", 1))
        except (TypeError, ValueError) as exc:
            raise ExtensionRegistrationError(
                "declarative widget schema_version must be 1"
            ) from exc
        if schema_version != 1:
            raise ExtensionRegistrationError(
                "declarative widget schema_version must be 1"
            )
        components = clean.get("components")
        if not isinstance(components, list):
            raise ExtensionRegistrationError(
                "declarative widget render requires components[]"
            )
        for idx, component in enumerate(components):
            if not isinstance(component, dict):
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} must be an object"
                )
            component_type = str(component.get("type") or "").strip()
            if component_type not in _DECLARATIVE_WIDGET_COMPONENTS:
                raise ExtensionRegistrationError(
                    "declarative widget component "
                    f"{idx} has unsupported type {component_type!r}"
                )
            if (
                component_type in {"form", "action", "poll"}
                and not str(component.get("route") or component.get("api_route") or "").strip()
            ):
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} requires route or api_route"
                )
            if component_type == "subscription":
                event_name = str(component.get("event") or component.get("message_type") or "").strip()
                if not event_name:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires event or message_type"
                    )
                _assert_ws_message_type(event_name)
            if component_type == "stream" and not str(component.get("route") or component.get("api_route") or "").strip():
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} requires route or api_route"
                )
            if component_type == "tabs":
                tabs = component.get("tabs")
                if not isinstance(tabs, list) or not tabs:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires non-empty tabs[]"
                    )
                for tab_idx, tab in enumerate(tabs):
                    if not isinstance(tab, dict) or not str(tab.get("label") or "").strip():
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} tab {tab_idx} requires label"
                        )
                    tab_components = tab.get("components", [])
                    if not isinstance(tab_components, list):
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} tab {tab_idx} components must be a list"
                        )
                    for child_idx, child in enumerate(tab_components):
                        child_type = str((child or {}).get("type") or "") if isinstance(child, dict) else ""
                        if child_type in {"form", "action", "poll", "subscription", "stream", "tabs"}:
                            raise ExtensionRegistrationError(
                                f"declarative widget component {idx} tab {tab_idx} child {child_idx} "
                                f"cannot use interactive type {child_type!r}"
                            )
                    _validate_ui_render({
                        "kind": "declarative",
                        "schema_version": schema_version,
                        "components": tab_components,
                    })
            method = str(component.get("method") or "GET").upper()
            if method not in VALID_EXTENSION_ROUTE_METHODS:
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} has unsupported method {method!r}"
                )
            if component_type == "stream" and method != "GET":
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} stream method must be GET"
                )
            if component_type == "form":
                fields = component.get("fields")
                if not isinstance(fields, list) or not fields:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires non-empty fields[]"
                    )
                for field_idx, field in enumerate(component.get("fields") or []):
                    if not isinstance(field, dict) or not str(field.get("name") or "").strip():
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} field {field_idx} requires name"
                        )
            if component_type == "kv":
                fields = component.get("fields")
                if not isinstance(fields, list) or not fields:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires non-empty fields[]"
                    )
                for field_idx, field in enumerate(component.get("fields") or []):
                    if not isinstance(field, dict) or not str(field.get("path") or "").strip():
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} field {field_idx} requires path"
                        )
            if component_type == "table":
                columns = component.get("columns")
                if not isinstance(columns, list) or not columns:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires non-empty columns[]"
                    )
                for col_idx, column in enumerate(component.get("columns") or []):
                    if not isinstance(column, dict) or not str(column.get("path") or "").strip():
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} column {col_idx} requires path"
                        )
            if component_type in {"image", "audio", "video", "file"}:
                has_media_source = any(
                    str(component.get(key) or "").strip()
                    for key in ("route", "api_route", "src", "path")
                )
                if not has_media_source:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires media source"
                    )
            if component_type == "gallery" and "items" in component and not isinstance(component.get("items"), list):
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} items must be a list"
                )
            if component_type == "gallery":
                for item_idx, item in enumerate(component.get("items") or []):
                    if not isinstance(item, dict):
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} item {item_idx} must be an object"
                        )
                    item_type = str(item.get("type") or "image").strip()
                    if item_type not in {"image", "audio", "video", "file"}:
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} item {item_idx} has unsupported type {item_type!r}"
                        )
                    has_media_source = any(
                        str(item.get(key) or "").strip()
                        for key in ("route", "api_route", "src", "path")
                    )
                    if not has_media_source:
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} item {item_idx} requires media source"
                        )
    return clean


def _assert_ws_message_type(message_type: str) -> str:
    candidate = str(message_type or "").strip()
    if not candidate:
        raise ExtensionRegistrationError("ws message_type must be non-empty")
    if len(candidate) > _EXTENSION_SHORT_MAX:
        raise ExtensionRegistrationError(
            f"ws message_type must be <= {_EXTENSION_SHORT_MAX} characters: {candidate!r}"
        )
    if not candidate.replace("_", "").isalnum():
        raise ExtensionRegistrationError(
            f"ws message_type must be alnum/underscore only: {candidate!r}"
        )
    return candidate


def set_ws_broadcaster(broadcaster: Callable[[dict], None] | None) -> None:
    """Install the host WebSocket broadcaster used by PluginAPI.send_ws_message."""
    global _ws_broadcaster
    with _lock:
        _ws_broadcaster = broadcaster


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
        granted_keys: Sequence[str] | None = None,
    ) -> None:
        self._skill = skill_name
        self._permissions = frozenset(str(p).strip() for p in (permissions or []))
        self._env_allow = frozenset(str(k).strip() for k in (env_allowlist or []))
        self._env_allow_upper = frozenset(k.upper() for k in self._env_allow)
        self._state_dir = pathlib.Path(state_dir)
        self._settings_reader = settings_reader
        self._registration_closed = False
        self._runtime_closing = False
        self._runtime_closed = False
        self._api_lock = threading.RLock()
        # v5.2.2: extensions may receive forbidden / "core" settings keys
        # (e.g. ``OPENROUTER_API_KEY``) when an owner grant has been
        # captured through the desktop launcher native confirmation
        # bridge. The grant is recorded against the current content
        # hash + manifest-requested set; the loader passes the granted
        # subset into ``PluginAPIImpl`` at load time so ``get_settings``
        # can honour it without re-reading the grants file on every
        # call. Without a grant, the forbidden denylist still drops the
        # value silently — same defense-in-depth as the script flow.
        self._granted_upper = frozenset(
            str(k).strip().upper() for k in (granted_keys or []) if str(k).strip()
        )

    # --- internal helpers ---

    def _require(self, perm: str) -> None:
        with _lock:
            self._require_open_locked()
        if perm not in VALID_EXTENSION_PERMISSIONS:
            raise ExtensionRegistrationError(
                f"unknown extension permission {perm!r}"
            )
        if perm not in self._permissions:
            raise ExtensionRegistrationError(
                f"skill {self._skill!r} cannot {perm!r} "
                f"— manifest permissions={sorted(self._permissions)}"
            )

    def _require_open_locked(self) -> None:
        if self._registration_closed or self._runtime_closing or self._runtime_closed or self._skill in _unloading:
            raise ExtensionRegistrationError(
                f"skill {self._skill!r} cannot register after unload has started"
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
        full = extension_surface_name(self._skill, short)
        with _lock:
            self._require_open_locked()
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
        methods_iter = (methods,) if isinstance(methods, str) else (methods or ())
        norm_methods = tuple(
            dict.fromkeys(
                str(m).strip().upper()
                for m in methods_iter
                if str(m).strip()
            )
        )
        if not norm_methods:
            raise ExtensionRegistrationError("route methods must be non-empty")
        invalid_methods = [m for m in norm_methods if m not in VALID_EXTENSION_ROUTE_METHODS]
        if invalid_methods:
            raise ExtensionRegistrationError(
                f"route methods {invalid_methods!r} are unsupported; "
                f"expected subset of {sorted(VALID_EXTENSION_ROUTE_METHODS)}"
            )
        mount = f"/api/extensions/{self._skill}/{rel}"
        with _lock:
            self._require_open_locked()
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
        full = extension_surface_name(self._skill, short)
        with _lock:
            self._require_open_locked()
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
            self._require_open_locked()
            if key in _ui_tabs:
                raise ExtensionRegistrationError(
                    f"ui tab {key!r} already registered"
                )
            _ui_tabs[key] = {
                "skill": self._skill,
                "tab_id": clean_tab,
                "title": str(title or clean_tab),
                "icon": str(icon or "extension"),
                "ws_prefix": extension_name_prefix(self._skill),
                "render": _validate_ui_render(dict(render or {})),
                "ui_host_pending": True,
            }
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).ui_tabs.append(key)

    def send_ws_message(self, message_type: str, data: Dict[str, Any]) -> None:
        if "ws_handler" not in self._permissions:
            raise ExtensionRegistrationError(
                f"skill {self._skill!r} cannot 'ws_handler' "
                f"— manifest permissions={sorted(self._permissions)}"
            )
        short = _assert_ws_message_type(message_type)
        full = extension_surface_name(self._skill, short)
        payload = {"type": full, "data": dict(data or {}), "skill": self._skill}
        with self._api_lock:
            with _lock:
                if self._runtime_closing or self._runtime_closed or self._skill in _unloading:
                    return
            broadcaster = _ws_broadcaster
            if broadcaster is None:
                log.debug("extension %s dropped WS message %s: no broadcaster", self._skill, full)
                return
            try:
                broadcaster(payload)
            except Exception:
                log.warning("extension %s WS broadcast failed for %s", self._skill, full, exc_info=True)

    def on_unload(self, callback: Callable[[], Any]) -> None:
        if not callable(callback):
            raise ExtensionRegistrationError("on_unload callback must be callable")
        with _lock:
            if self._registration_closed or self._runtime_closing or self._runtime_closed or self._skill in _unloading:
                raise ExtensionRegistrationError(
                    f"skill {self._skill!r} cannot register unload callbacks after unload has started"
                )
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).unload_callbacks.append(callback)

    def _close_registration(self) -> None:
        with _lock:
            self._registration_closed = True

    def _close_runtime_access(self) -> None:
        with _lock:
            self._registration_closed = True
            self._runtime_closing = True
        with self._api_lock:
            with _lock:
                self._runtime_closed = True

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
        with self._api_lock:
            with _lock:
                if self._runtime_closing or self._runtime_closed or self._skill in _unloading:
                    return {}
            if "read_settings" not in self._permissions:
                # Read without the permission → empty dict (fail silent for
                # forward-compat, but never leak). Reviewer catches the
                # missing permission.
                return {}
            settings = self._settings_reader() or {}
            with _lock:
                if self._runtime_closing or self._runtime_closed or self._skill in _unloading:
                    return {}
            out: Dict[str, Any] = {}
            forbidden_upper = {k.upper() for k in FORBIDDEN_EXTENSION_SETTINGS}
            for raw_key in keys or ():
                key = str(raw_key).strip()
                canonical = key.upper()
                if not key:
                    continue
                if canonical in forbidden_upper and canonical not in self._granted_upper:
                    # Forbidden / "core" key without an owner grant — drop
                    # silently so a malicious or buggy plugin cannot probe
                    # for its presence by ``get_settings`` length.
                    continue
                if key not in self._env_allow and canonical not in self._env_allow_upper:
                    continue
                settings_key = canonical if canonical in forbidden_upper else key
                if settings_key in settings:
                    out[settings_key] = settings[settings_key]
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
    digest = hashlib.sha1(str(skill_name or "").encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"ouroboros._extensions.m_{digest}"


def _purge_extension_bytecode(skill_dir: pathlib.Path) -> None:
    """Drop cached bytecode so rapid in-place edits reload fresh source."""
    for pycache in skill_dir.rglob("__pycache__"):
        if pycache.is_dir():
            shutil.rmtree(pycache, ignore_errors=True)


def _stage_extension_import_tree(
    skill: LoadedSkill,
    *,
    state_dir: pathlib.Path,
    entry_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Copy one extension tree to a fresh import root for cache-safe reloads.

    Python's import machinery can reuse stale source/bytecode for rapid same-path
    in-place edits even after ``sys.modules`` and ``__pycache__`` are purged.
    Loading from a fresh staged directory gives each reload a unique import path,
    so both the entry module and any relative imports resolve from fresh source.
    """
    resolved_root = skill.skill_dir.resolve()
    relative_entry = entry_path.relative_to(resolved_root)
    for path in sorted(skill.skill_dir.rglob("*")):
        if not path.is_symlink():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(resolved_root)
        except Exception as exc:
            raise RuntimeError(
                f"extension {skill.name!r} contains a symlink that resolves outside the skill tree: {path}"
            ) from exc
    import_root = state_dir / "__extension_imports" / uuid.uuid4().hex
    staged_skill_dir = import_root / "skill"
    import_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill.skill_dir, staged_skill_dir)
    _purge_extension_bytecode(staged_skill_dir)
    staged_entry = (staged_skill_dir / relative_entry).resolve()
    staged_entry.relative_to(staged_skill_dir.resolve())
    return import_root, staged_entry


def _extension_runtime_state(
    skill: LoadedSkill,
    *,
    current_hash: str | None = None,
) -> Dict[str, Any]:
    """Return the single source of truth for whether an extension may be live."""
    from ouroboros.config import get_runtime_mode

    hash_now = current_hash or skill.content_hash
    skill_dir_now = str(skill.skill_dir.resolve())
    review_stale = skill.review.is_stale_for(hash_now)
    with _lock:
        live_bundle = _extensions.get(skill.name)
        live_loaded = bool(
            live_bundle
            and live_bundle.content_hash == hash_now
            and live_bundle.skill_dir == skill_dir_now
        )
        loaded_present = live_bundle is not None
        load_failure = _load_failures.get(skill.name)
        matched_failure = bool(
            load_failure
            and load_failure.content_hash == hash_now
            and load_failure.skill_dir == skill_dir_now
        )

    reason = "ready"
    desired_live = True
    if not skill.manifest.is_extension():
        desired_live = False
        reason = "not_extension"
    elif skill.load_error:
        desired_live = False
        reason = "load_error"
    elif not skill.enabled:
        desired_live = False
        reason = "disabled"
    elif skill.review.status != "pass":
        desired_live = False
        reason = f"review_{skill.review.status or 'pending'}"
    elif review_stale:
        desired_live = False
        reason = "review_stale"
    # v5.1.2 Frame A: light no longer blocks extensions. Skills (script
    # AND extension) are owner-approved capabilities — light gates only
    # repo self-modification and the runtime_mode escalation ratchet.
    elif matched_failure:
        reason = "load_error"

    return {
        "skill": skill.name,
        "type": skill.manifest.type,
        "runtime_mode": get_runtime_mode(),
        "enabled": skill.enabled,
        "review_status": skill.review.status,
        "review_stale": review_stale,
        "load_error": skill.load_error or (load_failure.error if matched_failure and load_failure else None),
        "desired_live": desired_live,
        "live_loaded": live_loaded,
        "loaded_present": loaded_present,
        "loaded_matches_current": live_loaded,
        "reason": reason,
    }


def runtime_state_for_skill_name(
    skill_name: str,
    drive_root: pathlib.Path,
    *,
    repo_path: str | None = None,
) -> Dict[str, Any]:
    from ouroboros.config import get_skills_repo_path

    resolved_repo_path = get_skills_repo_path() if repo_path is None else repo_path
    skill = find_skill(drive_root, skill_name, repo_path=resolved_repo_path)
    if skill is None:
        with _lock:
            live_loaded = skill_name in _extensions
        return {
            "skill": skill_name,
            "type": "extension",
            "runtime_mode": "",
            "enabled": False,
            "review_status": "missing",
            "review_stale": True,
            "load_error": "skill not found",
            "desired_live": False,
            "live_loaded": live_loaded,
            "loaded_present": live_loaded,
            "loaded_matches_current": False,
            "reason": "missing",
        }
    return _extension_runtime_state(skill)


def is_extension_live(
    skill_name: str,
    drive_root: pathlib.Path,
    *,
    repo_path: str | None = None,
) -> bool:
    state = runtime_state_for_skill_name(skill_name, drive_root, repo_path=repo_path)
    return bool(state.get("desired_live")) and bool(state.get("live_loaded"))


def reconcile_extension(
    skill_name: str,
    drive_root: pathlib.Path,
    settings_reader: Callable[[], Dict[str, Any]],
    *,
    repo_path: str | None = None,
    retry_load_error: bool = False,
) -> Dict[str, Any]:
    """Unload/load one extension so every surface sees the same live state."""
    lifecycle_lock = _lifecycle_lock_for(skill_name)
    with lifecycle_lock:
        state = runtime_state_for_skill_name(skill_name, drive_root, repo_path=repo_path)
        loaded_present = bool(state.get("loaded_present"))
        was_live = bool(state.get("live_loaded"))
        if retry_load_error and state.get("reason") == "load_error" and not was_live:
            with _lock:
                _load_failures.pop(skill_name, None)
            state = runtime_state_for_skill_name(skill_name, drive_root, repo_path=repo_path)
            loaded_present = bool(state.get("loaded_present"))
            was_live = bool(state.get("live_loaded"))
        elif state.get("reason") == "load_error" and not loaded_present:
            state["action"] = "extension_load_error"
            return state
        if state.get("reason") == "missing" or state.get("reason") == "not_extension":
            if loaded_present:
                unload_extension(skill_name)
            state["action"] = "extension_unloaded" if loaded_present else "extension_inactive"
            state["live_loaded"] = False
            state["loaded_present"] = False
            return state

        if not state.get("desired_live"):
            if loaded_present:
                unload_extension(skill_name)
            state["action"] = "extension_unloaded" if loaded_present else "extension_inactive"
            state["live_loaded"] = False
            state["loaded_present"] = False
            return state

        if was_live:
            state["action"] = "extension_already_live"
            return state

        from ouroboros.config import get_skills_repo_path

        resolved_repo_path = get_skills_repo_path() if repo_path is None else repo_path
        loaded = find_skill(drive_root, skill_name, repo_path=resolved_repo_path)
        if loaded is None:
            state["reason"] = "missing"
            state["action"] = "extension_inactive"
            return state
        if loaded_present:
            unload_extension(skill_name)
        err = load_extension(loaded, settings_reader, drive_root=drive_root)
        if err:
            with _lock:
                _load_failures[skill_name] = _ExtensionLoadFailure(
                    content_hash=loaded.content_hash,
                    skill_dir=str(loaded.skill_dir.resolve()),
                    error=err,
                )
            state["reason"] = "load_error"
            state["load_error"] = err
            state["action"] = "extension_load_error"
            return state
        refreshed = runtime_state_for_skill_name(skill_name, drive_root, repo_path=resolved_repo_path)
        refreshed["action"] = "extension_loaded"
        return refreshed


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
    runtime_state = _extension_runtime_state(skill, current_hash=current_hash)
    # v5.1.2 Frame A: the previous ``runtime_mode_light`` short-circuit
    # is removed — light no longer blocks extensions. Stale reviews and
    # other gates remain.
    if runtime_state["reason"] in {"review_stale"} or skill.review.status != "pass" or skill.review.content_hash != current_hash:
        return (
            f"skill {skill.name!r} must carry a fresh PASS review "
            f"(status={skill.review.status!r}, "
            f"stale={skill.review.content_hash != current_hash})"
        )
    if runtime_state["reason"] == "disabled":
        return f"skill {skill.name!r} is disabled"
    entry_path = _plugin_entry_path(skill)
    if entry_path is None:
        return (
            f"skill {skill.name!r} manifest.entry does not resolve to a "
            "file inside the skill directory"
        )

    if drive_root is None:
        drive_root = pathlib.Path.home() / "Ouroboros" / "data"
    state_dir = skill_state_dir(drive_root, skill.name)

    # v5.2.2 dual-track grants: extensions may declare core / forbidden
    # settings keys in their manifest, but the loader only forwards the
    # subset the owner has explicitly granted through the desktop
    # launcher's native confirmation bridge. The grant is bound to the
    # current content hash + the exact requested set so a tampered
    # plugin or rotated manifest invalidates the grant automatically.
    requested_core = requested_core_setting_keys(list(skill.manifest.env_from_settings or []))
    granted_core: List[str] = []
    if requested_core:
        grants_file = load_skill_grants(drive_root, skill.name)
        grant_hash_ok = str(grants_file.get("content_hash") or "") == str(current_hash or "")
        grant_request_ok = sorted(grants_file.get("requested_keys") or []) == sorted(requested_core)
        persisted = (
            set(grants_file.get("granted_keys") or [])
            if grant_hash_ok and grant_request_ok
            else set()
        )
        granted_core = [key for key in requested_core if key in persisted]
        missing_grants = [key for key in requested_core if key not in set(granted_core)]
        if missing_grants:
            return (
                f"skill {skill.name!r} requests core settings keys "
                f"{requested_core}; missing owner grants for "
                f"{missing_grants}. Grant access from the Skills tab."
            )
    staged_import_root: Optional[pathlib.Path] = None

    module_key = _module_key(skill.name)
    try:
        importlib.invalidate_caches()
        staged_import_root, entry_path = _stage_extension_import_tree(
            skill,
            state_dir=state_dir,
            entry_path=entry_path,
        )
        # Build a package-style spec so a multi-file extension can use
        # intra-package imports (``from .helper import X``) without
        # manual ``sys.path`` wiring. The package root must be the
        # staged entry file's parent so nested ``entry: pkg/plugin.py`` layouts
        # resolve siblings from ``pkg/`` rather than the skill root.
        spec = importlib.util.spec_from_file_location(
            module_key,
            entry_path,
            submodule_search_locations=[str(entry_path.parent)],
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
            granted_keys=granted_core,
        )
        with _lock:
            bundle = _extensions.get(skill.name)
            if bundle is None:
                bundle = _ExtensionRegistrations()
                _extensions[skill.name] = bundle
            bundle.content_hash = current_hash
            bundle.skill_dir = str(skill.skill_dir.resolve())
            bundle.import_root = str(staged_import_root) if staged_import_root is not None else None
            bundle.api_instances.append(api)
            _extension_modules[skill.name] = module
            _load_failures.pop(skill.name, None)
        register(api)
        api._close_registration()
    except ExtensionRegistrationError as exc:
        # Tear down any partial registrations the plugin managed before
        # the error.
        unload_extension(skill.name)
        return f"skill {skill.name!r} registration error: {exc}"
    except Exception as exc:
        unload_extension(skill.name)
        log.exception("extension %s failed to load", skill.name)
        return f"skill {skill.name!r} load failure: {type(exc).__name__}: {exc}"
    finally:
        if staged_import_root is not None and skill.name not in _extensions:
            shutil.rmtree(staged_import_root, ignore_errors=True)
    return None


def unload_extension(skill_name: str) -> None:
    lifecycle_lock = _lifecycle_lock_for(skill_name)
    with lifecycle_lock:
        _unload_extension_locked(skill_name)


def _unload_extension_locked(skill_name: str) -> None:
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
        import_root = pathlib.Path(bundle.import_root) if bundle and bundle.import_root else None
        callbacks = list(bundle.unload_callbacks) if bundle else []
        api_instances = list(bundle.api_instances) if bundle else []
        if bundle:
            _unloading.add(skill_name)
        if bundle:
            for key in bundle.tools:
                _tools.pop(key, None)
            for key in bundle.routes:
                _routes.pop(key, None)
            for key in bundle.ws_handlers:
                _ws_handlers.pop(key, None)
            for key in bundle.ui_tabs:
                _ui_tabs.pop(key, None)
    for api in api_instances:
        close = getattr(api, "_close_runtime_access", None)
        if callable(close):
            close()
    try:
        for callback in callbacks:
            _run_unload_callback(skill_name, callback)
        prefix = _module_key(skill_name)
        # Iterate over a copy so we can mutate ``sys.modules`` safely.
        for mod_name in list(sys.modules.keys()):
            if mod_name == prefix or mod_name.startswith(prefix + "."):
                sys.modules.pop(mod_name, None)
        if import_root is not None:
            shutil.rmtree(import_root, ignore_errors=True)
    finally:
        with _lock:
            _unloading.discard(skill_name)


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
        state = reconcile_extension(
            skill.name,
            drive_root,
            settings_reader,
            repo_path=repo_path,
            retry_load_error=True,
        )
        results[skill.name] = state.get("load_error") or (None if state.get("desired_live") else state.get("reason"))
    return results


def snapshot() -> Dict[str, Any]:
    """Return a read-only snapshot of currently-registered surfaces.

    Used by ``/api/state`` and the Skills UI to surface what's live.
    UI tabs are hostable by the Widgets page once the extension is live.
    """
    with _lock:
        return {
            "extensions": sorted(_extensions.keys()),
            "tools": sorted(_tools.keys()),
            "routes": sorted(_routes.keys()),
            "ws_handlers": sorted(_ws_handlers.keys()),
            "ui_tabs": [dict(value, key=key) for key, value in sorted(_ui_tabs.items())],
            "ui_tabs_pending": [],
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
    "is_extension_live",
    "load_extension",
    "reconcile_extension",
    "unload_extension",
    "reload_all",
    "runtime_state_for_skill_name",
    "snapshot",
    "get_tool",
    "list_ws_handlers",
    "list_routes",
]
