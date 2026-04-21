"""Claude Code advisory pre-review gate.

Runs a read-only Claude Code review of the current worktree BEFORE the unified
multi-model pre-commit review. Advisory findings are non-blocking by themselves;
only the *absence* of a fresh matching advisory run blocks repo_commit.

Correct workflow:
  1. Finish ALL edits first
  2. advisory_pre_review(commit_message="...")   ← run AFTER all edits are done
  3. repo_commit(commit_message="...")           ← run IMMEDIATELY after advisory

⚠️ Any edit (repo_write / str_replace_editor) after step 2 automatically marks
   the advisory as stale — you must re-run advisory_pre_review before repo_commit.

Tool surface:
  advisory_pre_review   run a fresh advisory review
  review_status         show advisory history, open obligations, staleness state
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import subprocess
from typing import List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.review_state import (
    AdvisoryRunRecord,
    AdvisoryReviewState,
    compute_snapshot_hash,
    format_status_section,
    load_state,
    make_repo_key,
    save_state,
    update_state,
    _utc_now,
)
from ouroboros.tools.review_helpers import (
    build_advisory_changed_context,
    build_blocking_findings_json_section,
    load_checklist_section,
    build_goal_section,
    build_scope_section,
    check_worktree_readiness,
    check_worktree_version_sync as _check_worktree_version_sync_shared,
    CRITICAL_FINDING_CALIBRATION,
    get_advisory_runtime_diagnostics as _get_runtime_diagnostics,
    format_advisory_sdk_error as _format_advisory_error,
    normalize_reviewer_obligation_id,
    strip_obligation_suffix,
    _ANTI_THRASHING_RULE_VERDICT,
    _ANTI_THRASHING_RULE_ITEM_NAME,
    _HISTORY_VERIFICATION_ONLY_RULE,
    _run_review_preflight_tests,
)
from ouroboros.utils import (
    append_jsonl,
    utc_now_iso,
    truncate_review_artifact as _truncate_review_artifact,
    truncate_review_reason as _truncate_review_reason,
)

log = logging.getLogger(__name__)

_MAX_DIFF_CHARS_ERROR = 500_000  # Fail loudly above this — split the commit


def _emit_advisory_usage(
    ctx: "ToolContext",
    model: str,
    cost_usd: float,
    usage: dict,
    source: str = "advisory",
    provider: str = "anthropic",
) -> None:
    """Emit an llm_usage event so advisory and fallback LLM costs reach the budget.

    Uses the same routing as triad/scope review: event_queue first, pending_events
    fallback, so costs are tracked regardless of execution context.

    ``provider`` should be "anthropic" for SDK calls (always billed there) and
    the real routing provider (e.g., "openrouter") for fallback LLM calls that
    go through the shared LLMClient — otherwise /api/cost-breakdown attribution
    will be wrong.
    """
    try:
        from ouroboros.pricing import infer_api_key_type, infer_model_category
        from ouroboros.utils import utc_now_iso as _utc
        event = {
            "type": "llm_usage",
            "ts": _utc(),
            "task_id": getattr(ctx, "task_id", "") or "",
            "model": model,
            "api_key_type": infer_api_key_type(model, provider),
            "model_category": infer_model_category(model),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "cached_tokens": usage.get("cached_tokens", 0),
                "cost": cost_usd or usage.get("cost", 0),
            },
            "provider": provider,
            "source": source,
            "category": "review",
        }
        eq = getattr(ctx, "event_queue", None)
        if eq is not None:
            try:
                eq.put_nowait(event)
                return
            except Exception:
                pass
        pending = getattr(ctx, "pending_events", None)
        if pending is not None:
            pending.append(event)
    except Exception:
        log.debug("_emit_advisory_usage failed (non-critical)", exc_info=True)


_ADVISORY_PROMPT_MAX_CHARS = 1_600_000  # ~400K tokens; non-blocking skip when exceeded
def _load_doc(repo_dir: pathlib.Path, relpath: str, fallback: str = "") -> str:
    try:
        p = repo_dir / relpath
        if p.is_file():
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return fallback


def _get_staged_diff(
    repo_dir: pathlib.Path,
    paths: list[str] | None = None,
) -> str:
    """Return staged+unstaged diff (full, no truncation), scoped to ``paths`` when given."""
    try:
        path_args = (["--"] + list(paths)) if paths else []
        staged_result = subprocess.run(
            ["git", "diff", "--cached"] + path_args,
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        )
        if staged_result.returncode != 0:
            err = (staged_result.stderr or "").strip()[:200]
            return (
                f"⚠️ ADVISORY_ERROR: git diff --cached exited {staged_result.returncode}: {err}"
            )
        unstaged_result = subprocess.run(
            ["git", "diff"] + path_args,
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        )
        if unstaged_result.returncode != 0:
            err = (unstaged_result.stderr or "").strip()[:200]
            return (
                f"⚠️ ADVISORY_ERROR: git diff exited {unstaged_result.returncode}: {err}"
            )
        combined = ((staged_result.stdout or "") + (unstaged_result.stdout or "")).strip()
        if len(combined) > _MAX_DIFF_CHARS_ERROR:
            return (
                f"⚠️ ADVISORY_ERROR: staged diff is too large ({len(combined):,} chars). "
                "Split the commit into smaller pieces."
            )
        return combined or "(no unstaged/staged changes found)"
    except Exception as exc:
        return f"⚠️ ADVISORY_ERROR: failed to retrieve diff: {exc}"


def _get_changed_file_list(
    repo_dir: pathlib.Path,
    paths: list[str] | None = None,
) -> str:
    """Return porcelain status, optionally scoped to ``paths``."""
    try:
        path_args = (["--"] + list(paths)) if paths else []
        result = subprocess.run(
            ["git", "status", "--porcelain"] + path_args,
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            err = (result.stderr or "").strip()[:200]
            return f"⚠️ ADVISORY_ERROR: git status exited {result.returncode}: {err}"
        lines = [line.rstrip() for line in result.stdout.splitlines() if line.strip()]
        return "\n".join(lines) if lines else "(clean — no changed files)"
    except Exception as exc:
        return f"⚠️ ADVISORY_ERROR: git status error: {exc}"


def _build_blocking_history_section(drive_root: pathlib.Path, repo_key: str = "") -> str:
    """Build section summarizing unresolved obligations from blocking rounds."""
    try:
        state = load_state(drive_root)
    except Exception:
        return ""

    return build_blocking_findings_json_section(
        state.get_open_obligations(repo_key=repo_key),
        state.get_blocking_history(repo_key=repo_key),
    )


def _build_advisory_prompt(
    repo_dir: pathlib.Path,
    commit_message: str,
    goal: str = "",
    scope: str = "",
    resolved_paths: Optional[List[str]] = None,
    drive_root: Optional[pathlib.Path] = None,
    diff: Optional[str] = None,
    changed_files: Optional[str] = None,
    touched_pack: str = "",
    omitted_paths: Optional[List[str]] = None,
) -> str:
    """Build the read-only advisory review prompt (BIBLE, checklists, dev guide, diff, touched pack)."""
    bible = _load_doc(repo_dir, "BIBLE.md", "(BIBLE.md not found)")
    try:
        checklists = load_checklist_section("Repo Commit Checklist")
    except Exception:
        checklists = _load_doc(repo_dir, "docs/CHECKLISTS.md", "(CHECKLISTS.md not found)")
    dev_guide = _load_doc(repo_dir, "docs/DEVELOPMENT.md", "(DEVELOPMENT.md not found)")
    arch_doc = _load_doc(repo_dir, "docs/ARCHITECTURE.md", "(ARCHITECTURE.md not found)")
    if diff is None:
        diff = _get_staged_diff(repo_dir, paths=resolved_paths)
    if changed_files is None:
        changed_files = _get_changed_file_list(repo_dir, paths=resolved_paths)
    goal_section = build_goal_section(goal, scope, commit_message)
    scope_section = build_scope_section(scope)

    # Build blocking history section if drive_root is available
    blocking_history = ""
    if drive_root:
        blocking_history = _build_blocking_history_section(
            drive_root,
            make_repo_key(repo_dir),
        )

    omitted_note = ""
    if omitted_paths:
        preview = ", ".join(list(omitted_paths)[:5])
        if len(omitted_paths) > 5:
            preview += f", +{len(omitted_paths) - 5} more"
        omitted_note = (
            f"\n*(Inline pack contains omission notes for {len(omitted_paths)} path(s): {preview})*\n"
        )

    critical_calibration = CRITICAL_FINDING_CALIBRATION  # noqa: F841 — used in f-string below

    prompt = f"""\
You are performing a pre-commit review of an Ouroboros self-modifying AI agent codebase.

## Your role — NON-NEGOTIABLE REQUIREMENTS
- Review the current working tree changes with the SAME RIGOR as the downstream blocking reviewers.
  A false PASS here wastes an entire blocking review cycle ($10+).
- Use ONLY Read, Grep, Glob tools. Do NOT edit or execute any files.
- Read the FULL CONTENT of every changed file listed below using the Read tool.
  Do NOT evaluate security, bible compliance, or code quality from path listings or diff hunks alone.
- Return ONLY a JSON array. No prose, no markdown fences — only the JSON array.

## Thoroughness requirements
- Do NOT stop after finding the first issue. Check EVERY item in the checklist.
- Report ALL problems you find. If there are 5 bugs, list all 5 — each as a separate entry.
- Do NOT summarize multiple distinct problems into one finding.
- For PASS: brief reason is fine. For FAIL: cite the specific file, line/symbol, what is wrong,
  and provide a CONCRETE fix suggestion so the developer knows exactly what to change.

## Severity thresholds — treat as blocking reviewers do
- bible_compliance (item 1): ANY violation of BIBLE.md principles is CRITICAL.
- security_issues (item 5): ANY path traversal, secret leakage, or unsafe operation is CRITICAL.
- development_compliance (item 2): naming, entity type rules, module size, no ad-hoc LLM calls,
  no hardcoded [:N] truncation of cognitive artifacts — all CRITICAL when violated.
- self_consistency (item 13): if a concrete stale artifact exists (specific file + line), CRITICAL.
- Narrative/descriptive mismatches (for example README test counts or descriptive "N fixes" summaries)
  are ADVISORY unless they change release metadata, actual runtime behavior, safety guidance,
  or instructions a user/reviewer must rely on to use the changed feature correctly.

## Critical finding calibration (shared with triad and scope reviewers)

{critical_calibration}

## Output format
Return ONLY a JSON array. Each element:
{{
  "item": "<checklist item name>",
  "verdict": "PASS" | "FAIL",
  "severity": "critical" | "advisory",
  "reason": "<for FAIL: file, line/symbol, what is wrong, how to fix>"
}}

## CHECKLISTS.md (What to review)

{checklists}

{scope_section}

{goal_section}

## DEVELOPMENT.md (Engineering standards)

{dev_guide}

## BIBLE.md (Constitutional context — top priority)

{bible}

## ARCHITECTURE.md (System structure — critical for version sync and module checks)

{arch_doc}

{blocking_history}

## Commit message

{commit_message}

## Changed files (git status --porcelain)

{changed_files}

## Current touched files (full content — read these with the Read tool for deeper inspection)

{touched_pack}
{omitted_note}

## Staged diff

{diff}

## Step-by-step instructions
1. Read the FULL content of every changed file using the Read tool. Do not skip any file.
2. Check EVERY item from the "Repo Commit Checklist" — do not stop after the first issue.
3. Pay equal attention to EVERY checklist item listed below — do not favour early items.
   bible_compliance and security_issues must be evaluated at the same strictness as the
   downstream blocking reviewers.
4. Look for ALL bugs, logic errors, regressions, race conditions, and violations of BIBLE.md or DEVELOPMENT.md.
5. Cross-check: do tool descriptions in prompts match actual get_tools() exports?
   Does ARCHITECTURE.md header version match the VERSION file?
5a. **ALWAYS — Verdict and item-name discipline (applies unconditionally, even when no obligations exist):**
   - **VERDICT IS AUTHORITATIVE:** {_ANTI_THRASHING_RULE_VERDICT}
   - **DO NOT REPHRASE:** {_ANTI_THRASHING_RULE_ITEM_NAME}
6. **MANDATORY — Prior obligations:** If an "Unresolved obligations" section appears above,
   address EVERY listed obligation explicitly in your output:
   a. Include a separate JSON entry per obligation for the corresponding checklist item.
   b. If fixed: verdict=PASS, reason must state WHAT closes it (file, line, symbol, change).
   c. If not fixed: verdict=FAIL, severity=critical, reason must name the specific stale artifact.
   d. **TARGETING — multiple obligations with the same checklist item:**
      When two or more open obligations share the same item (e.g. two distinct `code_quality`
      findings), you MUST emit a separate JSON entry for EACH one and use the
      `(obligation <id>)` suffix in the `"item"` field to target it precisely:
        {{"item": "code_quality (obligation obl-0001)", "verdict": "PASS", ...}}
      A generic `"item": "code_quality"` entry when multiple same-item obligations are
      open will NOT resolve all of them — only the one matched by `obligation_id` will
      be closed; the rest remain open until explicitly addressed.
   e. You MAY also provide the stable `obligation_id` explicitly as a top-level JSON field.
      If both the suffix and the field are present, they must match.
   f. **VERDICT IS AUTHORITATIVE:** {_ANTI_THRASHING_RULE_VERDICT}
   g. **DO NOT REPHRASE:** {_ANTI_THRASHING_RULE_ITEM_NAME}
   h. **VERIFICATION ONLY:** {_HISTORY_VERIFICATION_ONLY_RULE}
7. Output ONLY the JSON array — no markdown fences, no commentary outside the JSON.
"""
    return prompt


_FALLBACK_EXTRACT_PROMPT = """\
The following text is the output of an advisory code review. It may contain narrative
reasoning, tool call traces, and a JSON checklist array. Extract ONLY the JSON checklist
array from this text and return it as a valid JSON array. No prose, no markdown fences.

Each element MUST have ALL of these required fields:
  "item":     checklist item name (string)
  "verdict":  "PASS" or "FAIL" (string)
  "severity": "critical" or "advisory" (string, REQUIRED — do not omit even for PASS entries)
  "reason":   brief explanation (string)

Optional field:
  "obligation_id": stable id for a previously surfaced obligation

If a FAIL entry in the source is missing a severity, infer it from context:
treat it as "critical" if it involves bugs, security, or constitutional violations,
otherwise "advisory".

If no valid checklist array exists in the text, return an empty JSON array: []

Advisory review output to extract from:
{raw_text}
"""

_FALLBACK_HEAD_CHARS = 4_000   # first N chars of raw text (context / tool-call traces)
_FALLBACK_TAIL_CHARS = 60_000  # last N chars — where the JSON array usually appears
_FALLBACK_OMISSION_NOTE = (
    "\n\n[⚠️ OMISSION NOTE: middle section of advisory output omitted "
    "to fit context window — JSON findings are expected in the tail section above]\n\n"
)


def _build_fallback_window(raw_text: str) -> str:
    """Build a head+tail window of raw_text for the LLM extraction fallback.

    The known failure pattern is: Claude writes a long narrative preamble + tool
    call traces, then places the JSON checklist array NEAR THE END.  A first-N
    truncation would discard the JSON.  We keep the first _FALLBACK_HEAD_CHARS
    (for context) and the last _FALLBACK_TAIL_CHARS (where JSON lives), with an
    explicit omission note for the middle section.
    """
    total = _FALLBACK_HEAD_CHARS + _FALLBACK_TAIL_CHARS
    if len(raw_text) <= total:
        return raw_text
    head = raw_text[:_FALLBACK_HEAD_CHARS]
    tail = raw_text[-_FALLBACK_TAIL_CHARS:]
    return head + _FALLBACK_OMISSION_NOTE + tail


def _resolve_fallback_model() -> str:
    """Resolve the light extraction model for the LLM-first advisory fallback.

    Uses OUROBOROS_MODEL_LIGHT (user-configured light model) if set, otherwise
    falls back to the system default from config.  Never hardcodes a specific
    model ID — all model selection is delegated to configuration (P3 LLM-First).
    """
    import os as _os
    from ouroboros.config import SETTINGS_DEFAULTS  # type: ignore[attr-defined]
    env_light = (_os.environ.get("OUROBOROS_MODEL_LIGHT") or "").strip()
    return env_light or str(SETTINGS_DEFAULTS.get("OUROBOROS_MODEL_LIGHT", ""))


def _llm_extract_advisory_items(raw_text: str, ctx: object) -> list:
    """LLM-first fallback: extract advisory checklist items from narrative text.

    Called when _parse_advisory_output() returns [] but we have non-empty raw output.
    Uses the light model via llm.py with no_proxy=True (fork-safe for macOS workers).

    Sends a head+tail window of raw_text so that the JSON array near the end of a
    long narrative response is always included even when the total text exceeds the
    combined head+tail budget.

    Returns a list of checklist item dicts, or [] on any failure.
    """
    try:
        from ouroboros.llm import LLMClient  # type: ignore[attr-defined]

        light_model = _resolve_fallback_model()
        input_text = _build_fallback_window(raw_text)
        prompt = _FALLBACK_EXTRACT_PROMPT.format(raw_text=input_text)
        messages = [{"role": "user", "content": prompt}]

        llm = LLMClient()
        response, fallback_usage = llm.chat(
            messages=messages,
            model=light_model,
            max_tokens=8192,
            reasoning_effort="low",
            no_proxy=True,
        )

        # Track fallback LLM cost — this is real spend even if it's a cheap call.
        # Derive provider from the model prefix for correct cost-breakdown attribution.
        if fallback_usage and isinstance(ctx, ToolContext):
            fallback_cost = float((fallback_usage or {}).get("cost", 0) or 0)
            from ouroboros.pricing import infer_provider_from_model as _infer_prov
            _emit_advisory_usage(
                ctx, light_model, fallback_cost, fallback_usage,
                "advisory_fallback", provider=_infer_prov(light_model),
            )

        content = response.get("content", "")
        if not isinstance(content, str):
            # Flatten list content blocks
            if isinstance(content, list):
                content = " ".join(
                    str(b.get("text", "")) for b in content if isinstance(b, dict)
                )
            else:
                content = str(content or "")

        items = _parse_advisory_output(content)
        if not _is_checklist_array(items):
            return []

        # Normalise: any FAIL item missing 'severity' gets "critical" so that
        # _handle_advisory_pre_review() never silently downgrade a blocking finding.
        normalised = []
        for it in items:
            if not isinstance(it, dict):
                continue
            verdict = str(it.get("verdict", "")).upper().strip()
            if verdict == "FAIL" and not str(it.get("severity", "")).strip():
                it = dict(it)
                it["severity"] = "critical"
            normalised.append(it)
        return normalised

    except Exception as exc:
        log.warning("Advisory LLM fallback extraction failed: %s", exc)
        return []


def _syntax_preflight_staged_py_files(
    repo_dir: pathlib.Path,
    resolved_paths: List[str],
) -> Optional[str]:
    """Compile each staged `.py` file in-process to catch SyntaxErrors before
    the Claude SDK advisory call.

    Purpose: the SDK call costs ~$1-2 and several minutes; it is wasteful to
    run it when a staged `.py` file would not even compile. Parsing is done via
    `compile(source, path, "exec", dont_inherit=True)` — no `__pycache__` is
    produced and no subprocess is started.

    Returns None when every staged `.py` file compiles cleanly (or none exist).
    Returns a formatted `PREFLIGHT_BLOCKED` message when one or more fail.

    Non-agent-repo skip: if the target repo does not contain `ouroboros/__init__.py`
    (i.e. we are not reviewing our own repo), skip the gate. Target Python
    version can differ from ours and we do not want to block on that.
    """
    if not (repo_dir / "ouroboros" / "__init__.py").exists():
        return None

    errors: List[str] = []
    for rel in resolved_paths:
        if not rel.endswith(".py"):
            continue
        file_path = repo_dir / rel
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except OSError:
            continue
        try:
            compile(source, rel, "exec", dont_inherit=True)
        except SyntaxError as exc:
            line = getattr(exc, "lineno", None) or "?"
            msg = getattr(exc, "msg", None) or str(exc)
            errors.append(f"{rel}:{line}: {msg}")
        except ValueError as exc:
            # `compile` raises ValueError (not SyntaxError) when the source
            # contains null bytes or other non-printable bytes that the
            # tokenizer rejects before parsing. Treat these the same as a
            # syntax error so the SDK call is still skipped and the agent
            # gets an actionable PREFLIGHT_BLOCKED message instead of an
            # opaque ADVISORY_ERROR.
            errors.append(f"{rel}:?: {exc}")

    if not errors:
        return None

    return (
        "⚠️ PREFLIGHT_BLOCKED: syntax errors:\n"
        + "\n".join(f"- {err}" for err in errors)
        + "\n\nFix the syntax error(s) above and re-run advisory_pre_review. "
        "Claude SDK advisory was skipped to save budget."
    )


def _run_claude_advisory(
    repo_dir: pathlib.Path,
    commit_message: str,
    ctx: ToolContext,
    goal: str = "",
    scope: str = "",
    paths: Optional[List[str]] = None,
    drive_root: Optional[pathlib.Path] = None,
) -> tuple:
    """Run the advisory review via Claude Agent SDK (read-only).

    Returns (items, raw_result, model_used, prompt_chars).
    raw_result starts with ⚠️ ADVISORY_ERROR: on failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return [], "⚠️ ADVISORY_ERROR: ANTHROPIC_API_KEY not set.", "", 0

    # Resolve model — single source of truth, honours CLAUDE_CODE_MODEL setting
    from ouroboros.gateways.claude_code import resolve_claude_code_model
    model = resolve_claude_code_model()

    # Fetch diff and changed-file list exactly once, validate, then pass into prompt builder.
    diff_text = _get_staged_diff(repo_dir, paths=paths)
    if diff_text.startswith("⚠️ ADVISORY_ERROR:"):
        return [], diff_text, "", 0

    changed_files_text = _get_changed_file_list(repo_dir, paths=paths)
    if changed_files_text.startswith("⚠️ ADVISORY_ERROR:"):
        return [], changed_files_text, "", 0

    # Parse touched paths from porcelain output to avoid a second git-status call inside
    # build_touched_file_pack.  Lines are "XY filename" or "(clean — no changed files)".
    try:
        always_inlined = {"docs/ARCHITECTURE.md"}
        resolved_paths, touched_pack, omitted_paths = build_advisory_changed_context(
            repo_dir,
            changed_files_text=changed_files_text,
            paths=paths,
            exclude_paths=always_inlined,
        )

        preflight_err = _syntax_preflight_staged_py_files(repo_dir, resolved_paths)
        if preflight_err:
            log.warning("Advisory skipped — syntax preflight blocked: %s", preflight_err.splitlines()[0])
            return [], preflight_err, "", 0

        prompt = _build_advisory_prompt(
            repo_dir,
            commit_message,
            goal=goal,
            scope=scope,
            resolved_paths=resolved_paths,
            drive_root=drive_root,
            diff=diff_text,
            changed_files=changed_files_text,
            touched_pack=touched_pack,
            omitted_paths=omitted_paths,
        )
    except RuntimeError as exc:
        return [], f"⚠️ ADVISORY_ERROR: failed to build advisory prompt: {exc}", "", 0
    except Exception as exc:
        return [], f"⚠️ ADVISORY_ERROR: unexpected error building prompt: {exc}", "", 0

    prompt_chars = len(prompt)
    diag = _get_runtime_diagnostics(model, prompt_chars, resolved_paths)

    # Budget gate: non-blocking skip when prompt too large (mirrors scope review)
    if prompt_chars > _ADVISORY_PROMPT_MAX_CHARS:
        tokens_approx = max(1, prompt_chars // 4)
        warning = (
            f"⚠️ ADVISORY_SKIPPED: advisory prompt too large "
            f"({prompt_chars:,} chars, ~{tokens_approx:,} tokens > "
            f"{_ADVISORY_PROMPT_MAX_CHARS:,} char limit). "
            f"Advisory review skipped — non-blocking. Consider splitting the commit."
        )
        log.warning("Advisory skipped — prompt too large: %d chars", prompt_chars)
        return [], warning, model, prompt_chars

    log.info(
        "Advisory SDK call: model=%s prompt_chars=%d touched=%s sdk=%s cli=%s",
        diag["model"], diag["prompt_chars"], diag["touched_paths"],
        diag["sdk_version"], diag["cli_version"],
    )

    try:
        from ouroboros.gateways.claude_code import (
            DEFAULT_CLAUDE_CODE_MAX_TURNS,
            run_readonly,
        )

        result = run_readonly(
            prompt=prompt,
            cwd=str(repo_dir),
            model=model,
            max_turns=DEFAULT_CLAUDE_CODE_MAX_TURNS,
        )

        if not result.success:
            err_msg = _format_advisory_error(
                prefix="SDK/CLI returned failure",
                result_error=result.error,
                stderr_tail=result.stderr_tail,
                session_id=result.session_id,
                diag=diag,
            )
            log.error("Advisory SDK failure:\n%s", err_msg)
            return [], err_msg, model, prompt_chars

        raw_text = result.result_text

        # Track SDK cost — advisory calls are real spend that must reach the budget.
        if result.cost_usd > 0:
            _emit_advisory_usage(ctx, model, result.cost_usd, result.usage or {}, "advisory_sdk")

        items = _parse_advisory_output(raw_text)

        # LLM-first fallback: if structural parse failed but we have raw output,
        # ask a light model to extract the JSON array from the narrative response.
        # This handles the "Claude writes findings at the end of a long narrative"
        # pattern that causes parse_failure on large diffs (confirmed root cause).
        if not items and raw_text and not raw_text.startswith("⚠️ ADVISORY_ERROR"):
            items = _llm_extract_advisory_items(raw_text, ctx)
            if items:
                log.info("Advisory: structural parse failed, LLM fallback extracted %d items", len(items))

        return items, raw_text, model, prompt_chars

    except ImportError:
        return [], (
            "⚠️ ADVISORY_ERROR: claude-agent-sdk not installed. "
            "Install: pip install 'ouroboros[claude-sdk]'"
        ), "", 0
    except Exception as e:
        err_msg = _format_advisory_error(
            prefix=f"SDK call raised {type(e).__name__}",
            result_error=str(e),
            stderr_tail="",
            session_id="",
            diag=diag,
        )
        log.error("Advisory SDK exception:\n%s", err_msg)
        return [], err_msg, model, prompt_chars


def _parse_advisory_output(stdout: str) -> list:
    """Extract the JSON findings array from Claude CLI output."""
    # Try direct parse first
    text = stdout.strip()

    # Unwrap Claude Code JSON envelope: {"result": "...", ...}
    try:
        outer = json.loads(text)
        if isinstance(outer, dict) and "result" in outer:
            text = str(outer["result"]).strip()
        elif isinstance(outer, list) and _is_checklist_array(outer):
            return outer
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    # Try direct parse of the inner result
    try:
        obj = json.loads(text)
        if isinstance(obj, list) and _is_checklist_array(obj):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Find embedded JSON array — scan all "]" positions from right to left and
    # for each try all "[" positions to its left, also right to left.
    # This correctly handles: stray arrays appearing AFTER the real checklist
    # (we find the checklist's "]" before the stray one), and code blocks with
    # brackets appearing BEFORE the checklist (their "[" is left of the
    # checklist's "[" so the inner-rightmost match wins).
    # Each candidate must also pass _is_checklist_array validation so that stray
    # arrays like [1,2,3] or code-snippet arrays are rejected.
    ends: list[int] = []
    search_from = 0
    while True:
        pos = text.find("]", search_from)
        if pos == -1:
            break
        ends.append(pos)
        search_from = pos + 1

    for end in reversed(ends):
        # Collect all "[" positions to the left of this "]"
        search_from = 0
        starts: list[int] = []
        while True:
            pos = text.find("[", search_from)
            if pos == -1 or pos > end:
                break
            starts.append(pos)
            search_from = pos + 1
        for start in reversed(starts):
            try:
                obj = json.loads(text[start:end + 1])
                if isinstance(obj, list) and _is_checklist_array(obj):
                    return obj
            except (json.JSONDecodeError, ValueError):
                continue

    return []


def _is_checklist_array(items: list) -> bool:
    """Return True iff items looks like a real advisory checklist array.

    Each element must be a dict containing at least 'item' and 'verdict' keys.
    An empty list is rejected (no findings = parse_failure, not a clean advisory).
    Stray arrays like [1,2,3], code snippets, or unrelated JSON lists are rejected.
    """
    if not items:
        return False
    return all(
        isinstance(el, dict) and "item" in el and "verdict" in el
        for el in items
    )


# -- Audit logging --

def _audit_bypass(ctx: ToolContext, snapshot_hash: str, commit_message: str,
                  bypass_reason: str, task_id: str) -> None:
    try:
        append_jsonl(ctx.drive_logs() / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "advisory_pre_review_bypassed",
            "snapshot_hash": snapshot_hash,
            "commit_message": commit_message,  # full — no [:200] truncation
            "bypass_reason": bypass_reason,
            "task_id": task_id,
        })
    except Exception:
        pass


def _record_bypass(ctx: ToolContext, state: "AdvisoryReviewState", snapshot_hash: str,
                   commit_message: str, reason: str, task_id: str,
                   drive_root: pathlib.Path,
                   snapshot_paths: Optional[List[str]] = None) -> str:
    """Audit, record, and save a bypassed advisory run. Returns JSON response."""
    _audit_bypass(ctx, snapshot_hash, commit_message, reason, task_id)
    repo_key = make_repo_key(pathlib.Path(ctx.repo_dir))

    def _mutate(bypass_state: "AdvisoryReviewState") -> None:
        next_run_attempt = len(
            bypass_state.filter_advisory_runs(
                repo_key=repo_key,
                tool_name="advisory_pre_review",
                task_id=task_id,
            )
        ) + 1
        bypass_state.add_run(AdvisoryRunRecord(
            snapshot_hash=snapshot_hash,
            commit_message=commit_message,
            status="bypassed",
            ts=_utc_now(),
            bypass_reason=reason,
            bypassed_by_task=task_id,
            snapshot_paths=snapshot_paths,
            repo_key=repo_key,
            tool_name="advisory_pre_review",
            task_id=task_id,
            attempt=next_run_attempt,
        ))

    update_state(drive_root, _mutate)
    if "ANTHROPIC_API_KEY" in reason:
        msg = (
            "⚠️ ANTHROPIC_API_KEY is not set — advisory review skipped automatically. "
            "Bypass has been durably audited in events.jsonl. "
            "Set ANTHROPIC_API_KEY in Settings to enable Claude Code advisory reviews."
        )
    else:
        msg = "Advisory review bypassed. Bypass has been durably audited."
    return json.dumps({"status": "bypassed", "snapshot_hash": snapshot_hash,
                       "bypass_reason": reason, "message": msg},
                      ensure_ascii=False, indent=2)


def _resolve_matching_obligations(
    state: "AdvisoryReviewState",
    items: list,
    snapshot_hash: str,
    *,
    repo_key: str | None = None,
) -> None:
    """Resolve open obligations whose checklist item appears in PASS but NOT in FAIL.

    An obligation is only resolved when the advisory emits PASS for that item
    and does not also emit a contradictory FAIL for the same item.  Conflicting
    entries (both PASS and FAIL for the same item) leave the obligation open so
    the agent is forced to re-examine and produce a clean, unambiguous result.
    """
    if not items:
        return
    # Build per-item verdict sets to detect contradictions
    item_verdicts: dict[str, set[str]] = {}
    obligation_verdicts: dict[str, set[str]] = {}
    for i in items:
        if not isinstance(i, dict):
            continue
        verdict = str(i.get("verdict", "")).upper().strip()
        item_name = str(i.get("item", "")).strip()
        if not item_name or not verdict:
            continue
        explicit_obligation_id = normalize_reviewer_obligation_id(i.get("obligation_id", ""))
        normalized_item_name, suffix_obligation_id = strip_obligation_suffix(item_name)
        normalized_item_name = normalized_item_name.strip().lower()
        if normalized_item_name:
            item_verdicts.setdefault(normalized_item_name, set()).add(verdict)
        # When the reviewer supplies BOTH an explicit `obligation_id` field and
        # an `(obligation <id>)` suffix embedded in `item`, they must agree. A
        # mismatch means the entry is ambiguous — recording both ids would let a
        # single malformed PASS clear two unrelated obligations (including their
        # associated commit-readiness debt). Treat such entries as ambiguous and
        # ignore them for obligation resolution; the item-name fallback still
        # applies below when it's unambiguous.
        if explicit_obligation_id and suffix_obligation_id:
            if explicit_obligation_id.lower() == suffix_obligation_id.lower():
                obligation_verdicts.setdefault(explicit_obligation_id, set()).add(verdict)
            # else: mismatch — skip both ids for this entry
            continue
        if explicit_obligation_id:
            obligation_verdicts.setdefault(explicit_obligation_id, set()).add(verdict)
        elif suffix_obligation_id:
            obligation_verdicts.setdefault(suffix_obligation_id, set()).add(verdict)

    # Only PASS items that have no FAIL entry for the same item
    unambiguous_pass = {
        item_name
        for item_name, verdicts in item_verdicts.items()
        if "PASS" in verdicts and "FAIL" not in verdicts
    }
    unambiguous_pass_ids = {
        obligation_id
        for obligation_id, verdicts in obligation_verdicts.items()
        if "PASS" in verdicts and "FAIL" not in verdicts
    }

    open_obs = state.get_open_obligations(repo_key=repo_key)

    # Count open obligations per item so item-name fallback is safe only when
    # unambiguous (exactly one open obligation for that item).  With per-finding
    # fingerprint keying, a same-item PASS must not clear a different finding
    # that was not addressed.
    from collections import Counter as _Counter
    item_open_count = _Counter(o.item.lower() for o in open_obs)

    resolved = [
        o.obligation_id for o in open_obs
        if o.obligation_id.lower() in unambiguous_pass_ids
        or (
            o.item.lower() in unambiguous_pass
            and item_open_count[o.item.lower()] == 1
        )
    ]
    if resolved:
        state.resolve_obligations(
            resolved,
            resolved_by=f"advisory run {snapshot_hash[:12]}",
            repo_key=repo_key,
        )
        state._sync_commit_readiness_debts(repo_key=repo_key)


def _next_step_guidance(latest: Optional["AdvisoryRunRecord"], state: "AdvisoryReviewState",
                        stale_from_edit: bool, stale_from_edit_ts: Optional[str],
                        open_obs: list, open_debts: list, effective_is_fresh: bool = False) -> str:
    """Return a concrete next-step string based on current advisory state."""
    unresolved_parts: list[str] = []
    if open_obs:
        unresolved_parts.append(f"{len(open_obs)} open obligation(s)")
    if open_debts:
        unresolved_parts.append(f"{len(open_debts)} commit-readiness debt item(s)")
    unresolved_summary = " and ".join(unresolved_parts)
    debt_prefix = (
        f"Start from the {len(open_debts)} commit-readiness debt item(s) surfaced by review_status, "
        "resolve or rebut each one, and only then continue with new edits. "
        if open_debts else ""
    )
    open_obligation_prefix = (
        f"There are still {len(open_obs)} open obligation(s) from previous blocking rounds. "
        if open_obs else ""
    )
    if not effective_is_fresh:
        # preflight_blocked for the exact current snapshot (SDK skipped —
        # a staged .py file has a SyntaxError). Raise it above parse_failure
        # in the priority list: the concrete syntax-error location is much
        # more actionable than the generic "could not parse" message.
        # tests_preflight_blocked: pytest failed before the SDK call.
        if latest and latest.status == "tests_preflight_blocked" and not stale_from_edit:
            tail = (
                f" There are still {len(open_obs)} open obligation(s) from previous "
                "blocking rounds — address those in the same edit pass."
                if open_obs else ""
            )
            return (
                "Last advisory run was blocked by test preflight: pytest failed before "
                "the Claude SDK call. Fix the failing tests and re-run advisory_pre_review. "
                "Use advisory_pre_review(skip_tests=True) only for intentional WIP code."
                + debt_prefix
                + tail
            )
        # preflight_blocked for the exact current snapshot (SDK skipped —
        # a staged .py file has a SyntaxError). Raise it above parse_failure
        # in the priority list: the concrete syntax-error location is much
        # more actionable than the generic "could not parse" message.
        if latest and latest.status == "preflight_blocked" and not stale_from_edit:
            tail = (
                f" There are still {len(open_obs)} open obligation(s) from previous "
                "blocking rounds — address those in the same edit pass."
                if open_obs else ""
            )
            return (
                "Last advisory run was blocked by syntax preflight: a staged .py "
                "file has a SyntaxError. See the file:line:msg in the raw_result. "
                "Fix the syntax error and re-run advisory_pre_review."
                + debt_prefix
                + tail
            )
        # parse_failure for the exact current snapshot (advisory ran but output was unparseable)
        if latest and latest.status == "parse_failure" and not stale_from_edit:
            if open_obs or open_debts:
                return (
                    "Last advisory run produced unparseable output (parse_failure). "
                    + open_obligation_prefix
                    + debt_prefix
                    + "After the first blocked review, stop patching one finding at a time: re-read the full diff, "
                    + "group obligations by root cause, rewrite the plan, finish all remaining edits, then re-run "
                    + "advisory_pre_review(commit_message='...'), or bypass: "
                    + "repo_commit(skip_advisory_pre_review=True) (audited)."
                )
            return (
                "Last advisory run produced unparseable output (parse_failure). "
                + "Re-run: advisory_pre_review(commit_message='...'), "
                + "or bypass: repo_commit(skip_advisory_pre_review=True) (audited)."
            )
        if open_obs or open_debts:
            stale_prefix = (
                f"Advisory was invalidated by a worktree edit at {stale_from_edit_ts}. "
                if stale_from_edit else
                "Advisory is stale or missing for the current snapshot. "
            )
            return (
                stale_prefix
                + open_obligation_prefix
                + debt_prefix
                + "After the first blocked review, stop patching one finding at a time: re-read the full diff, "
                + "group obligations by root cause, rewrite the plan, finish all remaining edits, then run "
                + "advisory_pre_review(commit_message='...')."
            )
        if stale_from_edit:
            return (
                f"Advisory was invalidated by a worktree edit at {stale_from_edit_ts}. "
                "Complete ALL remaining edits, then run: "
                "advisory_pre_review(commit_message='...')"
            )
        if not state.runs:
            return "No advisory run yet. Run: advisory_pre_review(commit_message='...')"
        return "Advisory is stale (snapshot changed). Run: advisory_pre_review(commit_message='...')"

    # Advisory is effectively fresh — check obligations and findings
    if open_obs or open_debts:
        return (
            f"Advisory is current but unresolved review debt remains: {unresolved_summary or 'review debt'}. "
            "repo_commit will be blocked until that debt is cleared. "
            + debt_prefix
            + "After the first blocked review, stop patching one "
            + "finding at a time: re-read the full diff, group obligations by root cause, rewrite "
            + "the plan, then continue. Fix the issues, re-run advisory_pre_review so it marks "
            + "them PASS, or bypass: repo_commit(skip_advisory_pre_review=True) (audited)."
        )

    if latest and latest.status == "skipped":
        return (
            "Advisory was skipped — prompt exceeded the budget gate (prompt too large for advisory). "
            "repo_commit may proceed. Consider splitting the commit into smaller chunks "
            "so advisory can run on the next change."
        )

    if latest and latest.status == "bypassed":
        return (
            "Advisory was bypassed (audited). "
            "No open obligations — repo_commit should proceed. "
            "Consider running advisory_pre_review for a proper review."
        )

    fresh_critical = [
        i for i in (latest.items if latest else []) or []
        if isinstance(i, dict) and str(i.get("verdict", "")).upper() == "FAIL"
        and str(i.get("severity", "")).lower() == "critical"
    ]
    if fresh_critical:
        return (
            f"Advisory found {len(fresh_critical)} critical issue(s). "
            "Fix ALL critical findings, then re-run advisory_pre_review. "
            "Do NOT call repo_commit until advisory is fresh with 0 critical findings."
        )
    return (
        "Advisory is fresh with no critical findings. "
        "Proceed with: repo_commit(commit_message='...'). "
        "⚠️ Do NOT make any further edits — any edit will make advisory stale."
    )


def _check_worktree_version_sync(repo_dir: pathlib.Path) -> str:
    """Backward-compatible alias — delegates to shared helper in review_helpers."""
    return _check_worktree_version_sync_shared(repo_dir)


# -- Tool handlers --

def _persist_preflight_record(
    ctx: ToolContext,
    snapshot_hash: str,
    commit_message: str,
    status: str,
    raw_result: str,
    paths,
    duration_sec: float,
    readiness_warnings: list,
) -> None:
    """Persist a durable AdvisoryRunRecord for any preflight-blocked outcome.

    Shared by syntax-preflight (``preflight_blocked``) and test-preflight
    (``tests_preflight_blocked``) paths so ``review_status`` and the
    ``Review Continuity`` context surface the concrete blocker instead of
    reporting "no advisory run yet" after restarts.
    Never raises; failures are logged and swallowed (non-fatal).
    Derives drive_root / repo_key / task_id from ctx to stay under 8 params.
    """
    try:
        drive_root = pathlib.Path(ctx.drive_root)
        repo_key = make_repo_key(pathlib.Path(ctx.repo_dir))
        task_id = str(getattr(ctx, "task_id", "") or "")

        def _mutate(pre_state: AdvisoryReviewState) -> None:
            next_attempt = len(
                pre_state.filter_advisory_runs(
                    repo_key=repo_key,
                    tool_name="advisory_pre_review",
                    task_id=task_id,
                )
            ) + 1
            pre_state.add_run(AdvisoryRunRecord(
                snapshot_hash=snapshot_hash,
                commit_message=commit_message,
                status=status,
                ts=_utc_now(),
                items=[],
                snapshot_summary="preflight block — SDK not called",
                raw_result=raw_result,
                snapshot_paths=paths,
                repo_key=repo_key,
                tool_name="advisory_pre_review",
                task_id=task_id,
                attempt=next_attempt,
                readiness_warnings=readiness_warnings,
                prompt_chars=0,
                model_used="",
                duration_sec=duration_sec,
            ))
        update_state(drive_root, _mutate)
    except Exception:
        log.debug("_persist_preflight_record failed (non-critical)", exc_info=True)


def _advisory_pre_sdk_gate(
    ctx: ToolContext,
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    snapshot_hash: str,
    commit_message: str,
    paths: Optional[List[str]],
    skip_tests: bool,
):
    """Run all cheap guard checks before the expensive Claude SDK advisory call.

    Returns a 3-tuple (readiness_warnings, changed_files, early_exit):
    - ``readiness_warnings``: list of non-blocking warning strings
    - ``changed_files``: porcelain git-status string (may be empty or error sentinel)
    - ``early_exit``: if not None, caller returns this JSON string immediately

    Extracted from ``_handle_advisory_pre_review`` to keep that function under
    the 300-line method hard gate (DEVELOPMENT.md / smoke test enforcement).
    repo_key, state, and task_id are derived here to stay under the 8-param limit.
    """
    repo_key = make_repo_key(repo_dir)
    task_id = str(getattr(ctx, "task_id", "") or "")
    state = load_state(drive_root)

    # Readiness gate FIRST: reject clean worktree before any fresh-run short-circuit.
    readiness_warnings = check_worktree_readiness(repo_dir, paths=paths)
    if readiness_warnings and any("no uncommitted changes" in w.lower() for w in readiness_warnings):
        ctx.emit_progress_fn(f"⚠️ Advisory readiness gate: {'; '.join(readiness_warnings)}")
        return readiness_warnings, "", json.dumps({
            "status": "error",
            "snapshot_hash": snapshot_hash,
            "message": "No uncommitted changes detected — nothing to review.",
            "readiness_warnings": readiness_warnings,
        }, ensure_ascii=False, indent=2)

    if readiness_warnings:
        try:
            append_jsonl(drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "advisory_readiness_gate",
                "warnings": readiness_warnings,
                "task_id": task_id,
            })
        except Exception:
            pass

    # Fresh-run short-circuit: skip if already covered and no obligations/debt outstanding.
    existing = state.find_by_hash(snapshot_hash, repo_key=repo_key)
    open_obligations = state.get_open_obligations(repo_key=repo_key)
    open_debts = state.get_open_commit_readiness_debts(repo_key=repo_key)
    already_fresh_ok = (
        existing and existing.status in ("fresh", "bypassed", "skipped")
        and not open_obligations and not open_debts
    )
    if already_fresh_ok:
        return readiness_warnings, "", json.dumps({
            "status": "already_fresh",
            "snapshot_hash": snapshot_hash,
            "ts": existing.ts,
            "items": existing.items,
            "readiness_warnings": readiness_warnings,
            "message": "A fresh advisory run already exists for this snapshot. Proceed with repo_commit.",
        }, ensure_ascii=False, indent=2)

    ctx.emit_progress_fn("Running advisory pre-review (Claude Code, read-only)...")
    changed_files = _get_changed_file_list(repo_dir, paths=paths)

    if changed_files.startswith("⚠️ ADVISORY_ERROR"):
        return readiness_warnings, changed_files, json.dumps({
            "status": "error",
            "snapshot_hash": snapshot_hash,
            "error": changed_files,
            "message": (
                "Advisory review aborted: could not retrieve changed file list. "
                "Fix the error and retry, or use skip_advisory_pre_review=True to bypass (will be audited)."
            ),
        }, ensure_ascii=False, indent=2)

    # Cheap version-sync check — non-fatal warning only.
    version_sync_warning = _check_worktree_version_sync(repo_dir)
    if version_sync_warning:
        ctx.emit_progress_fn(f"⚠️ Advisory preflight: {version_sync_warning}")

    # Test preflight gate (A3 — v4.41.0): run pytest before the expensive SDK call.
    if not skip_tests:
        ctx.emit_progress_fn("Running tests before advisory SDK call...")
        test_err = _run_advisory_tests(ctx)
        if test_err:
            msg = (
                "⚠️ TESTS_PREFLIGHT_BLOCKED: Tests must pass before advisory review.\n"
                "Fix the failures below, then re-run advisory_pre_review.\n"
                "Use skip_tests=True if this is intentionally incomplete WIP code.\n\n"
                f"{test_err}"
            )
            ctx.emit_progress_fn(msg)
            # Persist a durable non-fresh record so review_status / continuity can
            # surface the actual blocker instead of "no advisory run yet".
            _persist_preflight_record(
                ctx=ctx,
                snapshot_hash=snapshot_hash,
                commit_message=commit_message,
                status="tests_preflight_blocked",
                raw_result=msg,
                paths=paths,
                duration_sec=0.0,
                readiness_warnings=readiness_warnings,
            )
            return readiness_warnings, changed_files, json.dumps({
                "status": "tests_preflight_blocked",
                "snapshot_hash": snapshot_hash,
                "message": msg,
                "readiness_warnings": readiness_warnings,
            }, ensure_ascii=False, indent=2)
        ctx.emit_progress_fn("Tests passed ✓ — proceeding with advisory SDK call.")

    return readiness_warnings, changed_files, None


def _run_advisory_tests(ctx: ToolContext) -> Optional[str]:
    """Run pytest before the advisory SDK call.

    Thin wrapper around ``review_helpers._run_review_preflight_tests`` — kept
    as a distinct call site so the existing monkeypatch-based tests that stub
    ``_run_advisory_tests`` continue to work unchanged, and so the advisory
    and bypass paths can be traced separately in logs.

    Returns a non-None error string when tests fail, None when tests pass.
    Respects the shared ``OUROBOROS_PRE_PUSH_TESTS`` env gate.
    """
    return _run_review_preflight_tests(ctx)


def _handle_advisory_pre_review(
    ctx: ToolContext,
    commit_message: str = "",
    skip_advisory_pre_review: bool = False,
    goal: str = "",
    scope: str = "",
    paths: Optional[List[str]] = None,
    skip_tests: bool = False,
) -> str:
    """Run an advisory pre-commit review via Claude Agent SDK (read-only)."""
    repo_dir = pathlib.Path(ctx.repo_dir)
    drive_root = pathlib.Path(ctx.drive_root)

    snapshot_hash = compute_snapshot_hash(repo_dir, commit_message, paths=paths)

    # repo_key / task_id / state are needed for bypass recording only;
    # _advisory_pre_sdk_gate re-derives them internally to stay under 8 params.
    repo_key = make_repo_key(repo_dir)
    task_id = str(getattr(ctx, "task_id", "") or "")
    state = load_state(drive_root)

    # Auto-bypass if Anthropic key is absent — audit it transparently
    if not os.environ.get("ANTHROPIC_API_KEY", ""):
        return _record_bypass(ctx, state, snapshot_hash, commit_message,
                               "ANTHROPIC_API_KEY not set — auto-bypassed", task_id, drive_root,
                               snapshot_paths=paths)

    # Handle explicit bypass
    if skip_advisory_pre_review:
        return _record_bypass(ctx, state, snapshot_hash, commit_message,
                               "explicit skip_advisory_pre_review=True", task_id, drive_root,
                               snapshot_paths=paths)

    readiness_warnings, changed_files, early_exit = _advisory_pre_sdk_gate(
        ctx=ctx,
        repo_dir=repo_dir,
        drive_root=drive_root,
        snapshot_hash=snapshot_hash,
        commit_message=commit_message,
        paths=paths,
        skip_tests=skip_tests,
    )
    if early_exit is not None:
        return early_exit

    import time as _time
    _advisory_start = _time.monotonic()
    items, raw_result, model_used, prompt_chars = _run_claude_advisory(
        repo_dir, commit_message, ctx, goal=goal, scope=scope, paths=paths, drive_root=drive_root
    )
    _advisory_duration = _time.monotonic() - _advisory_start

    # Handle errors from the CLI
    if raw_result.startswith("⚠️ ADVISORY_ERROR"):
        return json.dumps({
            "status": "error",
            "snapshot_hash": snapshot_hash,
            "error": raw_result,
            "readiness_warnings": readiness_warnings,
            "message": (
                "Advisory review failed to run. Fix the error and retry, "
                "or use skip_advisory_pre_review=True to bypass (will be audited)."
            ),
        }, ensure_ascii=False, indent=2)

    # Handle syntax-preflight short-circuit (v4.38.0 + v4.39.0 persistence).
    # The SDK was intentionally skipped because a staged `.py` file would not
    # even compile. Surface this as an explicit `preflight_blocked` status so
    # it is not misclassified as `parse_failure` (which would hide the actual
    # syntax error from the agent). Persist a durable AdvisoryRunRecord so
    # `review_status` and the `Review Continuity` context can surface the
    # block reason after a restart.
    if raw_result.startswith("⚠️ PREFLIGHT_BLOCKED"):
        _persist_preflight_record(
            ctx=ctx,
            snapshot_hash=snapshot_hash,
            commit_message=commit_message,
            status="preflight_blocked",
            raw_result=raw_result,
            paths=paths,
            duration_sec=_advisory_duration,
            readiness_warnings=readiness_warnings,
        )
        return json.dumps({
            "status": "preflight_blocked",
            "snapshot_hash": snapshot_hash,
            "error": raw_result,
            "readiness_warnings": readiness_warnings,
            "message": (
                "Advisory SDK was skipped: a staged .py file has a SyntaxError. "
                "Fix the syntax error listed above and re-run advisory_pre_review."
            ),
        }, ensure_ascii=False, indent=2)

    # Budget gate: prompt too large — non-blocking skip (mirrors scope review).
    # Persist a durable "skipped" run so _check_advisory_freshness treats this
    # snapshot as having been reviewed (is_fresh returns True for status="skipped").
    if raw_result.startswith("⚠️ ADVISORY_SKIPPED:"):
        snapshot_summary = f"{changed_files.count(chr(10)) + 1} file(s) changed"
        def _mutate_skip(skip_state: AdvisoryReviewState) -> None:
            next_run_attempt = len(
                skip_state.filter_advisory_runs(
                    repo_key=repo_key,
                    tool_name="advisory_pre_review",
                    task_id=task_id,
                )
            ) + 1
            skip_state.add_run(AdvisoryRunRecord(
                snapshot_hash=snapshot_hash,
                commit_message=commit_message,
                status="skipped",
                ts=_utc_now(),
                items=[],
                snapshot_summary=snapshot_summary,
                raw_result=raw_result,
                snapshot_paths=paths,
                repo_key=repo_key,
                tool_name="advisory_pre_review",
                task_id=task_id,
                attempt=next_run_attempt,
                readiness_warnings=readiness_warnings,
                prompt_chars=prompt_chars,
                model_used=model_used,
                duration_sec=_advisory_duration,
            ))

        update_state(drive_root, _mutate_skip)
        return json.dumps({
            "status": "skipped",
            "snapshot_hash": snapshot_hash,
            "message": raw_result,
            "readiness_warnings": readiness_warnings,
        }, ensure_ascii=False, indent=2)

    # Classify findings
    critical_fails = [i for i in items if isinstance(i, dict)
                      and str(i.get("verdict", "")).upper() == "FAIL"
                      and str(i.get("severity", "")).lower() == "critical"]
    advisory_fails = [i for i in items if isinstance(i, dict)
                      and str(i.get("verdict", "")).upper() == "FAIL"
                      and str(i.get("severity", "")).lower() != "critical"]

    snapshot_summary = f"{changed_files.count(chr(10)) + 1} file(s) changed"

    # If items is empty but raw_result is non-empty, the advisory ran but failed to parse.
    # Treat this as a parse_failure to avoid silently treating it as an all-clear.
    run_status = "fresh" if items else "parse_failure"
    run = AdvisoryRunRecord(
        snapshot_hash=snapshot_hash,
        commit_message=commit_message,
        status=run_status,
        ts=_utc_now(),
        items=items,
        snapshot_summary=snapshot_summary,
        raw_result=raw_result,
        snapshot_paths=paths,
        repo_key=repo_key,
        tool_name="advisory_pre_review",
        task_id=task_id,
        attempt=len(
            state.filter_advisory_runs(
                repo_key=repo_key,
                tool_name="advisory_pre_review",
                task_id=task_id,
            )
        ) + 1,
        readiness_warnings=readiness_warnings,
        prompt_chars=prompt_chars,
        model_used=model_used,
        duration_sec=_advisory_duration,
    )
    state.add_run(run)

    # Surface parse failures as explicit errors (not silent all-clears)
    if run_status == "parse_failure":
        save_state(drive_root, state)
        return json.dumps({
            "status": "parse_failure",
            "snapshot_hash": snapshot_hash,
            "error": "Advisory ran but returned no parseable checklist items.",
            "raw_result": _truncate_review_artifact(raw_result),
            "readiness_warnings": readiness_warnings,
            "message": (
                "Advisory output could not be parsed. Re-run advisory_pre_review, "
                "or use skip_advisory_pre_review=True to bypass (will be audited)."
            ),
        }, ensure_ascii=False, indent=2)

    # Always try to resolve open obligations from parseable advisory results.
    # _resolve_matching_obligations only resolves when PASS is unambiguous (no concurrent FAIL
    # for the same item), so it is safe to call even when critical_fails is non-empty.
    # An obligation whose checklist item now passes should be resolved regardless of whether
    # *other* unrelated items still fail — leaving it open would turn unrelated criticals into
    # a perpetual hard gate on closed obligations.
    if items:
        _resolve_matching_obligations(state, items, snapshot_hash, repo_key=repo_key)

    save_state(drive_root, state)

    # Build human-readable summary
    findings_summary: List[str] = []
    for item in critical_fails:
        findings_summary.append(f"  CRITICAL [{item.get('item','?')}]: {item.get('reason','')}")
    for item in advisory_fails:
        findings_summary.append(f"  ADVISORY [{item.get('item','?')}]: {item.get('reason','')}")

    result = {
        "status": "fresh",
        "snapshot_hash": snapshot_hash,
        "ts": run.ts,
        "items": items,
        "critical_count": len(critical_fails),
        "advisory_count": len(advisory_fails),
        "snapshot_summary": snapshot_summary,
        "readiness_warnings": readiness_warnings,
        "message": (
            f"Advisory review complete. {len(critical_fails)} critical, "
            f"{len(advisory_fails)} advisory findings. "
            "Fix issues and run repo_commit when ready."
        ),
    }
    if findings_summary:
        result["findings"] = findings_summary

    return json.dumps(result, ensure_ascii=False, indent=2)


def _attempt_actor_summary(attempt) -> dict:
    """Return compact triad_actors / scope_actor fields for a CommitAttemptRecord.

    Surfaces model_id + status only — raw text is never injected into context.
    """
    triad_raw = getattr(attempt, "triad_raw_results", None) or []
    scope_raw = getattr(attempt, "scope_raw_result", None) or {}
    return {
        "triad_actors": [
            {"model_id": r.get("model_id", "?"), "status": r.get("status", "?")}
            for r in triad_raw
        ],
        "scope_actor": (
            {"model_id": scope_raw.get("model_id", "?"), "status": scope_raw.get("status", "?")}
            if scope_raw.get("status") else None
        ),
    }


def _selected_attempt_payload(ca) -> Optional[Dict[str, Any]]:
    """Serialise a single commit attempt for review_status output.

    Returns ``None`` when there is no selected attempt (e.g. first commit on
    a fresh repo, or the scoped filter matched nothing).
    """
    if ca is None:
        return None
    return {
        "status": ca.status,
        "commit_message": ca.commit_message,
        "ts": ca.ts,
        "duration_sec": round(ca.duration_sec, 1),
        "block_reason": ca.block_reason or None,
        "block_details_preview": (
            _truncate_review_artifact(ca.block_details, limit=300)
            if ca.block_details else None
        ),
        "repo_key": ca.repo_key or None,
        "tool_name": ca.tool_name or None,
        "task_id": ca.task_id or None,
        "attempt": int(ca.attempt or 0) or None,
        "phase": ca.phase or None,
        "blocked": bool(ca.blocked),
        "late_result_pending": bool(ca.late_result_pending),
        "critical_findings": len(ca.critical_findings or []),
        "advisory_findings": len(ca.advisory_findings or []),
        "obligation_ids": list(ca.obligation_ids or []),
        "readiness_warnings": list(ca.readiness_warnings or []),
        "pre_review_fingerprint": ca.pre_review_fingerprint[:12] or None,
        "post_review_fingerprint": ca.post_review_fingerprint[:12] or None,
        "fingerprint_status": ca.fingerprint_status or None,
        "degraded_reasons": list(ca.degraded_reasons or []),
        **_attempt_actor_summary(ca),
    }


def _obligations_payload(open_obs: list) -> list:
    """Serialise open obligations for review_status output.

    Short reason previews (200-char cap via truncate_review_artifact) keep the
    status JSON readable without clipping source timestamps or commit message
    references that downstream tooling needs for durable forensics.
    """
    return [
        {
            "obligation_id": ob.obligation_id,
            "fingerprint": getattr(ob, "fingerprint", ""),
            "item": ob.item,
            "severity": ob.severity,
            "reason": _truncate_review_artifact(ob.reason, limit=200),
            "status": ob.status,
            "source_ts": ob.source_attempt_ts,
            "source_commit": ob.source_attempt_msg,
        }
        for ob in open_obs
    ]


def _commit_readiness_debts_payload(open_debts: list) -> list:
    return [
        {
            "debt_id": debt.debt_id,
            "category": debt.category,
            "title": debt.title,
            "summary": _truncate_review_artifact(debt.summary, limit=220),
            "status": debt.status,
            "severity": debt.severity,
            "source": debt.source,
            "repo_key": debt.repo_key or None,
            "source_obligation_ids": list(debt.source_obligation_ids or []),
            "evidence": list(debt.evidence or []),
            "updated_at": debt.updated_at,
        }
        for debt in open_debts
    ]


def _handle_review_status(
    ctx: ToolContext,
    repo_key: str = "",
    tool_name: str = "",
    task_id: str = "",
    attempt: Optional[int] = None,
    include_raw: bool = False,
) -> str:
    """Show recent advisory pre-review run history AND last commit attempt state.

    Includes: advisory run history, staleness from edits, open obligations from
    blocking rounds, and a concrete next-step recommendation.

    When ``include_raw=True`` the output also contains the full per-actor
    evidence for the selected commit attempt: ``triad_raw_results`` (per
    triad model) and ``scope_raw_result`` (from the scope reviewer), with
    their raw text, parsed items, token counts, and cost. This is a durable
    read path for epistemic-integrity forensics — no need to open the
    advisory_review.json state file by hand.
    """
    drive_root = pathlib.Path(ctx.drive_root)
    state = load_state(drive_root)
    repo_dir_value = getattr(ctx, "repo_dir", "")
    repo_dir = (
        pathlib.Path(repo_dir_value)
        if isinstance(repo_dir_value, (str, pathlib.Path)) and str(repo_dir_value)
        else None
    )
    repo_filter = repo_key or (make_repo_key(repo_dir) if repo_dir is not None else None)
    tool_filter = tool_name or None
    task_filter = task_id or None
    filtered_runs = state.filter_advisory_runs(
        repo_key=repo_filter,
        tool_name=tool_filter,
        task_id=task_filter,
        attempt=attempt,
    )
    filtered_attempts = state.filter_attempts(
        repo_key=repo_filter,
        tool_name=tool_filter,
        task_id=task_filter,
        attempt=attempt,
    )

    runs_data = []
    for run in reversed(filtered_runs):  # full history — no [-5:] cap
        findings = [i for i in (run.items or []) if isinstance(i, dict)
                    and str(i.get("verdict", "")).upper() == "FAIL"]
        critical = [i for i in findings if str(i.get("severity", "")).lower() == "critical"]
        runs_data.append({
            "snapshot_hash": run.snapshot_hash[:12],
            "commit_message": run.commit_message,  # full — no [:80] truncation
            "status": run.status,
            "ts": run.ts,  # full ts — no [:16] truncation
            "critical_findings": len(critical),
            "total_findings": len(findings),
            "snapshot_summary": run.snapshot_summary,
            "bypass_reason": run.bypass_reason or None,
            "repo_key": run.repo_key or None,
            "tool_name": run.tool_name or None,
            "task_id": run.task_id or None,
            "attempt": int(run.attempt or 0) or None,
        })

    latest = filtered_runs[-1] if filtered_runs else None

    # Compute current snapshot hash using the same paths scope as the latest run
    # so path-scoped advisories don't appear falsely stale.
    try:
        if repo_dir is None:
            raise ValueError("repo_dir unavailable")
        latest_paths = latest.snapshot_paths if latest else None
        current_hash = compute_snapshot_hash(repo_dir, "", paths=latest_paths)
        hash_mismatch = bool(
            latest and latest.status in (
                "fresh", "bypassed", "skipped", "parse_failure",
                "preflight_blocked", "tests_preflight_blocked",
            ) and latest.snapshot_hash != current_hash
        )
    except Exception:
        current_hash = None
        hash_mismatch = False

    # Gate-accurate freshness: look up the run matching the CURRENT hash,
    # not just `latest` — handles restored snapshots where an older fresh run exists.
    open_obs = state.get_open_obligations(repo_key=repo_filter)
    open_debts = state.get_open_commit_readiness_debts(repo_key=repo_filter)
    matching_run = state.find_by_hash(current_hash, repo_key=repo_filter) if current_hash else None
    effective_is_fresh = bool(
        state.is_fresh(current_hash, repo_key=repo_filter) if current_hash else False
    )
    # Use matching_run for guidance; fall back to latest for history display
    guidance_run = matching_run or latest

    # Staleness: either explicit edit-invalidation OR live hash mismatch
    stale_from_edit = bool(
        hash_mismatch or (
            state.last_stale_from_edit_ts
            and state.last_stale_repo_key in ("", repo_filter)
        )
    )
    stale_from_edit_ts = (
        state.last_stale_from_edit_ts  # full ts — no [:16] truncation
        if state.last_stale_from_edit_ts and state.last_stale_repo_key in ("", repo_filter)
        else ("now (hash mismatch)" if hash_mismatch else None)
    )
    stale_reason = (
        state.last_stale_reason
        if state.last_stale_repo_key in ("", repo_filter)
        else ""
    ) or (
        "Current snapshot hash no longer matches the latest advisory run."
        if hash_mismatch else None
    )

    # Build commit attempt section
    selected_attempt = filtered_attempts[-1] if filtered_attempts else (
        None if (repo_filter or tool_filter or task_filter or attempt is not None) else state.last_commit_attempt
    )
    commit_attempt_data = _selected_attempt_payload(selected_attempt)

    attempts_data = []
    for entry in reversed(filtered_attempts):  # full history — no [-8:] cap
        attempts_data.append({
            "repo_key": entry.repo_key or None,
            "tool_name": entry.tool_name or None,
            "task_id": entry.task_id or None,
            "attempt": int(entry.attempt or 0) or None,
            "phase": entry.phase or None,
            "status": entry.status,
            "blocked": bool(entry.blocked),
            "late_result_pending": bool(entry.late_result_pending),
            "critical_findings": len(entry.critical_findings or []),
            "advisory_findings": len(entry.advisory_findings or []),
            "obligation_ids": list(entry.obligation_ids or []),
            "readiness_warnings": list(entry.readiness_warnings or []),
            "pre_review_fingerprint": entry.pre_review_fingerprint[:12] or None,
            "post_review_fingerprint": entry.post_review_fingerprint[:12] or None,
            "fingerprint_status": entry.fingerprint_status or None,
            "degraded_reasons": list(entry.degraded_reasons or []),
            **_attempt_actor_summary(entry),
            "ts": entry.ts,  # full ts — no [:16] truncation
        })

    # Open obligations (already computed above as open_obs for effective_is_fresh)
    obligations_data = _obligations_payload(open_obs)
    debts_data = _commit_readiness_debts_payload(open_debts)

    # Determine readiness and actionable next step (via module-level helper)

    # Build human-readable summary
    ca = selected_attempt
    if ca and ca.status in ("blocked", "failed"):
        reason_map = {
            "no_advisory": "No fresh advisory pre-review found. Run advisory_pre_review first.",
            "critical_findings": "Reviewers found critical issues. Fix all issues listed, then re-run advisory.",
            "review_quorum": "Not enough review models responded. Retry — usually transient.",
            "parse_failure": "Review models could not produce parseable output. Retry the commit.",
            "infra_failure": "Infrastructure failure (git lock, git command, or review API). Check block_details.",
            "scope_blocked": "Scope reviewer blocked the commit. Address scope review findings.",
            "preflight": "Preflight check failed (missing VERSION/README). Stage all related files.",
            "revalidation_failed": "The staged diff changed after review. Re-run advisory and review on the final staged diff.",
            "fingerprint_unavailable": "The staged diff could not be fingerprinted for revalidation. Fix the git diff issue and retry.",
            "overlap_guard": "Another reviewed attempt is still active. Wait for it to finish or expire before retrying.",
        }
        block_action = reason_map.get(
            ca.block_reason,
            f"{ca.status}: {ca.block_reason or 'unknown'}. Check block_details."
        )
        label = "BLOCKED" if ca.status == "blocked" else "FAILED"
        if ca.late_result_pending:
            block_action += " Late result is still pending."
        status_msg = f"Last commit {label} ({ca.block_reason or 'unclassified'}): {block_action}"
    else:
        # Use effective (gate-accurate) status derived from live snapshot, not latest run
        pass  # status_msg set below after effective_status is computed

    next_step_msg = _next_step_guidance(guidance_run, state, stale_from_edit, stale_from_edit_ts,
                                         open_obs, open_debts, effective_is_fresh=effective_is_fresh)
    # Derive primary status fields from current snapshot (gate-accurate), not from latest run.
    # matching_run is the advisory that matches the live worktree hash (may differ from latest).
    # If a matching run exists for this hash, use its actual status (including "parse_failure")
    # rather than collapsing to "stale" — that would hide the real gate-relevant state.
    # Only fall back to "stale"/"none" when there is NO matching run for the current snapshot.
    if matching_run:
        effective_status = matching_run.status
    elif latest:
        effective_status = "stale"
    else:
        effective_status = "none"
    effective_hash = (
        matching_run.snapshot_hash[:12] if matching_run and matching_run.snapshot_hash else None
    )
    # status_summary / message MUST be derived from the effective (gate-accurate) state only.
    # Never show "latest run status" here — that can be from a different snapshot and is
    # confusing / internally contradictory.  If a blocking commit attempt is recorded we
    # prepend that context, but the current-snapshot status always closes the sentence.
    if ca and ca.status in ("blocked", "failed"):
        # Keep the block-action sentence from above but append the live advisory state so
        # they remain consistent even when the worktree has changed since the last block.
        status_msg = f"{status_msg}  |  Current advisory: {effective_status}"
    else:
        status_msg = f"Current advisory: {effective_status}"
    if open_debts:
        status_msg = f"{status_msg}  |  Commit-readiness debt: {len(open_debts)}"

    retry_anchor = "commit_readiness_debt" if open_debts else None
    response_body: Dict[str, Any] = {
        "latest_advisory_status": effective_status,
        "latest_advisory_hash": effective_hash,
        "stale_from_edit": stale_from_edit,
        "stale_from_edit_ts": stale_from_edit_ts,
        "stale_reason": stale_reason,
        "filters": {
            "repo_key": repo_filter,
            "tool_name": tool_filter,
            "task_id": task_filter,
            "attempt": attempt,
        },
        "advisory_runs": runs_data,
        "attempts": attempts_data,
        "last_commit_attempt": commit_attempt_data,
        "open_obligations": obligations_data,
        "open_obligations_count": len(obligations_data),
        "commit_readiness_debts": debts_data,
        "commit_readiness_debts_count": len(debts_data),
        "repo_commit_ready": bool(effective_is_fresh and not open_obs and not open_debts),
        "retry_anchor": retry_anchor,
        "status_summary": status_msg,
        "message": status_msg,  # backward-compat alias for status_summary
        "next_step": next_step_msg,
    }
    if include_raw and selected_attempt is not None:
        # Per-actor durable evidence for epistemic-integrity forensics
        # (see v4.32.0 observability contract in docs/ARCHITECTURE.md).
        # Default false — verbose payload is opt-in since raw_text can be large.
        response_body["raw_evidence"] = {
            "attempt_ts": selected_attempt.ts,
            "attempt_number": int(selected_attempt.attempt or 0) or None,
            "tool_name": selected_attempt.tool_name or None,
            "triad_raw_results": list(selected_attempt.triad_raw_results or []),
            "scope_raw_result": dict(selected_attempt.scope_raw_result or {}),
        }
    return json.dumps(response_body, ensure_ascii=False, indent=2)


# -- Tool registration --

def get_tools() -> list:
    return [
        ToolEntry(
            name="advisory_pre_review",
            timeout_sec=1200,
            schema={
                "name": "advisory_pre_review",
                "description": (
                    "Run an advisory pre-commit review via Claude Agent SDK (read-only: Read, Grep, Glob only). "
                    "MUST be called before repo_commit. Returns structured JSON findings. "
                    "Findings are advisory (non-blocking), but repo_commit is blocked when ANY of the following "
                    "holds: (a) no fresh matching advisory run for the current staged snapshot, "
                    "(b) open obligations from prior blocked rounds remain unresolved, or "
                    "(c) repo-scoped commit-readiness debt is still open (see review_status for details). "
                    "Correct workflow: finish edits -> advisory_pre_review(...) -> repo_commit(...) immediately. "
                    "WARNING: any edit (repo_write/str_replace_editor) after advisory_pre_review "
                    "automatically marks advisory as stale and requires re-running it. "
                    "Use skip_advisory_pre_review=True to bypass the entire commit gate (bypass is durably "
                    "audited). Open obligations and commit-readiness debt remain in state for review_status "
                    "but do not block the bypassed commit."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "commit_message": {
                            "type": "string",
                            "description": "Intended commit message. Used to bind the advisory run to this specific commit.",
                        },
                        "skip_advisory_pre_review": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Explicitly bypass the advisory review. "
                                "Bypass is durably audited in events.jsonl. "
                                "Default: False."
                            ),
                        },
                        "goal": {
                            "type": "string",
                            "description": "High-level goal of this change. Used to judge completeness.",
                        },
                        "scope": {
                            "type": "string",
                            "description": "Declared scope boundary. Issues outside scope are advisory-only.",
                        },
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Explicit list of changed file paths. Auto-detected from git status if omitted.",
                        },
                        "skip_tests": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Skip the pre-advisory pytest run. Default: False (tests run by default). "
                                "Use True only for intentionally incomplete WIP code where test failures "
                                "are expected. Tests are run via 'pytest tests/ -q' before the SDK call "
                                "to catch broken code early and avoid wasting advisory budget."
                            ),
                        },
                    },
                    "required": ["commit_message"],
                },
            },
            handler=_handle_advisory_pre_review,
        ),
        ToolEntry(
            name="review_status",
            schema={
                "name": "review_status",
                "description": (
                    "Show recent advisory pre-review run history. "
                    "Read-only diagnostic — use to check if a fresh advisory run exists "
                    "before calling repo_commit. Also shows: last commit attempt state "
                    "(reviewing/blocked/succeeded/failed) with block reason and actionable guidance; "
                    "whether advisory is stale because of a worktree edit; "
                    "open obligations from previous blocking rounds; "
                    "open commit-readiness debt (durable repo-scoped anti-thrashing signal with fields "
                    "`commit_readiness_debts`, `commit_readiness_debts_count`); "
                    "`repo_commit_ready` (aligned with the real commit gate: fresh advisory AND no open "
                    "obligations AND no open debt); "
                    "`retry_anchor` (non-null, currently `commit_readiness_debt`, when debt is open — "
                    "start the next retry from that record instead of patching one obligation at a time); "
                    "and a concrete next_step recommendation. "
                    "Pass include_raw=true to surface the full per-actor evidence "
                    "(triad_raw_results, scope_raw_result) for the targeted attempt."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo_key": {
                            "type": "string",
                            "description": "Optional repo identity filter for attempt/advisory history.",
                        },
                        "tool_name": {
                            "type": "string",
                            "description": "Optional tool-name filter (for example repo_commit or repo_write_commit).",
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Optional task-id filter for attempt/advisory history.",
                        },
                        "attempt": {
                            "type": "integer",
                            "description": "Optional attempt number filter within the selected repo/tool/task scope.",
                        },
                        "include_raw": {
                            "type": "boolean",
                            "description": (
                                "If true, append full per-actor evidence "
                                "(triad_raw_results, scope_raw_result) for the "
                                "targeted commit attempt to the output. "
                                "Without this flag the output contains only "
                                "structured summaries. Defaults to false."
                            ),
                        },
                    },
                    "required": [],
                },
            },
            handler=_handle_review_status,
        ),
    ]
