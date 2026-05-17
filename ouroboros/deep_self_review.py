"""
Ouroboros — Deep self-review module.

Builds a full review pack (all git-tracked code + memory whitelist) and sends it
to a 1M-context model for a single-pass deep review against the Constitution.
"""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

# Filtering and pack iteration — shared with scope review (P7).
from ouroboros.tools.review_helpers import (  # noqa: E402
    _BINARY_SNIFF_BYTES,
    _MAX_FULL_REPO_FILE_BYTES,
    _is_probably_binary,
    iter_repo_pack_entries,
)
from ouroboros.utils import estimate_tokens  # noqa: E402

# Directory prefixes to skip entirely (relative to repo_dir, using forward slashes).
# - assets/ : README screenshots and app icons — no agent logic
_SKIP_DIR_PREFIXES = (
    "assets/",
)

_MEMORY_WHITELIST = [
    "memory/identity.md",
    "memory/scratchpad.md",
    "memory/registry.md",
    "memory/WORLD.md",
    "memory/knowledge/index-full.md",
    "memory/knowledge/patterns.md",
    "memory/knowledge/improvement-backlog.md",
]

_SYSTEM_PROMPT = """\
You are conducting a deep self-review of the Ouroboros project — a self-creating AI agent.

Primary directive: The Constitution (BIBLE.md) is your absolute reference.
Every finding must be checked against it.

What to look for: bugs, crashes, race conditions,
BIBLE.md violations (P0–P12), contradictions between code and docs,
security gaps, dead code, missing error handling, architectural issues,
known error patterns from patterns.md that remain unfixed, and ideas how to improve Ouroboros to work better and better comply with the Bible.

How to work: Read every file systematically. Cross-reference interactions
between modules. Prioritize: CRITICAL > IMPORTANT > ADVISORY.

Output: Structured report with prioritized findings, each citing the
specific file, line/section, the problem, and the proposed fix."""


def _dulwich_tracked_paths(repo_dir: pathlib.Path) -> tuple[list[str], list[str]]:
    """Return git-tracked paths through dulwich for macOS fork safety."""
    try:
        import dulwich.repo as _dulwich_repo  # local import — avoid top-level cost if unused
        _repo = _dulwich_repo.Repo(str(repo_dir))
        tracked = sorted(p.decode("utf-8", errors="replace") for p in _repo.open_index())
        if not tracked:
            raise RuntimeError("dulwich index is empty — cannot build review pack")
        return tracked, []
    except ImportError:
        return [], ["FATAL: dulwich not installed. Run: pip install dulwich"]
    except Exception as exc:
        return [], [f"FATAL: {exc}"]


def _append_memory_whitelist(
    parts: list[str],
    skipped: list[str],
    *,
    drive_root: pathlib.Path,
) -> int:
    file_count = 0
    for rel_mem in _MEMORY_WHITELIST:
        full_path = drive_root / rel_mem
        try:
            if not full_path.is_file():
                continue
            size = full_path.stat().st_size
            if size > _MAX_FULL_REPO_FILE_BYTES:
                skipped.append(f"drive/{rel_mem} (>{_MAX_FULL_REPO_FILE_BYTES // 1024}KB)")
                continue
            content = full_path.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                continue
            parts.append(f"## FILE: drive/{rel_mem}\n{content}\n")
            file_count += 1
        except Exception as exc:
            skipped.append(f"drive/{rel_mem} (read error: {exc})")
    return file_count


def _append_omission_section(parts: list[str], skipped: list[str]) -> None:
    if not skipped:
        return
    omission_lines = [
        "## OMITTED FILES (not included in review pack)",
        "These files were excluded. Reasons: sensitive=secrets/keys, "
        "vendored/minified=third-party bundled asset, binary/media=images/fonts/compiled blobs, "
        "excluded_dir=non-agent-logic directory (assets/), "
        "too_large=>1MB, read_error=unreadable.",
        "",
    ]
    omission_lines.extend(f"  - {entry}" for entry in skipped)
    parts.append("\n".join(omission_lines) + "\n")


def build_review_pack(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> Tuple[str, Dict[str, Any]]:
    """Build the full review pack from git-tracked files + memory whitelist.

    Returns (pack_text, stats) where stats has keys: file_count, total_chars, skipped.
    NO chunking, NO silent truncation.
    """
    tracked, fatal = _dulwich_tracked_paths(repo_dir)
    if fatal:
        return "", {"file_count": 0, "total_chars": 0, "skipped": fatal}

    entries, skipped = iter_repo_pack_entries(
        repo_dir,
        tracked_paths=tracked,
        skip_dir_prefixes=_SKIP_DIR_PREFIXES,
        include_oversized_placeholder=True,
    )
    parts = [f"## FILE: {rel}\n{content}\n" for rel, content, _lang, _note in entries]
    file_count = sum(1 for _rel, content, _lang, _note in entries if not content.startswith("[SKIPPED:"))
    file_count += _append_memory_whitelist(parts, skipped, drive_root=drive_root)
    _append_omission_section(parts, skipped)

    pack_text = "\n".join(parts)
    stats = {
        "file_count": file_count,
        "total_chars": len(pack_text),
        "skipped": skipped,
    }
    return pack_text, stats


def is_review_available() -> Tuple[bool, Optional[str]]:
    """Check if a suitable 1M-context model is available.

    Returns (available, model_id).
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        return True, "openai/gpt-5.5-pro"
    if os.environ.get("OPENAI_API_KEY") and not os.environ.get("OPENAI_BASE_URL"):
        return True, "openai::gpt-5.5-pro"
    return False, None


def run_deep_self_review(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    llm: Any,
    emit_progress: Callable[[str], None],
    event_queue: Any,
    model: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """Execute a deep self-review of the entire project.

    Returns (review_text, usage_dict). On any error, returns an error string
    with empty usage instead of raising.

    macOS fork-safety note
    ----------------------
    When the Ouroboros app bundle uses fork() to spawn the inner server.py
    subprocess, the child process inherits a multithreaded parent state.
    The first httpx HTTP request triggers macOS proxy detection via
    SCDynamicStoreCopyProxiesWithOptions() / CFPreferences, which is not
    fork-safe and causes a SIGSEGV (exit code -11, confirmed in macOS
    crash reports via the ``"crashed on child side of fork pre-exec"``
    marker in the ``asi`` field).

    We work around this by asking the shared LLMClient to send this one
    call with ``trust_env=False`` so httpx never consults env-vars or the
    OS proxy API.  The flag is passed through
    ``llm.chat(..., no_proxy=True)`` and handled only in the
    ``_chat_remote`` path of ``llm.py``.  Regular task LLM calls are
    unaffected.
    """
    try:
        # 1. Build pack
        emit_progress("Building review pack (reading all tracked files)...")
        pack_text, stats = build_review_pack(repo_dir, drive_root)
        # Check for fatal build failure (fail closed)
        if not pack_text and stats.get("skipped"):
            return f"❌ Failed to build review pack: {stats['skipped'][0]}", {}

        emit_progress(
            f"Review pack built: {stats['file_count']} files, "
            f"{stats['total_chars']:,} chars"
            + (f", {len(stats['skipped'])} skipped" if stats["skipped"] else "")
        )

        # 2. Estimate tokens and check limit
        # Budget aligned with scope/plan review at 850K input tokens. Uses the
        # shared estimate_tokens helper (chars/4) so the effective char budget
        # is identical across scope/plan/deep surfaces. The gate is applied to
        # the FULL assembled prompt (system + user), matching how scope_review
        # gates its assembled prompt and plan_review gates system+user. Gating
        # only on pack_text would understate the real request size.
        #
        # Math: the deep-review model (by default `openai/gpt-5.5-pro`, see
        # `is_review_available`) has a 1M context window that is shared between
        # input and output. chars/4 under-counts real tokens by ~15%, so actual
        # input at gate=850K is ≈1M. Output `max_tokens` lives inside the same
        # window, so near-gate prompts sit close to the API ceiling — the skip
        # path is best-effort, not a hard guarantee.
        from ouroboros.tools.review_helpers import REVIEW_PROMPT_TOKEN_BUDGET
        full_prompt_chars = len(_SYSTEM_PROMPT) + len(pack_text)
        estimated_tokens = estimate_tokens(_SYSTEM_PROMPT + pack_text)
        if estimated_tokens > REVIEW_PROMPT_TOKEN_BUDGET:
            return (
                f"❌ Review pack too large: ~{estimated_tokens:,} tokens "
                f"({full_prompt_chars:,} chars of system+pack, {stats['file_count']} files). "
                f"Maximum is ~{REVIEW_PROMPT_TOKEN_BUDGET:,} tokens. Reduce codebase size or split review."
            ), {}

        # 3. Determine model
        if not model:
            available, model = is_review_available()
            if not available:
                return "❌ Deep self-review unavailable: no OPENROUTER_API_KEY or OPENAI_API_KEY configured.", {}

        emit_progress(f"Sending to {model} (~{estimated_tokens:,} tokens). This may take several minutes...")

        # 4. Build messages
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": pack_text},
        ]

        # 5. Call LLM with no_proxy=True to prevent macOS fork-safety SIGSEGV.
        #    The flag is forwarded to _chat_remote in llm.py which builds a
        #    one-shot httpx.Client(trust_env=False, mounts={}).
        response, usage = llm.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort="high",
            max_tokens=100_000,
            temperature=None,
            no_proxy=True,
        )

        text = response.get("content") or ""
        if not text:
            return "⚠️ Model returned an empty response for the deep self-review.", usage or {}

        emit_progress(f"Deep self-review complete ({len(text):,} chars).")
        return text, usage or {}

    except Exception as e:
        log.error("Deep self-review failed: %s", e, exc_info=True)
        return f"❌ Deep self-review failed: {type(e).__name__}: {e}", {}
