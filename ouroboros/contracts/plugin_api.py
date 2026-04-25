"""Ouroboros — Frozen PluginAPI contract (v1, Phase 4).

Every ``type: extension`` skill's ``plugin.py`` module exports a single
entry point::

    def register(api: PluginAPI) -> None:
        api.register_tool(...)
        api.register_route(...)
        api.register_ws_handler(...)

``PluginAPI`` is the ONLY surface an extension may call. The ABI
declared here is frozen between releases in the same sense as
``ouroboros/contracts/tool_abi.py`` — breaking any method signature or
tightening a permission allowlist requires a deliberate bump in
``SKILL_MANIFEST_SCHEMA_VERSION`` and a release note in
``docs/ARCHITECTURE.md`` §12.

The surface intentionally mirrors what the Phase 3 plan approved:

- ``register_tool``      — add a tool callable via the normal tool
                           dispatch surface, namespaced as
                           ``ext.<skill>.<name>``.
- ``register_route``     — register an HTTP handler mounted under
                           ``/api/extensions/<skill>/<path>``.
- ``register_ws_handler``— attach a handler for WS message types
                           prefixed ``ext.<skill>.``.
- ``register_ui_tab``    — declare a future Settings/Skills UI tab.
- ``log``                — structured logger (the extension does not
                           touch ``logging``/``print`` directly).
- ``get_settings``       — read-only view of settings keys the skill's
                           manifest ``env_from_settings`` allowlist
                           permits AND the ``_FORBIDDEN_EXT_SETTINGS``
                           runtime denylist does not block.

All registrations are declarative — an extension that is later disabled
via ``toggle_skill`` is reloaded with all of its registrations torn
down, so the extension layer has no persistent side effects beyond the
skill's own state directory.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Protocol, Sequence, runtime_checkable


# Skills may NOT receive these settings keys even if their manifest
# explicitly requests them (shared by both ``skill_exec`` env forwarding
# and ``PluginAPI.get_settings`` — one allow/deny model for script and
# extension skills).
FORBIDDEN_SKILL_SETTINGS: frozenset[str] = frozenset(
    {
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "GITHUB_TOKEN",
        "OUROBOROS_NETWORK_PASSWORD",
    }
)
# Backwards-compatible alias for the frozen Phase 4 name.
FORBIDDEN_EXTENSION_SETTINGS: frozenset[str] = FORBIDDEN_SKILL_SETTINGS


# Permission names an extension may declare in its manifest. The values
# also live in ``ouroboros.contracts.skill_manifest.VALID_SKILL_PERMISSIONS``
# from Phase 1, kept in sync here for a frozen ABI surface.
VALID_EXTENSION_PERMISSIONS: frozenset[str] = frozenset(
    {
        "net",
        "fs",
        "subprocess",
        "widget",
        "ws_handler",
        "route",
        "tool",
        "read_settings",
    }
)

VALID_EXTENSION_ROUTE_METHODS: frozenset[str] = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"}
)


@runtime_checkable
class PluginAPI(Protocol):
    """Frozen ABI exposed to every extension's ``register(api)``.

    This Protocol is RUNTIME-CHECKABLE so smoke tests can assert the
    real ``ouroboros.extension_loader.PluginAPIImpl`` structurally
    matches the frozen surface.
    """

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
        """Register a tool. The runtime namespaces it to
        ``ext.<skill>.<name>``; attempting to register a collision with
        a built-in tool name or another extension's tool raises
        ``ExtensionRegistrationError``."""
        ...

    def register_route(
        self,
        path: str,
        handler: Callable[..., Any],
        *,
        methods: Sequence[str] = ("GET",),
    ) -> None:
        """Register an HTTP route. The final mount point is
        ``/api/extensions/<skill>/<path>``; ``path`` must not start
        with ``/`` and must not contain ``..`` segments. ``methods``
        must be a non-empty subset of ``VALID_EXTENSION_ROUTE_METHODS``."""
        ...

    def register_ws_handler(
        self,
        message_type: str,
        handler: Callable[..., Awaitable[Any]] | Callable[..., Any],
    ) -> None:
        """Register a WebSocket message handler. ``message_type`` is
        stored as ``ext.<skill>.<message_type>`` on the dispatcher;
        handlers receive ``(payload_dict)`` and may be async."""
        ...

    def register_ui_tab(
        self,
        tab_id: str,
        title: str,
        *,
        icon: str = "extension",
        render: Dict[str, Any] | None = None,
    ) -> None:
        """Register a Skills-UI tab declaration.

        The runtime stores the declaration in
        ``ouroboros.extension_loader._ui_tabs`` keyed by
        ``"<skill>:<tab_id>"``. As of v5.0.0 the Skills page
        (``web/modules/skills.js``) renders inline widgets for
        ``type: extension`` skills via a *name-keyed dispatch table*
        (``registerWidgetRenderer``) — the widget code is shipped
        alongside the launcher, NOT loaded from the extension's own
        package. This means:

        - Bundled (first-party) extensions like ``weather`` get a
          visual widget rendered inline on their Installed-tab card
          via the in-tree ``_renderWeatherWidget`` renderer.
        - Third-party extensions that call ``register_ui_tab`` are
          accepted at registration time but their widget surface
          will display "no widget renderer registered for <name>"
          until a future Ouroboros release ships a generic
          declarative renderer (consuming ``render.kind``,
          ``render.api_route``, ``render.schema_version``).

        v5 deliberately keeps the widget code in-tree so every visible
        byte stays under the same review gate as the rest of the SPA.
        A future release may relax this with a per-skill ``widget.js``
        review track."""
        ...

    # --- runtime access ---

    def log(
        self,
        level: str,
        message: str,
        **fields: Any,
    ) -> None:
        """Structured log. ``level`` one of ``debug``/``info``/``warning``/``error``."""
        ...

    def get_settings(self, keys: Sequence[str]) -> Dict[str, Any]:
        """Return a ``{key: value}`` mapping for the requested keys.

        Requires the manifest ``read_settings`` permission. Only keys
        that are simultaneously in the skill manifest's
        ``env_from_settings`` allowlist AND NOT in
        ``FORBIDDEN_EXTENSION_SETTINGS`` are returned; forbidden or
        unallowed keys are silently dropped. Missing keys omit from the
        result.
        """
        ...

    def get_state_dir(self) -> str:
        """Absolute path of the skill's private state directory
        (``~/Ouroboros/data/state/skills/<skill>/``).

        This is the **canonical** writable location for an extension's
        durable state. Extensions run IN-PROCESS and are not filesystem-
        sandboxed (Phase 4 does not wrap the interpreter in an OS-level
        jail), so a misbehaving plugin could technically ``open(...)``
        paths elsewhere. The Skill Review Checklist's
        ``path_confinement`` item is the authoritative enforcement;
        ``get_state_dir`` is where well-behaved extensions should put
        their durable state so operators can find it in the expected
        place and ``toggle_skill`` / clean-uninstall paths know where
        to look."""
        ...


class ExtensionRegistrationError(Exception):
    """Raised by the extension loader when a registration call violates
    the namespace / permission / schema contract. Surfaces to the
    agent as a ``load_error`` on the owning skill so the operator can
    fix the plugin and re-review."""


__all__ = [
    "PluginAPI",
    "ExtensionRegistrationError",
    "FORBIDDEN_SKILL_SETTINGS",
    "FORBIDDEN_EXTENSION_SETTINGS",
    "VALID_EXTENSION_PERMISSIONS",
    "VALID_EXTENSION_ROUTE_METHODS",
]
