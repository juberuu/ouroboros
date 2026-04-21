"""Helpers shared by server startup, onboarding, and WebSocket liveness."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable

from ouroboros.provider_models import (
    ANTHROPIC_DIRECT_DEFAULTS,
    OPENAI_DIRECT_DEFAULTS,
    migrate_model_value,
)
from ouroboros.config import SETTINGS_DEFAULTS


_DIRECT_PROVIDER_AUTO_DEFAULTS = {
    "openai": {
        "OUROBOROS_MODEL": OPENAI_DIRECT_DEFAULTS["main"],
        "OUROBOROS_MODEL_CODE": OPENAI_DIRECT_DEFAULTS["code"],
        "OUROBOROS_MODEL_LIGHT": OPENAI_DIRECT_DEFAULTS["light"],
        "OUROBOROS_MODEL_FALLBACK": OPENAI_DIRECT_DEFAULTS["fallback"],
    },
    "anthropic": {
        "OUROBOROS_MODEL": ANTHROPIC_DIRECT_DEFAULTS["main"],
        "OUROBOROS_MODEL_CODE": ANTHROPIC_DIRECT_DEFAULTS["code"],
        "OUROBOROS_MODEL_LIGHT": ANTHROPIC_DIRECT_DEFAULTS["light"],
        "OUROBOROS_MODEL_FALLBACK": ANTHROPIC_DIRECT_DEFAULTS["fallback"],
    },
}
_DIRECT_PROVIDER_LEGACY_DEFAULTS = {
    "openai": {
        "OUROBOROS_MODEL_LIGHT": {"openai::gpt-4.1"},
        "OUROBOROS_MODEL_FALLBACK": {"openai::gpt-4.1"},
    },
    "anthropic": {},
}
_ALL_MODEL_SLOT_KEYS = tuple(_DIRECT_PROVIDER_AUTO_DEFAULTS["openai"].keys())
_DIRECT_PROVIDER_REVIEW_RUNS = 3


def _truthy_setting(value) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "on"}


def _setting_text(settings: dict, key: str) -> str:
    return str(settings.get(key, "") or "").strip()


def _parse_model_list(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _serialize_model_list(models: list[str]) -> str:
    return ",".join(model.strip() for model in models if str(model or "").strip())


def _provider_prefix(provider: str) -> str:
    return f"{provider}::"


def _exclusive_direct_remote_provider(settings: dict) -> str:
    has_openrouter = bool(_setting_text(settings, "OPENROUTER_API_KEY"))
    has_official_openai = bool(_setting_text(settings, "OPENAI_API_KEY"))
    has_anthropic = bool(_setting_text(settings, "ANTHROPIC_API_KEY"))
    has_legacy_openai_base = bool(_setting_text(settings, "OPENAI_BASE_URL"))
    has_compatible = bool(_setting_text(settings, "OPENAI_COMPATIBLE_API_KEY"))
    has_cloudru = bool(_setting_text(settings, "CLOUDRU_FOUNDATION_MODELS_API_KEY"))
    if has_openrouter or has_legacy_openai_base or has_compatible or has_cloudru:
        return ""
    if has_official_openai and not has_anthropic:
        return "openai"
    if has_anthropic and not has_official_openai:
        return "anthropic"
    return ""


def _normalize_direct_review_models(settings: dict, provider: str) -> str:
    main_model = migrate_model_value(provider, _setting_text(settings, "OUROBOROS_MODEL"))
    current_models = _parse_model_list(_setting_text(settings, "OUROBOROS_REVIEW_MODELS"))
    migrated_models = [migrate_model_value(provider, model) for model in current_models]
    provider_prefix = _provider_prefix(provider)

    if not main_model.startswith(provider_prefix):
        return _serialize_model_list(migrated_models)

    has_foreign_models = any(not model.startswith(provider_prefix) for model in migrated_models)
    if not migrated_models or len(migrated_models) < 2 or has_foreign_models:
        # v4.39.0: seed a quorum-safe direct-provider fallback that still
        # fills all three commit-triad slots: `[main, light, light]`
        # (3 entries, 2 unique models).
        #
        # - Commit triad in `_run_unified_review` sees 3 reviewers — unchanged
        #   from the pre-v4.39.0 contract documented in DEVELOPMENT.md and
        #   ARCHITECTURE.md (`three models review the staged diff`).
        # - plan_task in `_run_plan_review_async` dedupes to 2 unique reviewers
        #   and passes the v4.39.0 quorum gate.
        # - The duplicated `light` slot is a minor redundancy in the commit
        #   triad's third vote — majority-vote already tolerates it, and the
        #   old fallback `[main] * 3` had even more duplication.
        #
        # `light_slot` is derived from the user's actual
        # `OUROBOROS_MODEL_LIGHT` first (so a custom lane like
        # `openai::o4-mini` is honoured); only when that setting is empty
        # or points at a foreign-provider model do we fall back to the
        # shipped `_DIRECT_PROVIDER_AUTO_DEFAULTS` light for this provider.
        # If the resolved light still collapses to the same model as main
        # (user explicitly overrode both lanes identically), degrade to the
        # legacy `[main] * _DIRECT_PROVIDER_REVIEW_RUNS` shape — commit
        # triad still works, `plan_task` emits its quorum-error hint.
        user_light_raw = _setting_text(settings, "OUROBOROS_MODEL_LIGHT")
        user_light = migrate_model_value(provider, user_light_raw) if user_light_raw else ""
        provider_defaults = _DIRECT_PROVIDER_AUTO_DEFAULTS.get(provider, {})
        default_light = migrate_model_value(
            provider, provider_defaults.get("OUROBOROS_MODEL_LIGHT", "")
        )
        if user_light and user_light.startswith(provider_prefix):
            light_slot = user_light
        else:
            light_slot = default_light
        if light_slot and light_slot != main_model:
            fallback = [main_model, light_slot, light_slot]
        else:
            fallback = [main_model] * _DIRECT_PROVIDER_REVIEW_RUNS
        return _serialize_model_list(fallback)
    return _serialize_model_list(migrated_models)


def classify_runtime_provider_change(before: dict, after: dict) -> str:
    """Classify what kind of normalization ``apply_runtime_provider_defaults`` did.

    Returns one of:

    - ``"none"`` — no change, or change was purely cosmetic.
    - ``"direct_normalize"`` — OpenRouter is NOT configured, and the function
      auto-filled direct-provider defaults.  This is the only case where a
      user-facing warning is appropriate.
    - ``"reverse_migrate"`` — OpenRouter IS configured (so no exclusive-direct
      provider is active).  ``apply_runtime_provider_defaults`` returned early
      without making any changes, so this is pure housekeeping and should NOT
      produce a warning.
    """
    provider_after = _exclusive_direct_remote_provider(after)
    if provider_after:
        return "direct_normalize"
    has_openrouter_after = bool(_setting_text(after, "OPENROUTER_API_KEY"))
    if has_openrouter_after:
        return "reverse_migrate"
    return "none"


def has_remote_provider(settings: dict) -> bool:
    """Return True when any supported remote-provider credential is configured."""
    return any(
        str(settings.get(key, "") or "").strip()
        for key in (
            "OPENROUTER_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_COMPATIBLE_API_KEY",
            "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        )
    )


def has_local_model_source(settings: dict) -> bool:
    """Return True when a local model source has been configured."""
    return bool(str(settings.get("LOCAL_MODEL_SOURCE", "") or "").strip())


def has_local_routing(settings: dict) -> bool:
    """Return True when any model slot is configured to use the local server."""
    return any(
        _truthy_setting(settings.get(k))
        for k in ("USE_LOCAL_MAIN", "USE_LOCAL_CODE", "USE_LOCAL_LIGHT", "USE_LOCAL_FALLBACK")
    )


def has_startup_ready_provider(settings: dict) -> bool:
    """Return True when startup/onboarding should consider runtime configured."""
    # Startup should only skip onboarding when the runtime can actually serve
    # chat after boot. A local model source alone is not enough unless at least
    # one lane is routed to that local runtime.
    return has_remote_provider(settings) or has_local_routing(settings)


def has_supervisor_provider(settings: dict) -> bool:
    """Return True when the runtime has enough provider config to start supervisor."""
    return has_remote_provider(settings) or has_local_routing(settings)


def apply_runtime_provider_defaults(settings: dict) -> tuple[dict, bool, list[str]]:
    """Auto-fill safe runtime defaults for the agreed provider cases."""
    normalized = dict(settings)
    provider = _exclusive_direct_remote_provider(normalized)

    if not provider:
        return normalized, False, []

    changed_keys: list[str] = []
    provider_defaults = _DIRECT_PROVIDER_AUTO_DEFAULTS[provider]
    for key in _ALL_MODEL_SLOT_KEYS:
        raw_current = _setting_text(normalized, key)
        current = migrate_model_value(provider, raw_current)
        default = _setting_text(SETTINGS_DEFAULTS, key)
        auto_value = provider_defaults[key]
        legacy_defaults = _DIRECT_PROVIDER_LEGACY_DEFAULTS.get(provider, {}).get(key, set())
        next_value = auto_value if current in {"", default, *legacy_defaults} else current
        if next_value != raw_current:
            normalized[key] = next_value
            changed_keys.append(key)

    review_models = _normalize_direct_review_models(normalized, provider)
    if review_models != _setting_text(normalized, "OUROBOROS_REVIEW_MODELS"):
        normalized["OUROBOROS_REVIEW_MODELS"] = review_models
        changed_keys.append("OUROBOROS_REVIEW_MODELS")

    return normalized, bool(changed_keys), changed_keys


def setup_remote_if_configured(settings: dict, log) -> None:
    """Set up GitHub remote and migrate credentials if configured."""
    slug = settings.get("GITHUB_REPO", "")
    token = settings.get("GITHUB_TOKEN", "")
    if not slug or not token:
        return
    from supervisor.git_ops import configure_remote, migrate_remote_credentials

    remote_ok, remote_msg = configure_remote(slug, token)
    if not remote_ok:
        log.warning("Remote configuration failed on startup: %s", remote_msg)
        return
    mig_ok, mig_msg = migrate_remote_credentials()
    if not mig_ok:
        log.warning("Credential migration failed on startup: %s", mig_msg)


async def ws_heartbeat_loop(
    has_clients_fn: Callable[[], bool],
    broadcast_fn: Callable[[dict], Awaitable[None]],
    interval_sec: float = 15.0,
) -> None:
    """Keep embedded clients active and give watchdogs a steady liveness signal."""
    while True:
        await asyncio.sleep(interval_sec)
        if not has_clients_fn():
            continue
        await broadcast_fn({
            "type": "heartbeat",
            "ts": datetime.now(timezone.utc).isoformat(),
        })
