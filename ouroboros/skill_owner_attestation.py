"""C1 (v6.39): skill owner-attestation.

The OWNER may explicitly skip the EXPENSIVE LLM skill-review for their OWN (external /
self-authored) skill, while the DETERMINISTIC preflight floor still gates it. Extracted
from skill_review.py to keep that module under the size gate; it reuses skill_review's
preflight + persistence machinery (referenced module-qualified so tests can monkeypatch).
"""

from __future__ import annotations

import pathlib
from typing import Any

from ouroboros import skill_review as _sr
from ouroboros.config import (
    SKILL_SOURCE_CLAWHUB,
    SKILL_SOURCE_EXTERNAL,
    SKILL_SOURCE_NATIVE,
    SKILL_SOURCE_OUROBOROSHUB,
)
from ouroboros.skill_loader import (
    SkillPayloadUnreadable,
    SkillReviewState,
    compute_content_hash,
    find_skill,
    save_review_state,
    skill_state_dir,
)
from ouroboros.utils import atomic_write_json, utc_now_iso


def run_owner_attestation(ctx: Any, drive_root: pathlib.Path, skill: Any, content_hash: str):
    """Owner-attested skill review — skip the EXPENSIVE LLM review for the owner's OWN skill.
    The DETERMINISTIC preflight floor STILL runs (an invalid manifest / sensitive-shaped /
    path-escaping payload can NEVER be attested); only the LLM phase is skipped. On a clean
    preflight, persist a durable CLEAN verdict bound to the content_hash
    (review_profile='owner_attested', reviewer_models=['owner_attestation'], one explicit
    PASS finding so the status serializes) and drop the owner-issued marker that
    load_review_state requires for the verdict to stay valid. Owner-only (the endpoint gates
    it); the agent can never forge the marker (it is an owner-state file)."""
    # persist=False: a FAILED attestation attempt must not clobber the skill's existing
    # review state — the endpoint surfaces the preflight failure (409) and leaves state as-is.
    preflight_outcome = _sr._run_deterministic_preflight(
        ctx, drive_root, skill, content_hash, persist=False
    )
    if preflight_outcome is not None:
        # Deterministic preflight FAILED -> NOT attestable (the LLM phase is skipped, this
        # floor is not). The PENDING outcome carries the skill_preflight FAIL finding.
        return preflight_outcome
    # The deterministic floor must also cover what the SKIPPED LLM manifest reviewer would
    # have caught: a parsed-but-structurally-invalid manifest surfaces only as
    # SkillManifest.validate() warnings. Owner-attestation refuses on ANY such warning.
    manifest = getattr(skill, "manifest", None)
    validate_warnings = (
        list(manifest.validate()) if manifest is not None and hasattr(manifest, "validate") else []
    )
    if validate_warnings:
        return _sr.SkillReviewOutcome(
            skill_name=skill.name, status=_sr.STATUS_PENDING, content_hash=content_hash,
            error=("owner-attestation refused: the manifest has validation issues the LLM "
                   "manifest reviewer would flag (" + "; ".join(str(w) for w in validate_warnings[:5])
                   + "); fix them or run the full skill_review"),
        )
    findings = [{
        "item": "owner_attestation",
        "verdict": "PASS",
        "severity": "info",
        "reason": "Owner attested this skill — the expensive LLM review was skipped; the "
                  "deterministic preflight floor still passed.",
        "model": "owner_attestation",
    }]
    # Honor the SAME lifecycle persistence guard as review_skill: when run through the
    # lifecycle, do not persist a clean verdict if the job is already terminal or the hash
    # no longer matches (a stale/superseded attestation must not overwrite live state).
    if getattr(ctx, "_skill_review_lifecycle_guard", False):
        from ouroboros.skill_review_runner import _can_persist_review_outcome
        if not _can_persist_review_outcome(
            drive_root, skill.name, content_hash,
            expected_job_id=str(getattr(ctx, "_skill_review_lifecycle_job_id", "") or ""),
        ):
            return _sr.SkillReviewOutcome(
                skill_name=skill.name, status=_sr.STATUS_PENDING, content_hash=content_hash,
                error="owner-attestation was not persisted: the lifecycle job is terminal or "
                      "no longer matches this content hash",
            )
    review_state = SkillReviewState(
        status=_sr.STATUS_CLEAN,
        content_hash=content_hash,
        findings=findings,
        reviewer_models=["owner_attestation"],
        timestamp=utc_now_iso(),
        prompt_chars=0,
        cost_usd=0.0,
        raw_result="",
        raw_actor_records=[],
        review_profile="owner_attested",
    )
    save_review_state(drive_root, skill.name, review_state)
    _sr._append_skill_review_history(
        drive_root, skill.name, status=_sr.STATUS_CLEAN, content_hash=content_hash, findings=findings,
    )
    marker_path = skill_state_dir(drive_root, skill.name) / "owner_attestation.json"
    atomic_write_json(marker_path, {"attested_at": utc_now_iso(), "content_hash": content_hash})
    skill.review = review_state
    return _sr.SkillReviewOutcome(
        skill_name=skill.name,
        status=_sr.STATUS_CLEAN,
        findings=findings,
        reviewer_models=["owner_attestation"],
        content_hash=content_hash,
        review_profile="owner_attested",
    )


def review_skill_owner_attest(ctx: Any, skill_name: str):
    """``review_impl`` variant for the owner-attestation endpoint: load + content-hash the
    skill exactly like ``review_skill``, then run the preflight-floored owner attestation
    (no LLM phase). Reused through ``run_skill_review_lifecycle`` so it shares the standard
    job tracking / payload shape."""
    drive_root = pathlib.Path(getattr(ctx, "drive_root", pathlib.Path.home() / "Ouroboros" / "data"))
    skill = find_skill(drive_root, skill_name)
    if skill is None:
        return _sr.SkillReviewOutcome(
            skill_name=skill_name, status=_sr.STATUS_PENDING,
            error=f"Skill {skill_name!r} not found in the external skills checkout",
        )
    if skill.load_error:
        return _sr.SkillReviewOutcome(
            skill_name=skill_name, status=_sr.STATUS_PENDING,
            error=f"Skill manifest could not be parsed: {skill.load_error}",
        )
    # Owner-attestation is for the owner's OWN skills only: a locally-authored/installed
    # `external` payload or anything the owner/agent self-authored. Marketplace-managed
    # payloads (native core / clawhub / ouroboroshub) are THIRD-PARTY and must still go
    # through the full LLM review. Reject those FIRST and unconditionally — even if some path
    # mislabels a third-party skill self-authored, it can never be owner-attested — then allow
    # only an external or self-authored (non-third-party) payload.
    source = str(getattr(skill, "source", "") or "")
    third_party = source in (SKILL_SOURCE_NATIVE, SKILL_SOURCE_CLAWHUB, SKILL_SOURCE_OUROBOROSHUB)
    attestable = not third_party and (
        source == SKILL_SOURCE_EXTERNAL or getattr(skill, "is_self_authored", False)
    )
    if not attestable:
        return _sr.SkillReviewOutcome(
            skill_name=skill.name, status=_sr.STATUS_PENDING,
            error=(f"Skill {skill.name!r} is marketplace/native-managed "
                   f"(source={source!r}); owner-attestation is only for "
                   "the owner's own external/self-authored skills — run the full review instead."),
        )
    try:
        content_hash = compute_content_hash(
            skill.skill_dir,
            manifest_entry=skill.manifest.entry,
            manifest_scripts=skill.manifest.scripts,
        )
    except SkillPayloadUnreadable as exc:
        return _sr.SkillReviewOutcome(
            skill_name=skill.name, status=_sr.STATUS_PENDING,
            error=f"Skill payload {exc.relpath!r} is unreadable; cannot owner-attest a partial hash.",
        )
    return run_owner_attestation(ctx, drive_root, skill, content_hash)
