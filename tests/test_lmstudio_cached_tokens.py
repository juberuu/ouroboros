"""Regression guard for the LM Studio MLX ``cached_tokens=0`` documentation.

LM Studio MLX does NOT emit ``cached_tokens`` via its API — verified
on 2026-05-02 across all three endpoints. This is a documentation-only
test: it pins the explanatory comment in ``ouroboros/llm.py`` so any
later refactor that drops it has to make a deliberate decision.

If the comment ever genuinely becomes outdated (e.g. LM Studio adds
the field), update the comment AND this test together.
"""

from __future__ import annotations

import pathlib


def test_lmstudio_cached_tokens_limitation_is_documented():
    src = pathlib.Path(__file__).parent.parent / "ouroboros" / "llm.py"
    body = src.read_text(encoding="utf-8")
    assert "LM Studio MLX does NOT emit" in body, (
        "ouroboros/llm.py must document the LM Studio MLX cached_tokens "
        "limitation near the response-usage parser. Without this comment "
        "future debuggers will spend hours wondering why cached_tokens=0 "
        "even when the MLX prefix cache is clearly hitting (we did, on "
        "2026-05-02)."
    )
    # Reference at least one of the verified non-emitting endpoints so the
    # comment can't be reduced to a vague disclaimer that doesn't actually
    # tell the reader where to look.
    assert "/v1/chat/completions" in body or "/api/v0/chat/completions" in body
