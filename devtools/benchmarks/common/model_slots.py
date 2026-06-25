#!/usr/bin/env python3
"""Shared single-model benchmark helper.

A single-model benchmark run pins every model slot to one model and lightens the
review triad to ``review_slots`` copies of that model (default 1). Three identical
reviewers add latency/cost but no diversity, and a single-model run cannot achieve
reviewer-model diversity anyway; the loud ``single_reviewer_no_diversity`` signal
stays on. This is a BENCHMARK convenience, NOT a claim that review got more reliable.

Generalized here so the SWE-bench Pro adapter (which builds a settings DICT written
to the container's settings.json) and Terminal-Bench (which mutates ``os.environ``
for a harbor subprocess) can share one definition: pass ``target=<dict>`` for the
former, leave it ``None`` for the latter.
"""
from __future__ import annotations

import os
from typing import MutableMapping, Optional

# Every model slot a single-model run pins. Superset that is correct for both the
# settings.json-profile path (SWE-bench Pro) and the forwarded-env path
# (Terminal-Bench); pinning a slot a given adapter ignores is a harmless no-op.
SINGLE_MODEL_SLOT_KEYS = (
    "OUROBOROS_MODEL",
    "OUROBOROS_MODEL_HEAVY",
    "OUROBOROS_MODEL_LIGHT",
    "OUROBOROS_MODEL_FALLBACKS",
    "OUROBOROS_MODEL_DEEP_SELF_REVIEW",
    "OUROBOROS_MODEL_CONSCIOUSNESS",
    "OUROBOROS_MODEL_VISION",
    "OUROBOROS_WEBSEARCH_MODEL",
    "OUROBOROS_SCOPE_REVIEW_MODELS",
    "OUROBOROS_SCOPE_REVIEW_MODEL",
    "CLAUDE_CODE_MODEL",
)


def pin_single_model(
    model: str,
    review_slots: int = 1,
    review_effort: str = "",
    target: Optional[MutableMapping[str, str]] = None,
) -> MutableMapping[str, str]:
    """Pin every model slot to ``model`` and set the review triad to ``review_slots``
    copies of it.

    ``target=None`` mutates ``os.environ`` (host-subprocess path, e.g. Terminal-Bench);
    pass a settings dict to update it instead (e.g. SWE-bench Pro ``derive_run_settings``).
    ``review_effort`` (when non-empty) pins review + scope-review effort. Returns the
    mutated mapping. A single configured reviewer is intentionally loud
    (``single_reviewer_no_diversity``); this helper does not suppress that.
    """
    sink: MutableMapping[str, str] = os.environ if target is None else target
    for key in SINGLE_MODEL_SLOT_KEYS:
        sink[key] = model
    sink["OUROBOROS_REVIEW_MODELS"] = ",".join([model] * max(1, int(review_slots)))
    if review_effort:
        sink["OUROBOROS_EFFORT_REVIEW"] = review_effort
        sink["OUROBOROS_EFFORT_SCOPE_REVIEW"] = review_effort
    return sink
