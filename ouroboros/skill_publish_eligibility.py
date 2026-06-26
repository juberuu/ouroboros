"""SSOT predicate for skill→hub publish eligibility (FR1).

One rule, consumed by the backend publish gate (``tools/skill_publish.py``), the
gateway serializer (``gateway/extensions.py`` emits a ``submit_hub`` object), and —
through that serialized object — the frontend Skills card. This ends the desync
where the backend accepted advisory-only ``warnings`` but the card still disabled
Submit unless the status was exactly ``clean``.

Imports ONLY the config-level review-status constants — no skill loading, no gateway
code — so it stays a thin, dependency-light predicate.
"""

from __future__ import annotations

from typing import Any, Dict

from ouroboros.skill_review_status import STATUS_CLEAN, STATUS_WARNINGS, normalize_skill_review_status

# Sources whose payload may be submitted to the hub (native without a marker is
# handled by skill_loader reclassification, not here).
PUBLISHABLE_SOURCES = ("external", "self_authored", "user_repo", "ouroboroshub", "clawhub")
# A no-blocker review — clean OR advisory-only warnings (warnings are disclosed in the
# PR body). This is the SSOT both the backend gate and the UI predicate use.
PUBLISHABLE_STATUSES = frozenset({STATUS_CLEAN, STATUS_WARNINGS})


def submit_hub_eligibility(
    *,
    source: str,
    review_status: str,
    review_profile: str = "",
    review_stale: bool = False,
    github_token_configured: bool = False,
) -> Dict[str, Any]:
    """Return ``{visible, disabled, reason}`` for the Submit-to-Hub affordance.

    ``visible`` is whether the action belongs on the card at all (publishable
    source); ``disabled`` + ``reason`` explain why a visible action can't proceed
    yet. A skill with an advisory-only ``warnings`` review IS publishable — matching
    the backend gate — so the card no longer falsely requires exactly ``clean``."""
    src = str(source or "native").lower()
    # Normalize the verdict the SAME way the backend publish gate does, so a raw verdict
    # (e.g. 'pass'/'advisory_pass') and the normalized form ('clean'/'warnings') agree.
    review_status = normalize_skill_review_status(review_status)
    if src not in PUBLISHABLE_SOURCES:
        return {"visible": False, "disabled": True, "reason": ""}
    if not github_token_configured:
        return {"visible": True, "disabled": True, "reason": "Configure GITHUB_TOKEN in Settings → Secrets"}
    if str(review_profile or "") == "owner_attested":
        # Owner-attested skills SKIPPED the LLM review; a public submission needs the
        # full tri-model review, so the hub refuses them.
        return {"visible": True, "disabled": True, "reason": "Owner-attested skills can't be published — run a full LLM review first"}
    if review_stale:
        return {"visible": True, "disabled": True, "reason": "Skill needs a fresh review before submission"}
    if str(review_status or "") not in PUBLISHABLE_STATUSES:
        return {"visible": True, "disabled": True, "reason": "Skill needs a clean (or advisory-only warnings) review before submission"}
    return {"visible": True, "disabled": False, "reason": "Open a PR to OuroborosHub from your GitHub fork"}
