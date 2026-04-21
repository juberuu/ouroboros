"""plan_review.py — Pre-implementation design review tool.

Runs 2-3 parallel full-codebase reviews of a proposed implementation plan
BEFORE any code is written (2 when `OUROBOROS_REVIEW_MODELS` has exactly two
distinct models, 3 when it has three distinct models; single-model and
duplicate configurations are rejected by the v4.39.0 quorum gate). Each
reviewer sees the entire repository (same as scope review) plus the plan
description and the files to be touched.

Purpose: surface forgotten touchpoints, implicit contract violations, and
simpler alternatives *before* the first edit — preventing the iterative
micro-fix spiral that makes commit-gate expensive.

Usage:
    plan_task(
        plan="I want to add X by changing Y and Z...",
        goal="What should be achieved",
        files_to_touch=["ouroboros/foo.py", "tests/test_foo.py"]  # optional
    )
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from pathlib import Path

from ouroboros.llm import LLMClient
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.review_helpers import (
    build_full_repo_pack,
    build_head_snapshot_section,
    load_checklist_section,
)
from ouroboros.utils import estimate_tokens

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Configuration
# ------------------------------------------------------------------ #

_PLAN_REVIEW_MAX_TOKENS = 65536
_PLAN_REVIEW_EFFORT = "high"

# Budget gate: skip with advisory warning if assembled prompt exceeds this token
# estimate. Unified with scope/deep review at 850K as a best-effort shared policy.
# plan_task uses the configurable `OUROBOROS_REVIEW_MODELS` set (not a fixed 1M
# model), so the exact headroom depends on each reviewer's actual context window.
# `estimate_tokens` (chars/4) under-counts real tokens by ~15%, so at gate=850K
# actual input reaches ≈1M tokens; the skip path is best-effort and individual
# reviewers may still reject oversized requests at the API level.
_PLAN_BUDGET_TOKEN_LIMIT = 850_000


# ------------------------------------------------------------------ #
# Tool registration
# ------------------------------------------------------------------ #

def get_tools():
    return [
        ToolEntry(
            name="plan_task",
            schema={
                "name": "plan_task",
                "description": (
                    "Run a pre-implementation design review of a proposed plan using 2–3 distinct "
                    "parallel full-codebase reviewers. Call this BEFORE writing any code for "
                    "non-trivial tasks (>2 files or >50 lines of changes). Each reviewer sees the "
                    "entire repository plus your plan description and the files you plan to touch. "
                    "They will identify forgotten touchpoints, implicit contract violations, simpler "
                    "alternatives, and Bible/architecture compliance issues — before you've written "
                    "a single line. Uses the distinct models configured in OUROBOROS_REVIEW_MODELS "
                    "(same slot as the commit triad). Requires at least 2 unique models for "
                    "majority-vote coordination; returns ERROR with a settings hint on single-model "
                    "or duplicate-model configurations. Returns structured feedback from every "
                    "unique reviewer with detailed explanations and alternative approaches. "
                    "Non-blocking: you decide what to do with the feedback."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan": {
                            "type": "string",
                            "description": (
                                "Describe what you plan to implement: which files you will change, "
                                "what the key design decisions are, and what you will NOT change."
                            ),
                        },
                        "goal": {
                            "type": "string",
                            "description": "The high-level goal of the task (what problem is being solved).",
                        },
                        "files_to_touch": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional list of repo-relative file paths you plan to modify. "
                                "Their current content (HEAD snapshot) will be injected so reviewers "
                                "can reason about concrete code, not just abstract plans."
                            ),
                        },
                    },
                    "required": ["plan", "goal"],
                },
            },
            handler=_handle_plan_task,
            timeout_sec=600,
        )
    ]


# ------------------------------------------------------------------ #
# Handler
# ------------------------------------------------------------------ #

def _handle_plan_task(
    ctx: ToolContext,
    plan: str = "",
    goal: str = "",
    files_to_touch: list | None = None,
) -> str:
    if not plan.strip():
        return "ERROR: plan parameter is required and must not be empty."
    if not goal.strip():
        return "ERROR: goal parameter is required and must not be empty."

    files_to_touch = files_to_touch or []

    try:
        try:
            asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run,
                    _run_plan_review_async(ctx, plan, goal, files_to_touch),
                ).result(timeout=590)
        except RuntimeError:
            result = asyncio.run(_run_plan_review_async(ctx, plan, goal, files_to_touch))
        return result
    except concurrent.futures.TimeoutError:
        return "ERROR: Plan review timed out after 590s."
    except Exception as e:
        log.error("plan_task failed: %s", e, exc_info=True)
        return f"ERROR: Plan review failed: {e}"


# ------------------------------------------------------------------ #
# Async orchestration
# ------------------------------------------------------------------ #

async def _run_plan_review_async(
    ctx: ToolContext,
    plan: str,
    goal: str,
    files_to_touch: list,
) -> str:
    repo_dir = ctx.repo_dir

    # --- Quorum validation ---
    # Two separate checks, each on its own signal:
    #
    # (1) User-authored duplicates are rejected against the RAW env var so a
    #     user who wrote `"a,a,b"` gets the hard error the task spec asks
    #     for. We must NOT validate duplicates against
    #     `config.get_review_models()` output because direct-provider
    #     fallback intentionally emits `[main, light, light]` (3 slots,
    #     2 unique) to satisfy the commit triad — those duplicates are
    #     server-generated, not user-authored.
    #
    # (2) Minimum-unique count runs on `config.get_review_models()` output
    #     (post-fallback) so single-provider setups that resolve to 2
    #     unique models after fallback still pass.
    import os as _os
    from ouroboros import config as _cfg

    raw_env = _os.environ.get("OUROBOROS_REVIEW_MODELS", "") or ""
    raw_user_list = [m.strip() for m in raw_env.split(",") if m.strip()]
    # The "no user-authored duplicates" rule must NOT fire when the raw env
    # contains exactly the auto-generated direct-provider fallback shape
    # (`[main, light, light]` or legacy `[main] * N`) — that payload is
    # persisted into `OUROBOROS_REVIEW_MODELS` by `apply_settings_to_env`
    # after `_normalize_direct_review_models` seeds the fallback, so the raw
    # env string looks like user-authored duplicates even though the agent
    # never wrote it. We only skip the check for THAT exact shape — any
    # other duplicate list (e.g. explicit user-authored `a,a,b` on a
    # direct-provider setup) must still fail the gate so majority-vote
    # coordination stays sound.
    exclusive_direct = _cfg._exclusive_direct_remote_provider_env()
    fallback_shape: list[str] = (
        _cfg.direct_provider_review_models_fallback(exclusive_direct)
        if exclusive_direct else []
    )
    is_auto_generated_fallback = bool(
        fallback_shape and raw_user_list == fallback_shape
    )
    if (
        raw_user_list
        and not is_auto_generated_fallback
        and len(set(raw_user_list)) != len(raw_user_list)
    ):
        duplicates = sorted({
            m for m in raw_user_list if raw_user_list.count(m) > 1
        })
        return (
            "ERROR: plan_task has duplicate reviewer models in "
            "OUROBOROS_REVIEW_MODELS — majority-vote is unsound when the "
            f"same model votes more than once. Duplicates: {duplicates}. "
            "Fix the setting so each model appears exactly once."
        )

    resolved_models = list(_cfg.get_review_models() or [])
    if not resolved_models:
        return (
            "ERROR: No review models configured. Set OUROBOROS_REVIEW_MODELS "
            "in settings."
        )

    unique_resolved = list(dict.fromkeys(resolved_models))
    # Majority-vote coordination requires >=2 distinct reviewers.
    if len(unique_resolved) < 2:
        single_provider_hint = ""
        if (
            resolved_models
            and all(m == resolved_models[0] for m in resolved_models)
            and len(resolved_models) >= 2
        ):
            single_provider_hint = (
                " If you are on a single-provider setup (OpenAI-only or "
                "Anthropic-only direct routing), configure distinct "
                "values for OUROBOROS_MODEL and OUROBOROS_MODEL_LIGHT so "
                "the direct-provider fallback seeds `[main, light, light]`, "
                "or add a second model explicitly via OUROBOROS_REVIEW_MODELS."
            )
        return (
            "ERROR: plan_task requires at least 2 unique reviewer models for "
            f"majority-vote coordination. Got {len(unique_resolved)} unique "
            f"model(s) from {resolved_models!r}. Fix OUROBOROS_REVIEW_MODELS "
            "in settings (example: 'openai/gpt-5.4,"
            "google/gemini-3.1-pro-preview,anthropic/claude-opus-4.7')."
            + single_provider_hint
        )

    # Quorum passed — now run on the UNIQUE reviewer set, not the padded list
    # from `_get_review_models`. Padding would let the same model cast more
    # than one vote (e.g. `[a, b, b]` from `[a, b]` env or `[main, light, light]`
    # from direct-provider fallback) and corrupt majority-vote coordination.
    # Running 2 unique reviewers is stricter and produces a sound majority;
    # the docs promise "2-3 unique reviewers".
    models = list(dict.fromkeys(_get_review_models()))

    # --- Build prompt components ---
    checklist = _load_plan_checklist()
    bible_text = _load_bible(repo_dir)
    dev_md = _load_doc(repo_dir, "docs/DEVELOPMENT.md")
    arch_md = _load_doc(repo_dir, "docs/ARCHITECTURE.md")

    # Full repo pack (same as scope review — reviewers see everything)
    ctx.emit_progress_fn("📐 plan_task: building full repo pack…")
    try:
        repo_pack, omitted = build_full_repo_pack(repo_dir, exclude_paths=set(files_to_touch))
    except Exception as e:
        return f"ERROR: Failed to build repo pack: {e}"

    omitted_note = ""
    if omitted:
        omitted_note = f"\n\n## OMITTED FILES\n" + "\n".join(f"- {p}" for p in omitted)

    # HEAD snapshots for files the agent plans to touch
    ctx.emit_progress_fn(f"📐 plan_task: reading {len(files_to_touch)} planned-touch file(s)…")
    head_snapshots = ""
    if files_to_touch:
        head_snapshots = build_head_snapshot_section(repo_dir, files_to_touch)

    # Assemble the full prompt
    system_prompt = _build_system_prompt(checklist, bible_text, dev_md, arch_md)
    user_content = _build_user_content(plan, goal, files_to_touch, head_snapshots, repo_pack, omitted_note)

    # Budget gate
    estimated_tokens = estimate_tokens(system_prompt + user_content)
    if estimated_tokens > _PLAN_BUDGET_TOKEN_LIMIT:
        return (
            f"⚠️ PLAN_REVIEW_SKIPPED: assembled prompt too large "
            f"({estimated_tokens:,} estimated tokens, limit {_PLAN_BUDGET_TOKEN_LIMIT:,}). "
            f"Consider reducing files_to_touch or splitting the plan into smaller scopes."
        )

    ctx.emit_progress_fn(
        f"📐 plan_task: running {len(models)} parallel reviewers "
        f"(~{estimated_tokens:,} tokens each)…"
    )

    # Run all models in parallel
    llm_client = LLMClient()
    semaphore = asyncio.Semaphore(3)
    tasks = [
        _query_reviewer(llm_client, model, system_prompt, user_content, semaphore)
        for model in models
    ]
    raw_results = await asyncio.gather(*tasks)

    # Track per-reviewer costs — plan_task calls 3 models (full repo pack, ~$6-8 total)
    # and these costs must reach the budget like any other LLM spend.
    _emit_plan_review_usage(ctx, raw_results)

    # Format output
    return _format_output(raw_results, models, goal, estimated_tokens)


# ------------------------------------------------------------------ #
# Single-reviewer query
# ------------------------------------------------------------------ #

def _emit_plan_review_usage(ctx: "ToolContext", raw_results: list) -> None:
    """Emit llm_usage events for each plan reviewer so costs reach the budget."""
    try:
        from ouroboros.pricing import infer_api_key_type, infer_model_category, infer_provider_from_model
        from ouroboros.utils import utc_now_iso
        for result in raw_results:
            if result.get("error"):
                continue
            tokens_in = result.get("tokens_in", 0)
            tokens_out = result.get("tokens_out", 0)
            if not tokens_in and not tokens_out:
                continue
            model = result.get("model") or result.get("request_model") or ""
            cost = float(result.get("cost", 0) or 0)
            provider = infer_provider_from_model(model)
            event = {
                "type": "llm_usage",
                "ts": utc_now_iso(),
                "task_id": getattr(ctx, "task_id", "") or "",
                "model": model,
                "api_key_type": infer_api_key_type(model, provider),
                "model_category": infer_model_category(model),
                "usage": {
                    "prompt_tokens": tokens_in,
                    "completion_tokens": tokens_out,
                    "cached_tokens": 0,
                    "cost": cost,
                },
                "provider": provider,
                "source": "plan_review",
                "category": "review",
                "cost": cost,
            }
            eq = getattr(ctx, "event_queue", None)
            if eq is not None:
                try:
                    eq.put_nowait(event)
                    continue
                except Exception:
                    pass
            pending = getattr(ctx, "pending_events", None)
            if pending is not None:
                pending.append(event)
    except Exception:
        log.debug("_emit_plan_review_usage failed (non-critical)", exc_info=True)


async def _query_reviewer(
    llm_client: LLMClient,
    model: str,
    system_prompt: str,
    user_content: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        try:
            msg, usage = await llm_client.chat_async(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                model=model,
                reasoning_effort=_PLAN_REVIEW_EFFORT,
                max_tokens=_PLAN_REVIEW_MAX_TOKENS,
                temperature=0.2,
                no_proxy=True,
            )
            content = msg.get("content") or "(empty response)"
            resolved_model = str((usage or {}).get("resolved_model") or model)
            prompt_tokens = (usage or {}).get("prompt_tokens", 0)
            completion_tokens = (usage or {}).get("completion_tokens", 0)
            cost = float((usage or {}).get("cost", 0) or 0)
            return {
                "model": resolved_model,
                "request_model": model,
                "text": content,
                "error": None,
                "tokens_in": prompt_tokens,
                "tokens_out": completion_tokens,
                "cost": cost,
            }
        except asyncio.TimeoutError:
            return {
                "model": model, "request_model": model,
                "text": "", "error": "Timeout after 120s",
                "tokens_in": 0, "tokens_out": 0,
            }
        except Exception as e:
            # Produce a human-readable error message that distinguishes the most
            # common failure modes, especially the hard-to-diagnose JSONDecodeError
            # that surfaces when a provider returns a non-JSON HTTP body (e.g. a
            # 413/429/500 error page) for an oversized prompt.
            error_msg = _classify_reviewer_error(e, model)
            return {
                "model": model, "request_model": model,
                "text": "", "error": error_msg,
                "tokens_in": 0, "tokens_out": 0,
            }


# ------------------------------------------------------------------ #
# Output formatting
# ------------------------------------------------------------------ #

def _format_output(raw_results: list, models: list, goal: str, estimated_tokens: int) -> str:
    """Render reviewer responses + aggregate verdict using majority-vote coordination.

    Aggregate rules:
    - `REVISE_PLAN` only when at least 2 reviewers independently return `REVISE_PLAN`.
      A lone dissenting `REVISE_PLAN` surfaces as `REVIEW_REQUIRED` with the dissent
      noted in the aggregate block — it is treated as a strong coordination signal,
      not an automatic block.
    - `REVIEW_REQUIRED` when at least one reviewer returns `REVIEW_REQUIRED` or
      `REVISE_PLAN` (minority), or had a non-substantive failure (error / empty /
      missing `AGGREGATE:` line). `GREEN` cannot be confirmed without a successful
      response from every reviewer.
    - `GREEN` only when every reviewer returned a parseable `AGGREGATE: GREEN`.
    """
    lines = [
        "## Plan Review Results",
        "",
        f"**Goal:** {goal}",
        f"**Models:** {len(models)} parallel reviewers",
        f"**Prompt size:** ~{estimated_tokens:,} tokens per reviewer",
        "",
        "---",
        "",
    ]

    # Per-reviewer categorisation: GREEN | REVIEW_REQUIRED | REVISE_PLAN | DEGRADED
    # DEGRADED covers error / empty / missing-aggregate-line (non-substantive failures).
    per_reviewer: list[str] = []

    for i, result in enumerate(raw_results):
        model_label = result.get("model") or result.get("request_model") or f"Model {i+1}"
        lines.append(f"### Reviewer {i+1}: {model_label}")
        lines.append("")

        if result.get("error"):
            lines.append(f"⚠️ **ERROR:** {result['error']}")
            lines.append("")
            per_reviewer.append("DEGRADED")
            continue

        text = result.get("text", "").strip()
        if not text:
            lines.append("⚠️ **ERROR:** Empty response from reviewer.")
            lines.append("")
            per_reviewer.append("DEGRADED")
            continue

        lines.append(text)
        lines.append("")

        reviewer_signal = _parse_aggregate_signal(text)
        if not reviewer_signal:
            # No parseable AGGREGATE: line — treat as degraded (non-substantive failure).
            per_reviewer.append("DEGRADED")
        elif reviewer_signal == "REVISE_PLAN":
            per_reviewer.append("REVISE_PLAN")
        elif reviewer_signal == "REVIEW_REQUIRED":
            per_reviewer.append("REVIEW_REQUIRED")
        else:
            # GREEN
            per_reviewer.append("GREEN")

        lines.append("---")
        lines.append("")

    # Majority-vote aggregation.
    revise_count = sum(1 for sig in per_reviewer if sig == "REVISE_PLAN")
    review_required_count = sum(1 for sig in per_reviewer if sig == "REVIEW_REQUIRED")
    degraded_count = sum(1 for sig in per_reviewer if sig == "DEGRADED")
    green_count = sum(1 for sig in per_reviewer if sig == "GREEN")

    # Explicit guard for the no-reviewer case. In normal operation
    # _run_plan_review_async always submits at least one reviewer, but
    # emitting zero per-reviewer counts in the aggregate block would look
    # misleadingly like all-zero clean PASS data rather than "no data at all".
    if not per_reviewer:
        lines.append("## Aggregate Signal")
        lines.append("")
        lines.append("❓ **REVIEW_REQUIRED**")
        lines.append("")
        lines.append("No reviewer responses were collected (empty reviewer list). "
                     "Treat as REVIEW_REQUIRED — re-run plan_task with at least one reviewer configured.")
        return "\n".join(lines)

    if revise_count >= 2:
        aggregate_signal = "REVISE_PLAN"
    elif revise_count == 1 or review_required_count > 0 or degraded_count > 0:
        aggregate_signal = "REVIEW_REQUIRED"
    elif green_count == len(per_reviewer):
        aggregate_signal = "GREEN"
    else:
        # Defensive fallback — no known signal variant, and neither GREEN nor
        # any failure/degradation was recorded. Should not occur given the
        # enumeration above, but a visible REVIEW_REQUIRED is safer than a
        # silent GREEN on anomalous bookkeeping.
        aggregate_signal = "REVIEW_REQUIRED"

    # Aggregate signal block
    signal_emoji = {
        "GREEN": "✅",
        "REVIEW_REQUIRED": "⚠️",
        "REVISE_PLAN": "❌",
    }.get(aggregate_signal, "❓")

    lines.append("## Aggregate Signal")
    lines.append("")
    lines.append(f"{signal_emoji} **{aggregate_signal}**")
    lines.append("")
    lines.append(
        f"Per-reviewer signals: REVISE_PLAN={revise_count}, "
        f"REVIEW_REQUIRED={review_required_count}, "
        f"GREEN={green_count}, DEGRADED={degraded_count}."
    )
    lines.append("")

    if aggregate_signal == "GREEN":
        lines.append(
            "All reviewers converged on GREEN. Read every reviewer's PROPOSALS "
            "section (they are the point of this call) and proceed with implementation."
        )
    elif aggregate_signal == "REVIEW_REQUIRED":
        reasons: list[str] = []
        if revise_count == 1:
            reasons.append(
                "one reviewer dissented with REVISE_PLAN while the others did not — "
                "a single dissent often sees the structural issue the others missed; "
                "read the dissenting reviewer's response in full before deciding"
            )
        if review_required_count > 0:
            reasons.append(
                f"{review_required_count} reviewer(s) raised RISKs or non-structural concerns"
            )
        if degraded_count > 0:
            reasons.append(
                f"{degraded_count} reviewer(s) failed to return a parseable response "
                "(error, empty, or missing AGGREGATE line) — GREEN cannot be confirmed"
            )
        if reasons:
            lines.append("Reason: " + "; ".join(reasons) + ".")
        lines.append(
            "Read every reviewer's full response and PROPOSALS section. "
            "Decide whether to adjust the plan before coding."
        )
    else:  # REVISE_PLAN
        lines.append(
            f"{revise_count} reviewers independently flagged REVISE_PLAN — majority "
            "confirms a structural problem with the plan. Redesign to address the "
            "flagged issues before writing any code."
        )

    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Prompt construction
# ------------------------------------------------------------------ #

def _build_system_prompt(
    checklist: str,
    bible_text: str,
    dev_md: str,
    arch_md: str,
) -> str:
    parts = [
        "You are a senior design reviewer for Ouroboros, a self-creating AI agent.",
        "Your job is to review a proposed implementation plan BEFORE any code is written.",
        "You are validating a concrete candidate plan, not brainstorming from zero. If the plan is weak, say exactly why and what boundary or contract was missed.",
        "You have full access to the entire codebase to find issues that the implementer may have missed.",
        "",
        "## Review stance — GENERATIVE, not audit",
        "",
        "Your primary job is to CONTRIBUTE ideas the implementer may not see, using full repo access.",
        "Finding defects in the plan is secondary; proposing concrete alternatives, surfacing existing",
        "surfaces that already solve the goal, and flagging subtle contract breaks is primary.",
        "Assume the implementer has already thought through the first-pass design — you are a design",
        "PARTNER who contributes, not an auditor who rubber-stamps.",
        "",
        "## Required output structure (follow exactly)",
        "",
        "1. **Your own approach** (1-2 sentences). State what YOU would do with full repo access:",
        "   the concrete alternative path, the existing file/function you would reuse, or the simpler route.",
        "   If after real effort you see no better approach, say so explicitly.",
        "2. **`## PROPOSALS` section** (top 1-2 ideas). Each proposal is one of:",
        "   - An existing function/module that already solves this (named exactly).",
        "   - A subtle contract break or shared-state interaction the plan likely missed.",
        "   - A simpler path with less surface area preserving the goal.",
        "   - A risk pattern visible from codebase history in your context.",
        "   - A BIBLE.md alignment issue with a specific principle cited.",
        "3. **Per-item verdicts**. For each checklist item below:",
        "   - **verdict**: PASS | RISK | FAIL",
        "   - **explanation**: 2-5 sentences describing what you found (or why it's fine)",
        "   - **concrete fix** (if RISK or FAIL): exact file, function, or line to address",
        "   - **alternative approaches** (if applicable): 1-2 more elegant solutions",
        "4. **Final line** (exactly one of):",
        "   - `AGGREGATE: GREEN` — no critical issues, implementer can proceed",
        "   - `AGGREGATE: REVIEW_REQUIRED` — risks or minor concerns, implementer should consider adjustments",
        "   - `AGGREGATE: REVISE_PLAN` — critical structural issues, plan must be revised before coding",
        "",
        "Be specific. Name exact files, functions, constants, or call sites.",
        "Vague concerns without a concrete pointer are advisory at most.",
        "If you see a simpler solution, say so directly — don't just hint.",
        "",
        "## Rules (what NOT to flag)",
        "",
        "- Do NOT mark RISK on `minimalism` just because you would have done it differently.",
        "  Flag RISK only when you can name (a) fewer files touched, (b) fewer lines changed,",
        "  or (c) reuse of a specific existing surface — concrete alternative, not taste.",
        "- Do NOT penalise missing tests, `VERSION` bumps, `README.md` changelog rows, or",
        "  `docs/ARCHITECTURE.md` updates — the plan has no code yet. Focus on design correctness",
        "  and elegance, not commit hygiene. Commit-gate reviewers handle that later.",
        "",
        "## Aggregate level — majority-vote coordination across 2-3 distinct reviewers",
        "",
        "- `AGGREGATE: REVISE_PLAN` should be used ONLY when you are confident the plan has a",
        "  concrete structural problem that warrants a redesign. The coordinator escalates to final",
        "  `REVISE_PLAN` only when at least 2 distinct reviewers independently flag it — a lone",
        "  dissenting `REVISE_PLAN` will surface as `REVIEW_REQUIRED` with your dissent noted",
        "  (with 2-reviewer setups, \"≥2 reviewers\" means both reviewers agreed). This is",
        "  deliberate: `plan_review` is a coordinative signal, not a block. Use `REVIEW_REQUIRED`",
        "  for real but non-structural risks; reserve `REVISE_PLAN` for defects worth blocking the",
        "  plan on.",
        "",
        "---",
        "",
    ]

    if checklist:
        parts += [
            "## Plan Review Checklist",
            "",
            checklist,
            "",
            "---",
            "",
        ]

    if bible_text:
        parts += [
            "## BIBLE.md (Constitution — highest priority)",
            "",
            bible_text,
            "",
            "---",
            "",
        ]

    if dev_md:
        parts += [
            "## DEVELOPMENT.md (Engineering handbook)",
            "",
            dev_md,
            "",
            "---",
            "",
        ]

    if arch_md:
        parts += [
            "## ARCHITECTURE.md (Current system structure)",
            "",
            arch_md,
            "",
            "---",
            "",
        ]

    return "\n".join(parts)


def _build_user_content(
    plan: str,
    goal: str,
    files_to_touch: list,
    head_snapshots: str,
    repo_pack: str,
    omitted_note: str,
) -> str:
    parts = [
        "## Implementation Plan Under Review",
        "",
        f"**Goal:** {goal}",
        "",
        "**Proposed Plan:**",
        plan,
        "",
    ]

    if files_to_touch:
        parts += [
            f"**Files planned to touch:** {', '.join(files_to_touch)}",
            "",
        ]

    if head_snapshots:
        parts += [
            "## Current State of Planned-Touch Files (HEAD)",
            "",
            head_snapshots,
            "",
        ]

    if repo_pack:
        parts += [
            "## Full Repository Code (for cross-module analysis)",
            "",
            repo_pack,
        ]

    if omitted_note:
        parts.append(omitted_note)

    return "\n".join(parts)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _classify_reviewer_error(exc: BaseException, model: str) -> str:
    """Return a human-readable error string for a reviewer failure.

    Distinguishes common failure modes so the agent can act on the error
    rather than staring at a raw ``JSONDecodeError`` or a cryptic SDK string.

    Categories:
    - Oversized prompt (JSONDecodeError / json.decoder.JSONDecodeError):
      Providers like OpenRouter return an HTML or plain-text error page when
      the prompt is too large.  The OpenAI SDK tries to ``json.loads`` that
      response body and raises JSONDecodeError.  The root cause is the prompt
      size, not a JSON formatting problem.
    - Rate limit / quota: 429 responses from the provider.
    - Bad request: 400 from the provider (often prompt too large for that model).
    - API connection error: network-level failure.
    - Fallback: full repr so nothing is silently swallowed.
    """
    import json

    exc_type = type(exc).__name__
    exc_str = str(exc)

    # JSONDecodeError: almost always "provider returned non-JSON error body".
    if isinstance(exc, json.JSONDecodeError):
        return (
            f"API error (provider returned non-JSON response body — likely oversized prompt "
            f"or HTTP error from {model}): {exc_str}"
        )

    # OpenAI SDK APIError hierarchy — import lazily so the module still loads
    # even if openai is not installed.
    try:
        from openai import (
            APIConnectionError,
            APIStatusError,
            BadRequestError,
            RateLimitError,
        )
        if isinstance(exc, RateLimitError):
            return f"Rate limit / quota exceeded for {model} (HTTP 429): {exc_str}"
        if isinstance(exc, BadRequestError):
            return (
                f"Bad request for {model} (HTTP 400 — prompt may be too large "
                f"for this model's context window): {exc_str}"
            )
        if isinstance(exc, APIConnectionError):
            return f"API connection error for {model} (network failure): {exc_str}"
        if isinstance(exc, APIStatusError):
            status = getattr(exc, "status_code", "?")
            return f"API status error {status} for {model}: {exc_str}"
    except ImportError:
        pass

    # Catch-all: preserve full repr for unknown exception types.
    return f"{exc_type}: {exc_str}"


def _parse_aggregate_signal(text: str) -> str:
    """Extract the aggregate signal from a reviewer's response.

    Parses lines matching ``AGGREGATE: <SIGNAL>`` (case-insensitive, optional
    leading whitespace) and returns the LAST valid match.  Using the last match
    means self-corrections or earlier example lines do not override the final
    verdict the reviewer actually intended.

    Returns one of "GREEN", "REVIEW_REQUIRED", "REVISE_PLAN", or "" if no
    valid aggregate line is found.

    Narrow regex prevents false positives when a reviewer discusses signal
    names in the explanatory body of their response.
    """
    import re
    pattern = re.compile(
        r"^\s*AGGREGATE\s*:\s*(GREEN|REVIEW_REQUIRED|REVISE_PLAN)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = pattern.findall(text)
    if matches:
        return matches[-1].upper()  # use the last match — final reviewer verdict
    return ""


def _get_review_models() -> list[str]:
    """Return exactly 3 reviewer models for the plan review.

    Delegates to ``ouroboros.config.get_review_models`` — the single source of
    truth that the commit triad also uses. This keeps plan_review and the
    commit triad in lockstep, including the direct-provider normalization
    logic (OpenAI-only / Anthropic-only fallback to main model × N).

    Normalizes to exactly 3 reviewers so the docs' promise of '3 parallel
    reviewers' is always honoured: pads with the last model if fewer than 3
    are configured; caps at 3 if more are configured.
    """
    from ouroboros import config as _cfg

    models = list(_cfg.get_review_models() or [])
    if not models:
        main = os.environ.get("OUROBOROS_MODEL", "anthropic/claude-opus-4.7")
        models = [main]

    # Pad to exactly 3 by repeating the last model if needed
    while len(models) < 3:
        models.append(models[-1])

    return models[:3]  # cap at 3


def _load_plan_checklist() -> str:
    """Load the Plan Review Checklist section from CHECKLISTS.md."""
    try:
        return load_checklist_section("Plan Review Checklist")
    except Exception as e:
        log.warning("Could not load Plan Review Checklist: %s", e)
        return ""


def _load_bible(repo_dir: Path) -> str:
    """Load BIBLE.md.

    Returns file contents on success. On failure, returns an explicit omission
    note so the reviewer knows that the Constitution is missing from context
    rather than silently receiving an empty string.
    """
    p = repo_dir / "BIBLE.md"
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8")
        return f"[⚠️ OMISSION: BIBLE.md not found at {p}]"
    except Exception as e:
        return f"[⚠️ OMISSION: BIBLE.md could not be loaded ({p}): {e}]"


def _load_doc(repo_dir: Path, rel_path: str) -> str:
    """Load a documentation file relative to the repo root.

    Returns file contents on success. On failure, returns an explicit omission
    note so the reviewer knows that context is missing rather than silently
    receiving an empty string.
    """
    p = repo_dir / rel_path
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8")
        return f"[⚠️ OMISSION: {rel_path} not found at {p}]"
    except Exception as e:
        return f"[⚠️ OMISSION: {rel_path} could not be loaded ({p}): {e}]"
