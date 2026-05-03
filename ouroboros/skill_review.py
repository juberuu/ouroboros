"""Tri-model skill review (Phase 3).

Reuses the same review infrastructure that vets repo commits
(``_handle_multi_model_review`` in ``ouroboros.tools.review``) but:

- runs against one external skill package, not the staged diff of the
  self-modifying Ouroboros repo;
- uses the dedicated ``## Skill Review Checklist`` section in
  ``docs/CHECKLISTS.md`` instead of the Repo Commit Checklist;
- persists the verdict to the *skill* state plane
  (``data/state/skills/<name>/review.json``), not ``advisory_review.json``;
- never touches ``open_obligations`` or ``commit_readiness_debts`` — the
  two surfaces are deliberately siloed so a sticky skill finding cannot
  block repo commits and vice versa.

The module is pure logic: it does not register a tool. The public entry
point is ``review_skill``; the ``skill_review`` CLI tool (in
``ouroboros/tools/skill_exec.py``) wraps it.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ouroboros.skill_loader import (
    SkillReviewState,
    compute_content_hash,
    find_skill,
    save_review_state,
)
from ouroboros.tools.review_helpers import load_checklist_section

log = logging.getLogger(__name__)


# Review-pack contents — per checklist item, cap file reads to avoid
# pathological skill payloads blowing up the review prompt budget. The
# hard cap is enforced per individual file; the total prompt budget is
# enforced by ``_handle_multi_model_review`` downstream.
_MAX_SKILL_FILE_BYTES = 64 * 1024
_MAX_SKILL_FILES = 40
_MAX_RAW_RESULT_CHARS = 4000
_SKILL_CHECKLIST_SECTION = "Skill Review Checklist"

# File extensions that represent LOADABLE native code. These are hard-
# blocked by review because the subprocess can load them via
# ``ctypes.CDLL`` / ``import _somemodule`` / Node native addons, which
# would run code the reviewer never saw.
#
# v5.7.0 stale-comment fix: the previous comment claimed inert binary
# assets (``.png``, ``.mp3``, ``.wav``) were "still allowed with a
# filename+size omission note" — that has not been true since v4.x.
# ``_read_capped_text`` raises ``_SkillBinaryPayload`` for ANY non-UTF-8
# file in the runtime-reachable surface, regardless of extension. Phase
# 3 onwards is text-only. The explicit loadable-binary extension set
# below is kept around as a belt-and-braces signal so the rejection
# error surface can name the offending category before the UTF-8
# decode branch fires; it is NOT an allowlist of "safe" extensions.
_LOADABLE_BINARY_EXTENSIONS = frozenset(
    {
        ".so", ".dylib", ".dll",          # native shared libs
        ".pyc", ".pyo",                    # precompiled Python
        ".node",                           # Node.js native addons
        ".wasm",                           # WebAssembly (loadable by node/python)
        ".exe", ".bin",                    # generic executables
    }
)


class _SkillPackTooLarge(RuntimeError):
    """Raised by ``_build_skill_file_pack`` when a skill has more files
    than the review prompt budget allows. ``review_skill`` translates
    this into a persisted ``status=pending`` outcome rather than
    quietly truncating executable payload."""

    def __init__(self, file_count: int, limit: int) -> None:
        super().__init__(
            f"Skill pack exceeds reviewable cap: {file_count} files > {limit}."
        )
        self.file_count = file_count
        self.limit = limit


class _SkillFileUnreadable(RuntimeError):
    """Raised when a runtime-reachable skill file cannot be read.

    Failing open (returning a placeholder) would let a skill author
    ship a ``scripts/main.py`` with unreadable permissions — review
    would PASS over a content hash that also skips the file, and the
    skill could later execute once permissions change. We fail closed
    instead: review returns ``status=pending`` with a clear error."""

    def __init__(self, relpath: str, err: BaseException) -> None:
        super().__init__(
            f"Skill file {relpath!r} unreadable: {type(err).__name__}: {err}"
        )
        self.relpath = relpath
        self.err = err


class _SkillBinaryPayload(RuntimeError):
    """Raised when a reviewable skill file is not valid UTF-8.

    A binary payload (``.so``, ``.pyc``, native addon, raw bytes the
    subprocess could ``ctypes.CDLL`` into) is unreviewable by design:
    the external LLM reviewers cannot inspect its bytes, and letting
    ``review_skill`` emit a PASS tied to a content hash that included an
    opaque blob defeats the ARCHITECTURE.md Section 10 invariant 11
    (review is the primary gate). We therefore refuse review outright
    and ask the operator to either remove the file or document it as a
    non-executable data asset via ``assets/``."""

    def __init__(self, relpath: str, size_bytes: int) -> None:
        super().__init__(
            f"Skill file {relpath!r} is binary ({size_bytes} bytes); "
            "review refuses opaque payloads in the executable surface."
        )
        self.relpath = relpath
        self.size_bytes = size_bytes


class _SkillFileTooLarge(RuntimeError):
    """Raised when a single skill file exceeds the per-file byte cap.

    Silently truncating an oversized script would let a malicious author
    hide code past the truncation boundary and still ship a ``pass``
    verdict. Review refuses oversized files outright and asks the author
    to split them."""

    def __init__(self, relpath: str, size_bytes: int, limit: int) -> None:
        super().__init__(
            f"Skill file {relpath!r} is {size_bytes} bytes "
            f"(limit {limit}); review refuses truncation."
        )
        self.relpath = relpath
        self.size_bytes = size_bytes
        self.limit = limit


def _truncate_raw_result(text: str) -> str:
    """Cap a review's raw response for durable storage using the shared
    ``ouroboros.utils.truncate_review_artifact`` helper (which emits an
    explicit OMISSION NOTE and is the SSOT for every cognitive-artifact
    truncation path across the repo). DEVELOPMENT.md forbids hardcoded
    ``[:N]`` slicing of review outputs — delegate to the shared helper
    instead of growing a second divergent implementation here.
    """
    from ouroboros.utils import truncate_review_artifact
    return truncate_review_artifact(str(text or ""), limit=_MAX_RAW_RESULT_CHARS)
_SKILL_REVIEW_ITEMS = (
    "manifest_schema",
    "permissions_honesty",
    "no_repo_mutation",
    "path_confinement",
    "env_allowlist",
    "timeout_and_output_discipline",
    "extension_namespace_discipline",
    # v5.7.0: ``kind: "module"`` widgets ship arbitrary JS that the host
    # mounts inside a sandboxed ``<iframe srcdoc>`` with a strict CSP.
    # Reviewers MUST verify the JS does not touch ``document.cookie``,
    # ``localStorage``/``sessionStorage``, or ``fetch`` URLs outside
    # ``/api/extensions/<skill>/`` — even though the host CSP also
    # blocks those, defense-in-depth at review time prevents shipping
    # code whose intent is to escape the iframe sandbox. Non-module
    # widgets and non-extension skills MUST be marked ``PASS`` with
    # reason "Not applicable".
    "widget_module_safety",
)
_CRITICAL_ITEMS = frozenset(
    {
        "manifest_schema",
        "permissions_honesty",
        "no_repo_mutation",
        "path_confinement",
        "env_allowlist",
        # ``extension_namespace_discipline`` is critical only for
        # type: extension (checklist) — we surface it to the reviewer
        # but do not hard-block non-extension skills on its FAIL.
    }
)


@dataclass
class SkillReviewOutcome:
    """Return payload from ``review_skill``."""

    skill_name: str
    status: str  # "pass" | "fail" | "advisory" | "pending"
    findings: List[Dict[str, Any]] = field(default_factory=list)
    reviewer_models: List[str] = field(default_factory=list)
    content_hash: str = ""
    prompt_chars: int = 0
    cost_usd: float = 0.0
    raw_result: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _read_capped_text(path: pathlib.Path, *, relpath: str = "") -> str:
    """Read a skill file for the review pack, refusing oversized files.

    Truncating an executable script would let malicious logic hide past
    the boundary and still ship a PASS verdict tied to the full content
    hash. If the file exceeds ``_MAX_SKILL_FILE_BYTES`` we raise
    ``_SkillFileTooLarge``; ``review_skill`` translates that into a
    persisted ``pending`` outcome with a descriptive error.

    Any non-UTF-8 file in the runtime-reachable skill surface is a
    hard-block. Rationale: the subprocess runs with ``cwd=skill_dir``
    and can therefore ``ctypes.CDLL('./payload')`` /
    ``import _extensionless_module`` / ``Buffer.from(fs.readFileSync(...))``
    into arbitrary opaque bytes, even if those bytes are disguised as
    extensionless files or misnamed ``.png``/``.mp3`` blobs. We accept
    the UX cost — Phase 3 skills must ship text-only payloads — to
    keep the review-is-primary-gate invariant honest. Media-bearing
    skills can stash binary assets OUTSIDE the skill checkout (e.g.
    fetch on demand) or wait for a future phase that adds an
    explicit manifest-declared binary-asset allowlist.

    The explicit loadable-binary extension denylist
    (``_LOADABLE_BINARY_EXTENSIONS``) is kept around as a
    belt-and-braces signal so the rejection error surface can identify
    such files even before the UTF-8 decode branch runs.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        # Fail CLOSED — see ``_SkillFileUnreadable`` docstring. A
        # placeholder return value would let review PASS over a file
        # that was excluded from both the review pack and the content
        # hash (``compute_content_hash`` similarly skips unreadable
        # files). ``review_skill`` translates this into ``pending``
        # with an actionable error.
        raise _SkillFileUnreadable(relpath or path.name, exc) from exc
    if len(data) > _MAX_SKILL_FILE_BYTES:
        raise _SkillFileTooLarge(
            relpath or path.name, len(data), _MAX_SKILL_FILE_BYTES
        )
    lowered = path.name.lower()
    if any(lowered.endswith(ext) for ext in _LOADABLE_BINARY_EXTENSIONS):
        raise _SkillBinaryPayload(relpath or path.name, len(data))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # ANY non-UTF8 byte sequence in the runtime-reachable surface
        # blocks review. Disguised/extensionless binaries would
        # otherwise slip through the extension-based check above.
        raise _SkillBinaryPayload(relpath or path.name, len(data)) from exc


def _build_skill_file_pack(
    skill_dir: pathlib.Path,
    *,
    manifest_entry: str = "",
    manifest_scripts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Return a fenced-code pack of every reviewable file in the skill dir.

    ``skill_loader._iter_payload_files`` already decides which files count
    for hashing; the pack here mirrors that set — passing the same
    ``manifest_entry`` and ``manifest_scripts`` so every file that could
    actually execute is visible to the reviewer just like it is tracked
    by the content hash.
    """
    from ouroboros.skill_loader import _iter_payload_files  # pylint: disable=W0212

    skill_dir = skill_dir.resolve()
    files = _iter_payload_files(
        skill_dir,
        manifest_entry=manifest_entry,
        manifest_scripts=manifest_scripts,
    )
    if not files:
        return "(empty skill directory — no manifest, no payload)"
    if len(files) > _MAX_SKILL_FILES:
        # Silently truncating here would let a pathological skill hide
        # executable logic in file #41+ and still pass review — the
        # caller (`review_skill`) must refuse to persist a PASS verdict
        # when the pack is incomplete. We raise a dedicated sentinel
        # instead of truncating so the review path short-circuits.
        raise _SkillPackTooLarge(len(files), _MAX_SKILL_FILES)
    extras = 0

    blocks: List[str] = []
    for file_path in files:
        rel = file_path.relative_to(skill_dir).as_posix()
        body = _read_capped_text(file_path, relpath=rel)
        blocks.append(
            f"### {rel}\n\n```\n{body}\n```"
        )
    return "\n\n".join(blocks)


def _load_governance_artifact(
    repo_root: pathlib.Path,
    relpath: str,
) -> str:
    """Load a core-governance doc for the skill-review prompt.

    DEVELOPMENT.md 'When adding a new reasoning flow' requires every new
    flow that reasons about code structure or engineering standards to
    load ``docs/ARCHITECTURE.md`` (and ``docs/DEVELOPMENT.md`` for
    engineering-standard checks) as first-class context, with an
    explicit OMISSION NOTE when the file is unavailable so the
    reviewer cannot silently operate on an incomplete surface.
    """
    candidate = repo_root / relpath
    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError:
        log.warning("Skill review: failed to load governance artifact %s", candidate, exc_info=True)
        return (
            f"⚠️ OMISSION NOTE: {relpath} could not be loaded from "
            f"{repo_root}. Skill review operated without this governance "
            "artifact — verdicts about architectural/engineering alignment "
            "may be incomplete."
        )
    return text


# Resolve the repo root from this module's location so the governance
# loader works both in source checkouts and packaged builds (identical to
# how ``review_helpers.REPO_ROOT`` is computed).
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _build_review_prompt(
    skill_name: str,
    skill_dir: pathlib.Path,
    manifest_dump: str,
    content_hash: str,
    file_pack: str,
) -> str:
    try:
        checklist_section = load_checklist_section(_SKILL_CHECKLIST_SECTION)
    except ValueError as exc:
        checklist_section = (
            f"(⚠️ SKILL_REVIEW_ERROR: checklist section missing: {exc})"
        )
    architecture_text = _load_governance_artifact(_REPO_ROOT, "docs/ARCHITECTURE.md")
    development_text = _load_governance_artifact(_REPO_ROOT, "docs/DEVELOPMENT.md")
    bible_text = _load_governance_artifact(_REPO_ROOT, "BIBLE.md")
    items_json = json.dumps(list(_SKILL_REVIEW_ITEMS))
    return f"""\
You are performing a SKILL review, not a repo-commit review.

This review vets a single external skill package that lives OUTSIDE the
self-modifying Ouroboros repository. The skill cannot execute until it
produces a PASS verdict from this review.

## Skill identity
- name: {skill_name}
- skill_dir: {skill_dir}
- content_hash: {content_hash}

## Manifest (parsed)
```json
{manifest_dump}
```

## Checklist (source of truth — follow it literally)

{checklist_section}

## Governance context — docs/ARCHITECTURE.md

Use Section 10 (Key Invariants) and Section 12 (External Skills Layer)
as the binding description of what the skill is allowed to touch. In
particular invariant 11 is the authoritative rule: skills must not write
to the self-modifying repo, and reviewed execution is the primary gate.

{architecture_text}

## Governance context — docs/DEVELOPMENT.md

Use this as the engineering-standards baseline when judging
``timeout_and_output_discipline`` and when checking whether the skill's
code conforms to the module/function size expectations and the
no-silent-truncation rule for cognitive artifacts.

{development_text}

## Governance context — BIBLE.md

BIBLE.md is Ouroboros' constitutional core. Skills execute inside the
Ouroboros runtime, so a skill that violates a constitutional principle
(for example P0 bounded agency, or P9 version-history limits if the
skill manipulates release metadata) is grounds for FAIL even when the
Skill Review Checklist items permit the behaviour in isolation. Treat
BIBLE.md as the tie-breaker when a skill looks checklist-compliant but
contradicts the runtime's constitutional commitments.

{bible_text}

## Skill files (every runtime-reachable file in skill_dir, text-only)

{file_pack}

## Output contract

Return ONLY a JSON array with exactly one entry per checklist item.
Expected items (in order): {items_json}

Each entry MUST have this shape:

{{"item": "<one of the items above>",
  "verdict": "PASS" | "FAIL",
  "severity": "critical" | "advisory",
  "reason": "<why, citing concrete files/lines inside the skill pack>"}}

Rules:

- Every item must appear exactly once.
- No prose before or after the JSON array.
- If the skill's ``type`` is not ``extension``, mark
  ``extension_namespace_discipline`` as PASS with reason
  "Not applicable — type != extension".
- Base every critical FAIL on a concrete file/line you can quote from
  the skill pack. Do not invent violations.
"""


# ---------------------------------------------------------------------------
# Parsing / aggregation
# ---------------------------------------------------------------------------


def _extract_actor_findings(
    result_json: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Flatten per-reviewer findings and return the set of responsive models.

    ``ouroboros.tools.review._parse_model_response`` flattens each provider
    response into ``{"model", "provider", "verdict", "text", ...}`` before
    wrapping them in ``{"results": [...]}``. The ``text`` field holds the
    raw model output, which is expected to be the JSON array described in
    the skill review output contract (one entry per checklist item,
    exactly ``len(_SKILL_REVIEW_ITEMS)`` items).

    Returns ``(findings, responsive_models)``:

    - ``findings``: the concatenated per-item entries from every
      reviewer that produced a valid, complete response.
    - ``responsive_models``: the list of reviewer model IDs that
      actually met the contract (all 7 items present, each with a
      PASS/FAIL verdict). A reviewer that returned only a subset is
      treated as non-responsive for quorum purposes so a truncated
      response cannot pass the quorum gate and synthesise a false PASS.

    A top-level ``actor["verdict"] == "ERROR"`` means the provider
    returned a transport error — we skip those entirely.
    """
    findings: List[Dict[str, Any]] = []
    responsive: List[str] = []
    required_items = set(_SKILL_REVIEW_ITEMS)
    for actor in result_json.get("results") or []:
        if not isinstance(actor, dict):
            continue
        if str(actor.get("verdict") or "").upper() == "ERROR":
            continue
        model = str(actor.get("model") or actor.get("request_model") or "").strip()
        text = str(actor.get("text") or "")
        if not text.strip():
            continue
        parsed_items = _parse_json_array(text)
        actor_findings: List[Dict[str, Any]] = []
        covered_items: set[str] = set()
        for entry in parsed_items:
            if not isinstance(entry, dict):
                continue
            item = str(entry.get("item") or "")
            verdict = str(entry.get("verdict") or "").upper()
            if not item or verdict not in ("PASS", "FAIL"):
                # Ignore entries that do not meet the per-row contract.
                continue
            covered_items.add(item)
            actor_findings.append(
                {
                    "item": item,
                    "verdict": verdict,
                    "severity": str(entry.get("severity") or "advisory").lower(),
                    "reason": str(entry.get("reason") or "").strip(),
                    "model": model,
                }
            )
        # Reject partial responses: reviewer must cover every checklist
        # item. A response that only covers one item could otherwise pass
        # the quorum gate with zero observed FAILs on the other six items.
        if not required_items.issubset(covered_items):
            continue
        findings.extend(actor_findings)
        if model and model not in responsive:
            responsive.append(model)
    return findings, responsive


def _parse_json_array(content: str) -> List[Any]:
    text = content.strip()
    if "```" in text:
        # Strip ``` fences the models sometimes add.
        parts = text.split("```")
        for chunk in parts:
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("["):
                text = chunk
                break
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _aggregate_status(
    findings: List[Dict[str, Any]],
    skill_type: str,
    *,
    is_module_widget: bool = False,
) -> str:
    """Collapse per-reviewer findings into a single status.

    - any critical FAIL on a checklist item that is always-critical
      (or on ``extension_namespace_discipline`` when ``type==extension``;
      or on ``widget_module_safety`` for any extension. Reviewers mark it
      PASS/Not applicable for non-module widgets, but modules can be
      registered dynamically from plugin.py so manifest-only detection is
      not enough.)
      → ``fail``;
    - any advisory FAIL without a matching critical FAIL → ``advisory``;
    - otherwise → ``pass``.

    If the reviewer pipeline returned zero parseable findings (transport
    failure, all actors errored), the caller surfaces that as ``error``;
    this helper is only invoked when we have at least one finding.
    """
    has_critical_fail = False
    has_advisory_fail = False
    is_extension = skill_type == "extension"
    for finding in findings:
        verdict = finding.get("verdict") == "FAIL"
        if not verdict:
            continue
        item = finding.get("item")
        severity = finding.get("severity")
        if severity == "critical":
            if item in _CRITICAL_ITEMS:
                has_critical_fail = True
            elif item == "extension_namespace_discipline" and is_extension:
                has_critical_fail = True
            elif item == "widget_module_safety" and is_extension:
                has_critical_fail = True
            else:
                has_advisory_fail = True
        else:
            has_advisory_fail = True
    if has_critical_fail:
        return "fail"
    if has_advisory_fail:
        return "advisory"
    return "pass"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def review_skill(
    ctx: Any,
    skill_name: str,
    *,
    persist: bool = True,
) -> SkillReviewOutcome:
    """Run tri-model review on one skill and optionally persist the verdict.

    Returns a ``SkillReviewOutcome`` regardless of review outcome. On a
    transport / infrastructure failure the outcome has ``status="pending"``
    and ``error`` populated — the caller decides whether to surface it.
    """
    # Deferred import because review.py pulls a wide import graph that
    # skill_review does not need until the tool actually runs.
    from ouroboros.tools.review import _handle_multi_model_review
    from ouroboros.config import get_review_models

    drive_root = pathlib.Path(getattr(ctx, "drive_root", pathlib.Path.home() / "Ouroboros" / "data"))
    skill = find_skill(drive_root, skill_name)
    if skill is None:
        return SkillReviewOutcome(
            skill_name=skill_name,
            status="pending",
            error=f"Skill {skill_name!r} not found in the external skills checkout",
        )
    if skill.load_error:
        return SkillReviewOutcome(
            skill_name=skill_name,
            status="pending",
            error=f"Skill manifest could not be parsed: {skill.load_error}",
        )

    from ouroboros.skill_loader import SkillPayloadUnreadable
    try:
        content_hash = compute_content_hash(
            skill.skill_dir,
            manifest_entry=skill.manifest.entry,
            manifest_scripts=skill.manifest.scripts,
        )
    except SkillPayloadUnreadable as exc:
        return SkillReviewOutcome(
            skill_name=skill.name,
            status="pending",
            error=(
                f"Skill payload {exc.relpath!r} is unreadable "
                f"({type(exc.err).__name__}: {exc.err}). Review refuses "
                "to emit a PASS over a partial hash — fix file "
                "permissions or remove the unreadable file and re-run."
            ),
        )
    manifest_dump = json.dumps(
        {
            "name": skill.manifest.name,
            "description": skill.manifest.description,
            "version": skill.manifest.version,
            "type": skill.manifest.type,
            "runtime": skill.manifest.runtime,
            "timeout_sec": skill.manifest.timeout_sec,
            "permissions": list(skill.manifest.permissions),
            "env_from_settings": list(skill.manifest.env_from_settings),
            "requires": list(skill.manifest.requires),
            "scripts": list(skill.manifest.scripts),
            "entry": skill.manifest.entry,
        },
        ensure_ascii=False,
        indent=2,
    )
    try:
        file_pack = _build_skill_file_pack(
            skill.skill_dir,
            manifest_entry=skill.manifest.entry,
            manifest_scripts=skill.manifest.scripts,
        )
    except _SkillPackTooLarge as exc:
        return SkillReviewOutcome(
            skill_name=skill.name,
            status="pending",
            content_hash=content_hash,
            error=(
                f"Skill pack exceeds reviewable cap ({exc.file_count} files "
                f"> {exc.limit}). Reduce the skill payload or split it into "
                "multiple skills — review cannot cover every executable file "
                "as-is, and silently truncating would let a large skill slip "
                "malicious code past review."
            ),
        )
    except _SkillFileTooLarge as exc:
        return SkillReviewOutcome(
            skill_name=skill.name,
            status="pending",
            content_hash=content_hash,
            error=(
                f"Skill file {exc.relpath!r} is {exc.size_bytes} bytes, over "
                f"the {exc.limit}-byte per-file cap. Review refuses to "
                "truncate executable skill payload — shrink the file or "
                "split its logic so every byte can actually be reviewed."
            ),
        )
    except _SkillBinaryPayload as exc:
        return SkillReviewOutcome(
            skill_name=skill.name,
            status="pending",
            content_hash=content_hash,
            error=(
                f"Skill file {exc.relpath!r} ({exc.size_bytes} bytes) is "
                "binary / non-UTF-8. Review refuses opaque payloads in the "
                "executable skill surface — the subprocess could load them "
                "via ctypes/native addons without reviewer inspection. "
                "Remove the file from the skill or refactor the skill to "
                "store such payloads outside the hashed surface."
            ),
        )
    except _SkillFileUnreadable as exc:
        return SkillReviewOutcome(
            skill_name=skill.name,
            status="pending",
            content_hash=content_hash,
            error=(
                f"Skill file {exc.relpath!r} is unreadable "
                f"({type(exc.err).__name__}: {exc.err}). Review refuses "
                "to fail open — fix the file permissions or remove the "
                "file before re-running review_skill."
            ),
        )
    prompt = _build_review_prompt(
        skill_name=skill.name,
        skill_dir=skill.skill_dir,
        manifest_dump=manifest_dump,
        content_hash=content_hash,
        file_pack=file_pack,
    )

    models = list(get_review_models())
    try:
        result_json_text = _handle_multi_model_review(
            ctx,
            content=(
                "Review the skill package whose manifest and payload are "
                "included above, using the Skill Review Checklist. Return "
                "ONLY the JSON array described in the output contract."
            ),
            prompt=prompt,
            models=models,
        )
    except Exception as exc:  # pragma: no cover — transport failure path
        log.warning("Skill review infrastructure failure for %s", skill.name, exc_info=True)
        return SkillReviewOutcome(
            skill_name=skill.name,
            status="pending",
            reviewer_models=models,
            content_hash=content_hash,
            error=f"infrastructure failure: {exc}",
        )

    try:
        result_json = json.loads(result_json_text)
    except json.JSONDecodeError:
        return SkillReviewOutcome(
            skill_name=skill.name,
            status="pending",
            reviewer_models=models,
            content_hash=content_hash,
            error="review returned non-JSON top-level response",
            raw_result=_truncate_raw_result(result_json_text),
        )

    if "error" in result_json:
        return SkillReviewOutcome(
            skill_name=skill.name,
            status="pending",
            reviewer_models=models,
            content_hash=content_hash,
            error=f"review service error: {result_json['error']}",
        )

    findings, responded_models = _extract_actor_findings(result_json)
    responded_models = sorted(responded_models)
    if len(responded_models) < 2:
        return SkillReviewOutcome(
            skill_name=skill.name,
            status="pending",
            findings=findings,
            reviewer_models=models,
            content_hash=content_hash,
            error=(
                "Skill review quorum failure: fewer than 2 reviewers returned "
                "parseable findings. Raw result preserved."
            ),
            raw_result=_truncate_raw_result(result_json_text),
        )

    is_module_widget = (
        skill.manifest.is_extension()
        and isinstance(skill.manifest.ui_tab, dict)
        and str(((skill.manifest.ui_tab or {}).get("render") or {}).get("kind") or "") == "module"
    )
    status = _aggregate_status(
        findings,
        skill_type=skill.manifest.type,
        is_module_widget=is_module_widget,
    )
    outcome = SkillReviewOutcome(
        skill_name=skill.name,
        status=status,
        findings=findings,
        reviewer_models=responded_models,
        content_hash=content_hash,
        prompt_chars=len(prompt),
        raw_result=_truncate_raw_result(result_json_text),
    )

    if persist:
        save_review_state(
            drive_root,
            skill.name,
            SkillReviewState(
                status=outcome.status,
                content_hash=content_hash,
                findings=findings,
                reviewer_models=responded_models,
                timestamp=datetime.now(timezone.utc).isoformat(),
                prompt_chars=outcome.prompt_chars,
                cost_usd=outcome.cost_usd,
                raw_result=outcome.raw_result,
            ),
        )

    return outcome


__all__ = [
    "SkillReviewOutcome",
    "review_skill",
]
