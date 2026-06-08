"""Regression test for PR-B: retry backoff (#15).

(#14 — not billing provider-glitch empties — was moved to CONSULT-BUGS.md: doing
it correctly requires deciding whether the durable usage SSOT in events.jsonl
should exclude finish_reason=null responses, a provider-billing semantics call
left to the maintainer.)
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_backoff_doubled_with_cap():
    src = (REPO / "ouroboros" / "loop_llm_call.py").read_text(encoding="utf-8")
    assert "min(2 ** attempt * 4, 30)" in src      # doubled per-attempt backoff
    assert "min(2 ** attempt * 2, 30)" not in src  # old value gone
