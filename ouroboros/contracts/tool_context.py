"""Minimal ``ToolContextProtocol`` — the frozen ABI between tools and the runtime.

Rationale
---------
The concrete dataclass ``ouroboros.tools.registry.ToolContext`` is a convenient
carrier with ~20 fields (browser state, event queue, review history, LLM
overrides, …). Individual tool handlers only depend on a much smaller subset.

``ToolContextProtocol`` declares that minimum surface explicitly (6 attributes
plus 3 path helpers) so that:

- external skills/extensions can type-annotate against a small, stable ABI
  instead of the evolving ``ToolContext`` dataclass;
- contract tests can pin the duck-typed invariant that the dataclass still
  satisfies the protocol (``test_contracts.py``);
- future refactors of ``ToolContext`` cannot silently drop one of these
  fields without breaking the test suite.

It is a ``runtime_checkable`` Protocol, so ``isinstance(ctx, ToolContextProtocol)``
works at runtime, but **adds no new behaviour**. Existing tools are not
required to import from this module.
"""

from __future__ import annotations

import pathlib
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class ToolContextProtocol(Protocol):
    """The minimum set of attributes every tool handler may rely on.

    Every attribute listed here is already present on the concrete
    ``ouroboros.tools.registry.ToolContext`` dataclass. Do **not** extend this
    protocol without a deliberate contract bump — third-party skills and
    extensions will pin against it.
    """

    # --- Filesystem roots (always set before any tool runs) ---
    repo_dir: pathlib.Path
    drive_root: pathlib.Path

    # --- Event surface ---
    # ``pending_events`` is a mutable list the runtime drains into the UI.
    # ``emit_progress_fn`` is a best-effort callback for single-string progress.
    pending_events: list
    emit_progress_fn: Callable[[str], Any]

    # --- Addressing / routing ---
    # ``current_chat_id`` and ``task_id`` may be ``None`` when a tool runs
    # outside a task (e.g. background setup); callers must tolerate that.
    current_chat_id: Any
    task_id: Any

    # --- Safe path helpers (boundary-checked against repo_dir/drive_root) ---
    def repo_path(self, rel: str) -> pathlib.Path: ...
    def drive_path(self, rel: str) -> pathlib.Path: ...
    def drive_logs(self) -> pathlib.Path: ...


__all__ = ["ToolContextProtocol"]
