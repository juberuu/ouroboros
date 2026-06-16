"""WS2 — skill-dispatch resilience (v6.34.0).

A1: the ctx calling-convention is decided on the RAW handler (the runtime wrapper
is (*args, **kwargs), so inspecting it always forces a ctx-first call → TypeError
for keyword-only / zero-arg handlers like unix_computer_use wait/capabilities).
(A2's proposed _execution_lock skip-for-no-deps was withdrawn: it reopened the
cross-skill dependency-leak the lock guards — see test_extension_isolated_deps.)
"""

from __future__ import annotations

import functools
import inspect


def test_handler_wants_ctx_raw_vs_wrapper():
    from ouroboros.extension_process_runner import _handler_wants_ctx

    # ctx-less handlers must NOT receive ctx.
    assert _handler_wants_ctx(lambda: None) is False
    assert _handler_wants_ctx(lambda *, ms=500: None) is False

    def kwonly(*, x=1):
        return x

    assert _handler_wants_ctx(kwonly) is False

    # ctx-first handlers DO receive ctx.
    def ctxfn(ctx, x=1):
        return x

    assert _handler_wants_ctx(ctxfn) is True

    # The bug: a naked (*args, **kwargs) runtime wrapper reports ctx-wanted...
    def naked_wrapper(*args, **kwargs):
        return None

    assert _handler_wants_ctx(naked_wrapper) is True

    # ...the fix: a functools.wraps wrapper exposes __wrapped__ so inspect.unwrap
    # recovers the real ctx-less signature (dispatch's legacy fallback).
    @functools.wraps(kwonly)
    def wrapped(*args, **kwargs):
        return kwonly(**kwargs)

    assert _handler_wants_ctx(inspect.unwrap(wrapped)) is False


def test_dispatch_uses_stored_wants_ctx_flag_for_keyword_only_handler():
    """A keyword-only handler dispatched through the descriptor's wants_ctx flag
    is called WITHOUT ctx (no positional ctx -> no TypeError). This mirrors the
    extension_dispatch decision without standing up a full skill."""
    from ouroboros.extension_process_runner import _handler_wants_ctx

    calls = {}

    def kw_handler(*, ms=500):
        calls["ms"] = ms
        return "ok"

    # register_tool decides this on the RAW handler:
    wants_ctx = _handler_wants_ctx(kw_handler)
    assert wants_ctx is False

    ext_tool = {"handler": kw_handler, "wants_ctx": wants_ctx}
    call_args = {"ms": 250}
    _wants = ext_tool.get("wants_ctx")
    if _wants is None:
        _wants = _handler_wants_ctx(inspect.unwrap(ext_tool["handler"]))
    result = ext_tool["handler"](object(), **call_args) if _wants else ext_tool["handler"](**call_args)
    assert result == "ok"
    assert calls["ms"] == 250  # bound by name, ctx NOT passed positionally
