"""Atlas-backed deep self-review against BIBLE.md using a large-context model."""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

# Pack filtering is shared with scope review.
from ouroboros.tools.review_context_atlas import (  # noqa: E402
    ReviewContextAtlasRequest,
    compile_review_context_atlas,
)
from ouroboros.tools.review_helpers import (  # noqa: E402
    _MAX_FULL_REPO_FILE_BYTES,
    REVIEW_PROMPT_TOKEN_BUDGET,
    calibrated_input_token_limit,
)
from ouroboros.utils import atomic_write_json, estimate_tokens, utc_now_iso  # noqa: E402
from ouroboros.config import get_context_mode, get_deep_self_review_model, resolve_effort  # noqa: E402
from ouroboros.provider_models import provider_for_model, provider_has_credentials  # noqa: E402
from ouroboros.context_layout import generate_doc_nav_map  # noqa: E402

# Non-agent visual assets.
_SKIP_DIR_PREFIXES = (
    "assets/",
)

# Output reservation inside the reviewer's 1M window (same class of fix as
# scope_review._SCOPE_INPUT_TOKEN_LIMIT): 920K input + 100K output exceeds 1M
# and yields a deterministic provider 400, so the assembled INPUT prompt is
# gated on min(SSOT budget, window − output − tokenizer margin).
_DEEP_MAX_OUTPUT_TOKENS = 100_000
_DEEP_MODEL_CONTEXT_WINDOW = 1_000_000
_DEEP_OUTPUT_MARGIN_TOKENS = 155_000
_DEEP_INPUT_TOKEN_LIMIT = min(
    REVIEW_PROMPT_TOKEN_BUDGET,
    _DEEP_MODEL_CONTEXT_WINDOW - _DEEP_MAX_OUTPUT_TOKENS - _DEEP_OUTPUT_MARGIN_TOKENS,
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

# The omission section is appended to the pack AFTER the atlas has filled its
# budget, so it must be (a) bounded and (b) reserved inside atlas_fixed_tokens.
# An unbounded per-file listing here is exactly what historically overflowed the
# assembled prompt past the final gate by a few hundred tokens (the atlas filled
# to its ceiling, then the uncounted omission listing was appended on top).
_OMISSION_SECTION_RESERVE_TOKENS = 2_000
_OMISSION_SAMPLE_MAX_ENTRIES = 40

# Bonus scale for graph-centrality ranking (D2). Bounded well below the atlas's
# force/anchor/canonical tiers (10000/9000/8000) so protected and governance
# surfaces always outrank a merely well-connected module; meaningfully above the
# generic path-prefix bonuses (~200) so hub modules win among peers.
_CENTRALITY_MAX_BONUS = 600.0
_CENTRALITY_PER_IMPORTER = 30.0

_SYSTEM_PROMPT = """\
You are conducting a deep self-review of the Ouroboros project — a self-creating AI agent.

Primary directive: The Constitution (BIBLE.md) is your absolute reference.
Every finding must be checked against it.

What to look for: bugs, crashes, race conditions,
BIBLE.md violations (P0–P12), contradictions between code and docs,
security gaps, dead code, missing error handling, architectural issues,
known error patterns from patterns.md that remain unfixed, and ideas how to improve Ouroboros to work better and better comply with the Bible.

How to work: Use the generated atlas coverage manifest systematically. Raw code is
included for selected functional/protected surfaces; every tracked file is still
accounted for by hash, size, classification, and omission/manifest disposition.
Cross-reference interactions between modules. Prioritize: CRITICAL > IMPORTANT > ADVISORY.

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
    """Append a BOUNDED omission summary: counts per reason + a capped sample.

    Full per-file coverage (hash, size, disposition, reason for every tracked
    path) already lives in the atlas coverage manifest persisted to
    ``state/deep_self_review_context.json`` — this in-prompt section is a
    summary with an explicit pointer, not the coverage SSOT. Its size is
    reserved via ``_OMISSION_SECTION_RESERVE_TOKENS`` in ``atlas_fixed_tokens``
    and enforced here, so the assembled prompt provably fits the gate the
    atlas budgeted for. The cap is an explicit, visible summarization with an
    omission note — not silent truncation.
    """
    if not skipped:
        return
    counts: dict[str, int] = {}
    for entry in skipped:
        tag = entry.split("(", 1)[1].split(":", 1)[0].strip() if "(" in entry else "other"
        counts[tag] = counts.get(tag, 0) + 1
    header = [
        "## OMITTED FILES (not included in review pack)",
        "Reasons: sensitive=secrets/keys, vendored/minified=third-party bundled asset, "
        "binary/media=images/fonts/compiled blobs, excluded_dir=non-agent-logic directory, "
        "excluded_test=wider tests excluded, oversized=>1MB, read_error=unreadable, "
        "budget_omitted=required atlas file did not fit.",
        "Full per-file coverage for every tracked path is in the atlas coverage "
        "manifest (persisted to state/deep_self_review_context.json).",
        "",
        "Omitted counts by reason: "
        + ", ".join(f"{tag}={count}" for tag, count in sorted(counts.items())),
        "",
    ]
    sample = skipped[:_OMISSION_SAMPLE_MAX_ENTRIES]
    lines = header + [f"Sample ({len(sample)} of {len(skipped)} entries):"]
    lines.extend(f"  - {entry}" for entry in sample)
    if len(skipped) > len(sample):
        lines.append(
            f"  - … {len(skipped) - len(sample)} more entries omitted here "
            "(complete list in the coverage manifest)"
        )
    section = "\n".join(lines) + "\n"
    # Defensive hard bound: pathological entry lengths must never exceed the
    # reserve the atlas budgeted for. Trim sample rows (never the header) with a
    # visible note until the section fits.
    while estimate_tokens(section) > _OMISSION_SECTION_RESERVE_TOKENS and sample:
        sample = sample[: max(0, len(sample) - 5)]
        lines = header + [f"Sample ({len(sample)} of {len(skipped)} entries):"]
        lines.extend(f"  - {entry}" for entry in sample)
        lines.append(
            f"  - … {len(skipped) - len(sample)} more entries omitted here to fit "
            "the reserved omission budget (complete list in the coverage manifest)"
        )
        section = "\n".join(lines) + "\n"
    parts.append(section)


def _compute_graph_centrality(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> Dict[str, float]:
    """Per-path centrality bonus from the code-intelligence import graph.

    Reverse-import in-degree over ``resolved_import_paths``: a module imported
    by many others is structurally load-bearing and the most useful raw code to
    inline in a bounded full-repo pack. Returns a bounded score bonus per
    rel_path; empty dict on any failure (ranking then falls back to the atlas's
    existing path/size heuristics — selection still works, just less informed).
    Deep-review-only: scope/plan review never pass centrality to the atlas.
    """
    try:
        from ouroboros.code_intelligence import build_code_inventory

        inventory = build_code_inventory(repo_dir, drive_root=drive_root, persist=True)
        in_degree: Dict[str, int] = {}
        for file in inventory.files:
            for target in file.resolved_import_paths or ():
                if target and target != file.path:
                    in_degree[target] = in_degree.get(target, 0) + 1
        return {
            path: min(_CENTRALITY_MAX_BONUS, count * _CENTRALITY_PER_IMPORTER)
            for path, count in in_degree.items()
            if count > 0
        }
    except Exception:
        # Keep the documented "empty dict on ANY failure" contract: inventory
        # shape drift must degrade to heuristic ranking, not kill the review.
        log.debug("Graph centrality unavailable; using heuristic ranking", exc_info=True)
        return {}


def build_review_pack(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    fixed_prompt_tokens: int = 0,
    hard_budget_reduction: int = 0,
    input_token_limit: int = 0,
) -> Tuple[str, Dict[str, Any]]:
    """Build bounded repo atlas + full memory whitelist pack.

    ``hard_budget_reduction`` lowers the budgets handed to the atlas — used by
    the final-shrink retry in ``run_deep_self_review`` when estimator drift
    between the atlas's per-section accounting and the final concatenation
    pushes the assembled prompt over the input gate. ``input_token_limit``
    overrides the default GPT-family cap with the model-family-calibrated cap
    resolved by the caller (Claude-family reviewers need a smaller estimated
    budget for the same 1M window — see review_helpers).
    """
    tracked, fatal = _dulwich_tracked_paths(repo_dir)
    if fatal:
        return "", {"file_count": 0, "total_chars": 0, "skipped": fatal}

    skipped: list[str] = []
    memory_parts: list[str] = []
    memory_count = _append_memory_whitelist(memory_parts, skipped, drive_root=drive_root)
    memory_text = "\n".join(memory_parts)

    # Low context mode: render ARCHITECTURE.md as a navigation map (full sections
    # read on demand) and exclude it from the atlas full-file selection instead of
    # inlining ~32K tokens. Reuses the atlas ``already_included`` mechanism so the
    # shared commit-gate atlas (scope / plan review) is unaffected.
    nav_parts: list[str] = []
    already_included: frozenset[str] = frozenset()
    if get_context_mode() == "low":
        try:
            arch_text = (repo_dir / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        except Exception:
            arch_text = ""
        if arch_text.strip():
            nav_parts.append(
                generate_doc_nav_map(
                    arch_text, title="ARCHITECTURE.md", rel_path="docs/ARCHITECTURE.md"
                )
                + "\n\nNote for this deep self-review call: this surface has no tool loop, "
                "so the navigation map is an index of omitted sections, not an actionable "
                "read_file instruction. Flag any needed full ARCHITECTURE.md section explicitly."
            )
            already_included = frozenset({"docs/ARCHITECTURE.md"})

    # Reserve the (bounded) omission section inside the atlas's fixed budget —
    # it is appended to the pack after the atlas fills, so an unreserved section
    # arithmetically guarantees overflow whenever the atlas reaches its ceiling.
    atlas_fixed_tokens = (
        int(fixed_prompt_tokens)
        + estimate_tokens(memory_text)
        + estimate_tokens("\n".join(nav_parts))
        + _OMISSION_SECTION_RESERVE_TOKENS
    )
    effective_limit = int(input_token_limit) or _DEEP_INPUT_TOKEN_LIMIT
    hard_budget = max(10_000, effective_limit - max(0, int(hard_budget_reduction)))
    centrality = _compute_graph_centrality(repo_dir, drive_root)

    def _compile(compact: bool):
        return compile_review_context_atlas(
            ReviewContextAtlasRequest(
                repo_dir=repo_dir,
                tracked_paths=tuple(tracked),
                already_included=already_included,
                fixed_prompt_tokens=atlas_fixed_tokens,
                target_total_tokens=min(850_000, hard_budget),
                hard_total_tokens=hard_budget,
                include_tests=False,
                title="Generated Deep Self-Review Atlas",
                compact_manifest=compact,
                centrality_scores=centrality,
            )
        )

    atlas = _compile(False)
    if atlas.status == "budget_exceeded":
        # Graceful compact retry (mirrors scope review): the durable manifest
        # keeps full per-file coverage while the visible prompt switches to the
        # compact coverage index, freeing manifest tokens for required files.
        atlas = _compile(True)
    if atlas.status == "budget_exceeded":
        return "", {
            "file_count": 0,
            "total_chars": 0,
            "skipped": ["FATAL: generated repository atlas exceeded hard budget even with the compact manifest"],
            "context_manifest": atlas.manifest,
        }
    skipped.extend(
        f"{record.rel_path} ({record.disposition}: {record.reason})"
        for record in atlas.omitted
        if record.disposition not in {"already_included", "manifest_only"}
    )
    parts = [atlas.text]
    parts.extend(nav_parts)
    parts.extend(memory_parts)
    file_count = len(atlas.selected) + memory_count
    _append_omission_section(parts, skipped)

    pack_text = "\n".join(parts)
    stats = {
        "file_count": file_count,
        "total_chars": len(pack_text),
        "skipped": skipped,
        "context_manifest": atlas.manifest,
    }
    return pack_text, stats


def is_review_available() -> Tuple[bool, Optional[str]]:
    """Return whether a suitable large-context review model is configured.

    Provider/credential knowledge comes from the provider registry SSOT; the
    one deliberate deep-review-specific rule kept here: ``openai::`` is only
    trusted when ``OPENAI_BASE_URL`` is unset (a redirected endpoint cannot be
    assumed to honor the 1M-context contract this review depends on).
    """
    configured = get_deep_self_review_model()
    provider = provider_for_model(configured)
    if provider == "openai":
        if provider_has_credentials("openai") and not os.environ.get("OPENAI_BASE_URL"):
            return True, configured
        return False, None
    if configured.startswith("openai/"):
        # OpenRouter route with a direct-OpenAI rewrite fallback.
        if provider_has_credentials("openrouter"):
            return True, configured
        if provider_has_credentials("openai") and not os.environ.get("OPENAI_BASE_URL"):
            return True, "openai::" + configured.split("/", 1)[1]
        return False, None
    if provider_has_credentials(provider):
        return True, configured
    return False, None


def run_deep_self_review(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    llm: Any,
    emit_progress: Callable[[str], None],
    event_queue: Any,
    model: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """Execute full-project deep review; return error text instead of raising.

    no_proxy=True avoids macOS fork-safety SIGSEGV by using a one-shot httpx
    client with trust_env=False in llm.py; regular task calls are unaffected.
    """
    try:
        # Resolve the reviewer BEFORE building the pack: the input cap is
        # model-family-calibrated (Claude-family tokenizers need a smaller
        # estimated budget for the same 1M window — see review_helpers).
        if not model:
            available, model = is_review_available()
            if not available:
                return (
                    "❌ Deep self-review unavailable: configure "
                    "OUROBOROS_MODEL_DEEP_SELF_REVIEW and the matching provider API key."
                ), {}
        input_limit = calibrated_input_token_limit(
            model,
            context_window=_DEEP_MODEL_CONTEXT_WINDOW,
            output_reserve=_DEEP_MAX_OUTPUT_TOKENS,
            tokenizer_margin=_DEEP_OUTPUT_MARGIN_TOKENS,
        )

        emit_progress("Building generated review atlas and memory pack...")
        pack_text, stats = build_review_pack(
            repo_dir,
            drive_root,
            fixed_prompt_tokens=estimate_tokens(_SYSTEM_PROMPT),
            input_token_limit=input_limit,
        )
        if not pack_text and stats.get("skipped"):
            return f"❌ Failed to build review pack: {stats['skipped'][0]}", {}

        emit_progress(
            f"Review pack built: {stats['file_count']} files, "
            f"{stats['total_chars']:,} chars"
            + (f", {len(stats['skipped'])} skipped" if stats["skipped"] else "")
        )

        # Gate full system+pack like scope review: reserve output headroom
        # inside the 1M window (min(SSOT, window − output − margin)) so a large
        # pack cannot trigger the deterministic input+output>window provider 400.
        estimated_tokens = estimate_tokens(_SYSTEM_PROMPT + pack_text)
        if estimated_tokens > input_limit:
            # Deterministic final shrink (instead of the historical fatal error):
            # rebuild once with the atlas budget reduced by the measured overage
            # plus margin, so residual estimator drift between per-section
            # accounting and this final concatenation cannot kill the review.
            overage = estimated_tokens - input_limit
            emit_progress(
                f"Pack overshot the input limit by ~{overage:,} tokens; "
                "rebuilding with a tighter atlas budget..."
            )
            pack_text, stats = build_review_pack(
                repo_dir,
                drive_root,
                fixed_prompt_tokens=estimate_tokens(_SYSTEM_PROMPT),
                hard_budget_reduction=overage + 8_000,
                input_token_limit=input_limit,
            )
            if not pack_text and stats.get("skipped"):
                return f"❌ Failed to build review pack: {stats['skipped'][0]}", {}
            estimated_tokens = estimate_tokens(_SYSTEM_PROMPT + pack_text)
        full_prompt_chars = len(_SYSTEM_PROMPT) + len(pack_text)
        if estimated_tokens > input_limit:
            return (
                f"❌ Review pack too large: ~{estimated_tokens:,} tokens "
                f"({full_prompt_chars:,} chars of system+pack, {stats['file_count']} files). "
                f"Maximum is ~{input_limit:,} tokens "
                f"(window minus {_DEEP_MAX_OUTPUT_TOKENS:,} output reserve, "
                f"calibrated for {model}). "
                "Reduce codebase size or split review."
            ), {}

        if stats.get("context_manifest"):
            try:
                atomic_write_json(
                    drive_root / "state" / "deep_self_review_context.json",
                    {
                        "ts": utc_now_iso(),
                        "model": model,
                        "context_manifest": stats["context_manifest"],
                    },
                    trailing_newline=True,
                )
            except Exception:
                log.warning("Failed to persist deep self-review context manifest", exc_info=True)

        emit_progress(f"Sending to {model} (~{estimated_tokens:,} tokens). This may take several minutes...")

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": pack_text},
        ]

        # no_proxy prevents macOS fork-safety SIGSEGV in bundled child process.
        from ouroboros.llm_observability import chat_observed

        response, usage = chat_observed(
            llm,
            drive_root=drive_root,
            task_id="deep_self_review",
            call_type="deep_self_review",
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort=resolve_effort("deep_self_review"),
            max_tokens=_DEEP_MAX_OUTPUT_TOKENS,
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
