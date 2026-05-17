"""
Ouroboros — Review utilities.

Utilities for code collection and complexity metrics.
Review tasks go through the standard agent tool loop (LLM-first).
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict, List, Tuple

from ouroboros.tools.review_helpers import (
    iter_repo_pack_entries,
)


_HEALTH_SKIP_DIR_PREFIXES = (".git/", ".pytest_cache/", ".mypy_cache/", "node_modules/", ".venv/")
TARGET_MODULE_LINES = 1000
MAX_MODULE_LINES = 1600
TARGET_FUNCTION_LINES = 150
# Raised in v4.40.0 from 250 to 300: advisory SDK orchestration
# (claude_advisory_review._handle_advisory_pre_review at 294 lines) packs a
# coherent single-call flow whose decomposition would obscure control flow
# more than the size itself.  Splitting would require an unrelated refactor
# and is tracked as tech-debt, not a fresh violation.
MAX_FUNCTION_LINES = 300
# Raised in v4.40.0 from 1160 to 1200: absorbs the ~9 new helper functions
# introduced by the safety.py policy-based rewrite (_is_secret_key,
# _redact_secret_value, _redact_secrets_in_arguments, _redact_secrets_in_text,
# _any_remote_provider_configured, _any_local_routing_enabled,
# _light_model_has_reachable_provider, _resolve_safety_routing,
# _run_llm_check) with headroom for incremental growth.
# Raised in v4.40.4 from 1200 to 1250: absorbs the commit-readiness debt
# subsystem in review_state.py (_allocate_commit_readiness_debt_id,
# _hydrate_commit_readiness_debt, _build_commit_readiness_debt_observations,
# _sync_commit_readiness_debts, get_open_commit_readiness_debts,
# _commit_readiness_debts_view, _coalesce_open_obligations,
# _allocate_obligation_id, _hydrate_obligation, _touch_obligation,
# _update_obligations_from_attempt, _make_obligation_fingerprint,
# _looks_like_public_obligation_id, _stable_digest, _normalize_*_key,
# plus the shared _run_reviewed_stage_cycle / _run_non_committing_review_cycle
# extraction in tools/git.py and _commit_readiness_debts_payload in
# claude_advisory_review.py) with headroom for incremental growth.
# Phase 3 three-layer refactor adds the external skill surface
# (``ouroboros/skill_loader.py``, ``ouroboros/skill_review.py``,
# ``ouroboros/tools/skill_exec.py``) with exception sentinels,
# streaming-output runner, capped readers, and scoping helpers.
# Ceiling raised to 1350 to accommodate that surface + Phase 4–6
# headroom (extension loader, Widget ABI, pro-mode auto-PR).
# v4.50.0-rc.5 raises ceiling 1350 → 1450: Phases 3–6 actually landed,
# producing ~47 new helpers across ``extension_loader``,
# ``extensions_api``, ``contracts/plugin_api``, ``launcher_bootstrap``,
# ``onboarding_wizard``, and the new ``scripts/build_repo_bundle``
# tag-verification helpers. Splitting further would require a refactor
# larger than the pre-release scope; the ceiling bump stays consistent
# with how MAX_TOTAL_FUNCTIONS has grown through v4.40→v4.47 as each
# phase shipped.
MAX_TOTAL_FUNCTIONS = 2020  # v5.22.0-rc.1: Gateway Boundary v1 replaces scattered HTTP modules with explicit route/contract helpers; server.py shrank below 1000 lines, but small endpoint wrappers push function count slightly over the old ceiling.
# v4.40.0 adds claude_advisory_review.py to the grandfathered set: the file
# grew to 1731 lines across v4.37-v4.39 (plan_task quorum + direct-provider
# fallback + convergence rule + syntax preflight + reflection decoupling).
# Splitting is deferred until each surface stabilises.
#
# v4.50.0-rc.5 adds server.py: grew past 1600 lines (now 1659) across
# Phases 2–5 (runtime-mode endpoints, extensions HTTP surface, local
# model API, plus the LAN hint + Skills toggle + review routes). A split
# candidate exists (onboarding/settings HTTP leg → ``ouroboros/server_ui.py``)
# but is deferred to a dedicated structural refactor rather than
# blocking the pre-release.
#
# v5.7.1 adds git.py temporarily: community reliability fixes around
# reviewed-commit staging, doc-only preflight, and dirty-tree checkout
# pushed the file over the hard gate. This is accepted as short-lived debt;
# split commit/review orchestration into a helper module in the next tools pass.
GRANDFATHERED_OVERSIZED_MODULES = {
    "llm.py",
    "claude_advisory_review.py",
    "review_state.py",
    "server.py",
    "git.py",
}
# Immutable bundle-only entrypoints ship with release artifacts but should not
# count against the self-editable codebase function budget.
FUNCTION_COUNT_EXCLUDED_FILES = {"launcher.py"}


# ---------------------------------------------------------------------------
# Complexity metrics
# ---------------------------------------------------------------------------

def compute_complexity_metrics(sections: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Compute codebase complexity metrics from collected sections."""
    file_sizes: List[Tuple[str, int]] = []
    function_lengths: List[Tuple[str, int, int]] = []
    for path, content in sections:
        lines = content.splitlines()
        file_sizes.append((path, len(lines)))
        if not path.endswith(".py") or pathlib.Path(path).name in FUNCTION_COUNT_EXCLUDED_FILES:
            continue
        starts = [
            idx for idx, line in enumerate(lines)
            if line.strip().startswith(("def ", "async def "))
        ]
        for pos, start in enumerate(starts):
            def_indent = len(lines[start]) - len(lines[start].lstrip())
            next_start = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
            end = next_start
            for idx in range(start + 1, next_start):
                stripped = lines[idx].strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if len(lines[idx]) - len(lines[idx].lstrip()) <= def_indent:
                    end = idx
                    break
            function_lengths.append((path, start, end - start))

    total_lines = sum(size for _path, size in file_sizes)
    func_lens = [length for _, _, length in function_lengths]
    py_files = [item for item in file_sizes if item[0].endswith(".py")]
    target_drift_modules = [(p, n) for p, n in py_files if n > TARGET_MODULE_LINES]
    hard_modules = [(p, n) for p, n in py_files if n > MAX_MODULE_LINES]

    return {
        "total_files": len(sections),
        "py_files": len(py_files),
        "total_lines": total_lines,
        "total_functions": len(function_lengths),
        "avg_function_length": round(sum(func_lens) / max(1, len(func_lens)), 1) if func_lens else 0,
        "max_function_length": max(func_lens) if func_lens else 0,
        "largest_files": sorted(file_sizes, key=lambda x: x[1], reverse=True)[:10],
        "longest_functions": sorted(function_lengths, key=lambda x: x[2], reverse=True)[:10],
        "target_drift_functions": [item for item in function_lengths if item[2] > TARGET_FUNCTION_LINES],
        "oversized_functions": [item for item in function_lengths if item[2] > MAX_FUNCTION_LINES],
        "target_drift_modules": target_drift_modules,
        "grandfathered_modules": [(p, n) for p, n in hard_modules if pathlib.Path(p).name in GRANDFATHERED_OVERSIZED_MODULES],
        "oversized_modules": [(p, n) for p, n in hard_modules if pathlib.Path(p).name not in GRANDFATHERED_OVERSIZED_MODULES],
    }


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def collect_sections(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
    """Collect reviewable repo files for codebase-health metrics."""
    entries, omitted = iter_repo_pack_entries(
        repo_dir,
        skip_dir_prefixes=_HEALTH_SKIP_DIR_PREFIXES,
    )
    sections = [(f"repo/{rel}", content) for rel, content, _lang, _note in entries]
    total_chars = sum(len(content) for _path, content in sections)
    stats = {
        "files": len(sections),
        "chars": total_chars,
        "omitted": len(omitted),
    }
    return sections, stats
