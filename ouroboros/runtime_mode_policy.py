"""Runtime-mode policy for protected Ouroboros source surfaces.

``advanced`` is allowed to evolve the application layer, but must not casually
rewrite the core contracts, safety files, or release/managed-repo invariants.
``pro`` may touch those paths only when the commit pipeline runs the extra
core-patch review gate.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Iterable


SAFETY_CRITICAL_PATHS = frozenset({
    "BIBLE.md",
    "ouroboros/safety.py",
    "ouroboros/runtime_mode_policy.py",
    "ouroboros/tools/core_patch_gate.py",
    "ouroboros/tools/registry.py",
    "prompts/SAFETY.md",
})

FROZEN_CONTRACT_PATH_PREFIXES = (
    "ouroboros/contracts/",
)

FROZEN_CONTRACT_PATHS = frozenset({
    "tests/test_contracts.py",
    "docs/CHECKLISTS.md",
})

RELEASE_INVARIANT_PATHS = frozenset({
    ".github/workflows/ci.yml",
    "Ouroboros.spec",
    "build.sh",
    "build_linux.sh",
    "build_windows.ps1",
    "scripts/build_repo_bundle.py",
    "ouroboros/launcher_bootstrap.py",
    "supervisor/git_ops.py",
})

PROTECTED_RUNTIME_PATH_PREFIXES = FROZEN_CONTRACT_PATH_PREFIXES
PROTECTED_RUNTIME_PATHS = (
    SAFETY_CRITICAL_PATHS
    | FROZEN_CONTRACT_PATHS
    | RELEASE_INVARIANT_PATHS
)


@dataclass(frozen=True)
class ProtectedPath:
    path: str
    category: str


def normalize_repo_path(path: str) -> str:
    """Normalize a repo-relative path to forward-slash POSIX form."""
    cleaned = str(path or "").strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return pathlib.PurePosixPath(cleaned).as_posix()


def protected_path_category(path: str) -> str:
    """Return the protected-surface category for *path*, or ``""``."""
    norm = normalize_repo_path(path)
    if not norm or norm == ".":
        return ""
    if norm in SAFETY_CRITICAL_PATHS:
        return "safety-critical"
    if norm in FROZEN_CONTRACT_PATHS or any(
        norm.startswith(prefix) for prefix in FROZEN_CONTRACT_PATH_PREFIXES
    ):
        return "frozen-contract"
    if norm in RELEASE_INVARIANT_PATHS:
        return "release-invariant"
    return ""


def is_protected_runtime_path(path: str) -> bool:
    return bool(protected_path_category(path))


def protected_paths_in(paths: Iterable[str]) -> list[ProtectedPath]:
    found: list[ProtectedPath] = []
    seen: set[str] = set()
    for path in paths:
        norm = normalize_repo_path(path)
        if norm in seen:
            continue
        category = protected_path_category(norm)
        if category:
            found.append(ProtectedPath(path=norm, category=category))
            seen.add(norm)
    return found


def mode_allows_protected_write(runtime_mode: str) -> bool:
    return str(runtime_mode or "").strip().lower() == "pro"


def format_protected_paths(paths: Iterable[ProtectedPath | str]) -> str:
    rendered: list[str] = []
    for item in paths:
        if isinstance(item, ProtectedPath):
            rendered.append(f"{item.path} ({item.category})")
        else:
            category = protected_path_category(str(item))
            rendered.append(
                f"{normalize_repo_path(str(item))} ({category})"
                if category else normalize_repo_path(str(item))
            )
    return ", ".join(rendered)


def protected_write_block_message(
    *,
    path: str,
    runtime_mode: str,
    action: str,
) -> str:
    norm = normalize_repo_path(path)
    category = protected_path_category(norm)
    return (
        f"⚠️ CORE_PROTECTION_BLOCKED: runtime_mode={runtime_mode!r} refuses "
        f"to {action} protected {category or 'core'} path: {norm}. "
        "Switch to runtime_mode='pro' and let the core-patch review gate pass "
        "before committing protected core/contract/release surfaces."
    )


def core_patch_notice(paths: Iterable[ProtectedPath | str]) -> str:
    return (
        "⚠️ CORE_PATCH_NOTICE: runtime_mode='pro' is editing protected "
        "Ouroboros core/contract/release surface(s): "
        f"{format_protected_paths(paths)}. The commit pipeline must pass the "
        "extra core-patch review gate before these changes can be committed."
    )

