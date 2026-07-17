#!/usr/bin/env python3
"""Run Ouroboros review without committing.

The default operator lane runs the REAL production commit-gate cycle
(advisory → triad → scope) on the staged diff. ``--contributor`` is a separate
non-committing PR-readiness lane: it reviews the exact committed
``base_ref..head_ref`` proposal with triad + scope only, using the shipped
reviewer defaults from the target-base checkout through OpenRouter. A clean
contributor packet means READY_FOR_INTEGRATION, never merge authorization;
maintainers still allocate release metadata and run the production gate on the
final squash landing tree.

Both lanes reuse the runtime review substrate. The wrapper adds operator
ergonomics only:

* an isolated detached worktree so the reviewed tree cannot change mid-run;
* a fresh drive root (never the live data root) for review state/observability;
* `OUROBOROS_RUNTIME_MODE=pro` by default — release diffs touch protected
  paths, which only pro mode may stage for review;
* OpenRouter key health-check/selection from the named operator pool
  (`limit_remaining` probe; `hope*` keys last; values never printed);
* the real advisory pre-review in the default operator lane;
* an explicit contributor profile that excludes Claude advisory, ignores local
  reviewer overrides, forces blocking clean semantics, and emits a redacted
  shareable evidence packet bound to base/head/tree/diff hashes;
* typed exit codes separating infrastructure failures from genuine review
  blocks.

Exit codes:
    0  review passed
    1  genuine review block (critical findings)
    2  staged diff is empty
    3  not a reviewer verdict (oversize diff policy, advisory/transport/key
       trouble, protection gate, quorum loss, preflight) — diagnose the named
       cause; rerunning without fixing it reproduces the same block

Usage (from repo/):
    python scripts/run_external_review.py ["commit message"] [--output DIR]
    python scripts/run_external_review.py --contributor \
        --base-ref upstream/ouroboros --head-ref HEAD ["PR title"]
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

REPO = pathlib.Path(__file__).resolve().parents[1]
DATA = pathlib.Path(
    os.environ.get("OUROBOROS_DATA_DIR", "") or (REPO.parent / "data")
).expanduser().resolve(strict=False)

# Allow `import ouroboros` when invoked as a standalone script from any cwd.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Release diffs touch protected core paths; only pro mode may stage them for
# review. An explicit operator env value still wins.
os.environ.setdefault("OUROBOROS_RUNTIME_MODE", "pro")

# Genuine review verdicts (the author must address findings); every other
# non-passed outcome is environment/infrastructure and is safe to retry after
# fixing the environment.
_GENUINE_BLOCK_REASONS = {"critical_findings"}
_OPENROUTER_MIN_REMAINING_USD = 10.0
_CONTRIBUTOR_DEFAULT_BASE_REF = "upstream/ouroboros"
_CONTRIBUTOR_PROFILE = "external_pr_readiness"
_CONTRIBUTOR_LANDING_OBLIGATION_ITEMS = frozenset({
    "version_bump",
    "changelog_and_badge",
})
_CONTRIBUTOR_DEFAULT_KEYS = (
    "OUROBOROS_REVIEW_MODELS",
    "OUROBOROS_SCOPE_REVIEW_MODELS",
    "OUROBOROS_EFFORT_REVIEW",
    "OUROBOROS_EFFORT_SCOPE_REVIEW",
    "OUROBOROS_SCOPE_REVIEW_FLOOR",
)
_REVIEW_SUBSTRATE_PATHS = frozenset({
    "BIBLE.md",
    "docs/ARCHITECTURE.md",
    "docs/CHECKLISTS.md",
    "docs/DEVELOPMENT.md",
    "scripts/run_external_review.py",
    "ouroboros/config.py",
    "ouroboros/capability_evidence.py",
    "ouroboros/code_intelligence.py",
    "ouroboros/context_budget.py",
    "ouroboros/deadline_utils.py",
    "ouroboros/llm.py",
    "ouroboros/outcomes.py",
    "ouroboros/platform_layer.py",
    "ouroboros/pricing.py",
    "ouroboros/provider_models.py",
    "ouroboros/preflight_runner.py",
    "ouroboros/review_substrate.py",
    "ouroboros/review_state.py",
    "ouroboros/runtime_mode_policy.py",
    "ouroboros/triad_review.py",
    "ouroboros/usage_accounting.py",
    "ouroboros/observability.py",
    "ouroboros/utils.py",
    "ouroboros/tools/claude_advisory_review.py",
    "ouroboros/tools/commit_gate.py",
    "ouroboros/tools/git.py",
    "ouroboros/tools/parallel_review.py",
    "ouroboros/tools/registry.py",
    "ouroboros/tools/review.py",
    "ouroboros/tools/review_context_atlas.py",
    "ouroboros/tools/review_helpers.py",
    "ouroboros/tools/review_revalidation.py",
    "ouroboros/tools/release_sync.py",
    "ouroboros/tools/review_synthesis.py",
    "ouroboros/tools/scope_review.py",
    "ouroboros/tools/scope_review_contract.py",
})
_RELEASE_MACHINERY_PATHS = frozenset({
    ".github/workflows/ci.yml",
    "build.sh",
    "build_linux.sh",
    "build_windows.ps1",
    "ouroboros/tools/release_sync.py",
    "scripts/build_repo_bundle.py",
    "supervisor/git_ops.py",
})

_CONTRIBUTOR_CONTRACT = {
    "profile": _CONTRIBUTOR_PROFILE,
    "purpose": "non_committing_external_pr_readiness",
    "commit_authorization": False,
    "release_metadata_owner": "maintainer_final_squash_landing",
    "version_checklist_rule": (
        "When the proposal leaves release-version values unchanged, treat "
        "version_bump and changelog_and_badge as PASS/Not applicable for this "
        "non-committing readiness review. Do not relax any other checklist item. "
        "If the proposal changes release metadata or release machinery, review "
        "those changes normally."
    ),
    "landing_rule": (
        "A maintainer must apply the proposal onto current ouroboros, allocate "
        "collision-free release metadata, and run the production review gate on "
        "the exact landing tree before commit/tag/push."
    ),
}


def _keys_file() -> pathlib.Path | None:
    candidates = [
        pathlib.Path(os.environ["OUROBOROS_KEYS_FILE"]).expanduser()
        if os.environ.get("OUROBOROS_KEYS_FILE", "").strip()
        else None,
        DATA.parent / "file1.txt",
        pathlib.Path.home() / "ouro" / "file1.txt",
        pathlib.Path.home() / "file1.txt",
    ]
    return next((path for path in candidates if path is not None and path.is_file()), None)


def _load_settings_into_env() -> None:
    """Load data/settings.json scalars into env; never print secret values."""
    settings_path = pathlib.Path(
        os.environ.get("OUROBOROS_SETTINGS_PATH", "") or (DATA / "settings.json")
    ).expanduser().resolve(strict=False)
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - operator script
            print(f"WARN: could not parse settings.json: {exc}", file=sys.stderr)
            data = {}
        for key, value in (data.items() if isinstance(data, dict) else []):
            if os.environ.get(key, "").strip():
                continue
            if isinstance(value, bool):
                os.environ[key] = "1" if value else "0"
            elif isinstance(value, (str, int, float)) and str(value) != "":
                os.environ[key] = str(value)
    else:
        print(f"WARN: settings.json not found at {settings_path}", file=sys.stderr)

    def _fallback(env_name: str, prefix: str) -> None:
        if os.environ.get(env_name, "").strip():
            return
        f1 = _keys_file()
        if f1 is None:
            return
        for line in f1.read_text(encoding="utf-8").splitlines():
            if line.strip().lower().startswith(prefix + ":"):
                os.environ[env_name] = line.split(":", 1)[1].strip()
                break

    _fallback("OPENAI_API_KEY", "openai")
    _fallback("ANTHROPIC_API_KEY", "anthropic")
    _fallback("OPENROUTER_API_KEY", "openrouter")
    if not os.environ.get("TOTAL_BUDGET", "").strip():
        print(
            "WARN: TOTAL_BUDGET is not configured (settings.json not found?) — "
            "the $10 default will starve a full triad+scope run. Export "
            "OUROBOROS_SETTINGS_PATH or TOTAL_BUDGET explicitly.",
            file=sys.stderr,
        )


def _git_text(args: list[str], *, cwd: pathlib.Path | None = None) -> str:
    cwd = cwd or REPO
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _git_bytes(args: list[str], *, cwd: pathlib.Path | None = None) -> bytes:
    cwd = cwd or REPO
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"").decode(
            "utf-8", errors="replace"
        ).strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _settings_defaults_at_ref(ref: str) -> dict[str, str]:
    """Read literal reviewer defaults from the target checkout without executing it."""
    source = _git_text(["show", f"{ref}:ouroboros/config.py"])
    tree = ast.parse(source, filename=f"{ref}:ouroboros/config.py")
    wanted = set(_CONTRIBUTOR_DEFAULT_KEYS)
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "SETTINGS_DEFAULTS" for target in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            break
        for key_node, value_node in zip(node.value.keys, node.value.values):
            try:
                key = ast.literal_eval(key_node)
            except (TypeError, ValueError):
                continue
            if key not in wanted:
                continue
            try:
                value = ast.literal_eval(value_node)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"target default {key} is not a literal value at {ref}"
                ) from exc
            found[str(key)] = str(value)
    missing = sorted(wanted - set(found))
    if missing:
        raise RuntimeError(
            f"target config at {ref} is missing reviewer defaults: {', '.join(missing)}"
        )
    return found


def _split_models(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def _apply_contributor_review_env(defaults: dict[str, str]) -> None:
    """Force target-base shipped slots through OpenRouter with clean blocking semantics."""
    models = _split_models(defaults["OUROBOROS_REVIEW_MODELS"])
    scope_models = _split_models(defaults["OUROBOROS_SCOPE_REVIEW_MODELS"])
    explicit_non_openrouter = [
        model for model in [*models, *scope_models]
        if "::" in model and not model.startswith("openrouter::")
    ]
    if explicit_non_openrouter:
        raise RuntimeError(
            "target-base reviewer defaults explicitly select non-OpenRouter routes: "
            + ", ".join(explicit_non_openrouter)
        )
    for key, value in defaults.items():
        os.environ[key] = value
    os.environ["OUROBOROS_REVIEW_ENFORCEMENT"] = "blocking"
    os.environ["OUROBOROS_SCOPE_REVIEW_DEGRADED"] = "false"
    os.environ["OUROBOROS_OBSERVABILITY_KEEP_RAW"] = "0"
    os.environ["OUROBOROS_PRE_PUSH_TESTS"] = "1"
    os.environ["OUROBOROS_PREFLIGHT_DIFF_AWARE"] = "false"


def _require_contributor_budget() -> float:
    """Require an explicit finite USD ceiling before contributor API calls."""
    raw = str(os.environ.get("TOTAL_BUDGET", "") or "").strip()
    if not raw:
        raise RuntimeError(
            "TOTAL_BUDGET is required for --contributor; set the USD ceiling "
            "you explicitly authorize for this review run"
        )
    try:
        budget = float(raw)
    except ValueError as exc:
        raise RuntimeError("TOTAL_BUDGET must be a positive finite number") from exc
    if not math.isfinite(budget) or budget <= 0:
        raise RuntimeError("TOTAL_BUDGET must be a positive finite number")
    return budget


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_file_at_ref(ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout if result.returncode == 0 else None


def _release_carrier_projection(ref: str) -> dict[str, str]:
    """Extract release-only values without executing code from either revision."""
    projection: dict[str, str] = {}

    version = _git_file_at_ref(ref, "VERSION")
    if version is not None:
        projection["VERSION"] = version.strip()

    pyproject = _git_file_at_ref(ref, "pyproject.toml")
    if pyproject is not None:
        project_match = re.search(
            r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)",
            pyproject,
        )
        version_match = re.search(
            r'(?m)^version\s*=\s*"([^"]+)"',
            project_match.group(1) if project_match else "",
        )
        if version_match:
            projection["pyproject.project.version"] = version_match.group(1)

    package = _git_file_at_ref(ref, "web/package.json")
    if package is not None:
        try:
            package_version = str((json.loads(package) or {}).get("version") or "")
        except Exception:
            package_version = "<invalid-json>"
        if package_version:
            projection["web.package.version"] = package_version

    api_types = _git_file_at_ref(ref, "web/modules/api_types.js")
    if api_types is not None:
        match = re.search(
            r"GATEWAY_CONTRACT_VERSION\s*=\s*['\"]([^'\"]+)['\"]",
            api_types,
        )
        if match:
            projection["gateway.contract.version"] = match.group(1)

    readme = _git_file_at_ref(ref, "README.md")
    if readme is not None:
        badge = re.search(r"\[!\[Version\s+([^\]]+)\]", readme)
        if badge:
            projection["readme.badge.version"] = badge.group(1)
        history = readme.split("## Version History", 1)
        if len(history) == 2:
            row = re.search(r"(?m)^\|\s*\d+\.\d+\.\d+[^\n]*$", history[1])
            if row:
                projection["readme.latest_history_row"] = row.group(0).strip()

    architecture = _git_file_at_ref(ref, "docs/ARCHITECTURE.md")
    if architecture is not None:
        header = re.search(r"(?m)^# Ouroboros v([^\s]+)", architecture)
        if header:
            projection["architecture.header.version"] = header.group(1)

    return projection


def _release_sensitive_changes(
    base_sha: str,
    head_sha: str,
    changed_paths: list[str],
) -> dict:
    base_projection = _release_carrier_projection(base_sha)
    head_projection = _release_carrier_projection(head_sha)
    fields = sorted(
        key
        for key in set(base_projection) | set(head_projection)
        if base_projection.get(key) != head_projection.get(key)
    )
    machinery = sorted(set(changed_paths) & _RELEASE_MACHINERY_PATHS)
    return {
        "changed": bool(fields or machinery),
        "carrier_fields": fields,
        "machinery_paths": machinery,
    }


def _contributor_snapshot(base_ref: str, head_ref: str) -> dict:
    """Resolve a clean, exact committed PR proposal whose target tip is its parent."""
    base_sha = _git_text(["rev-parse", f"{base_ref}^{{commit}}"]).strip()
    head_sha = _git_text(["rev-parse", f"{head_ref}^{{commit}}"]).strip()
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_sha, head_sha],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if ancestry.returncode != 0:
        raise RuntimeError(
            f"{base_ref} ({base_sha[:12]}) is not an ancestor of {head_ref} "
            f"({head_sha[:12]}). Fetch and rebase the PR onto current {base_ref}."
        )
    dirty = _git_text(["status", "--porcelain"])
    if dirty.strip():
        raise RuntimeError(
            "the contributor worktree is not clean; commit the intended PR "
            "snapshot before review"
        )
    patch = _git_bytes(["diff", "--binary", "--no-ext-diff", f"{base_sha}..{head_sha}"])
    if not patch.strip():
        raise RuntimeError("the contributor diff is empty")
    changed_paths = [
        line.strip()
        for line in _git_text(["diff", "--name-only", f"{base_sha}..{head_sha}"]).splitlines()
        if line.strip()
    ]
    if "VERSION" in changed_paths:
        raise RuntimeError(
            "contributor proposals must not bump VERSION; maintainers allocate "
            "collision-free release metadata on the final squash landing"
        )
    target_version = _git_text(["show", f"{base_sha}:VERSION"]).strip()
    target_config = _git_bytes(["show", f"{base_sha}:ouroboros/config.py"])
    base_script = _git_bytes(["show", f"{base_sha}:scripts/run_external_review.py"])
    head_script = _git_bytes(["show", f"{head_sha}:scripts/run_external_review.py"])
    substrate_changed = sorted(set(changed_paths) & _REVIEW_SUBSTRATE_PATHS)
    release_sensitive = _release_sensitive_changes(base_sha, head_sha, changed_paths)
    return {
        "base_ref": base_ref,
        "base_sha": base_sha,
        "merge_base_sha": _git_text(["merge-base", base_sha, head_sha]).strip(),
        "head_ref": head_ref,
        "head_sha": head_sha,
        "head_tree_sha": _git_text(["rev-parse", f"{head_sha}^{{tree}}"]).strip(),
        "target_version": target_version,
        "target_config_sha256": _hash_bytes(target_config),
        "patch": patch.decode("utf-8", errors="surrogateescape"),
        "diff_sha256": _hash_bytes(patch),
        "changed_paths": changed_paths,
        "review_substrate_changed": substrate_changed,
        "base_script_sha256": _hash_bytes(base_script),
        "head_script_sha256": _hash_bytes(head_script),
        "review_substrate_matches_base": not substrate_changed,
        "release_sensitive_changes": release_sensitive,
        "release_metadata_or_machinery_changed": release_sensitive["changed"],
    }


def _openrouter_pool() -> list[tuple[str, str]]:
    """Named OpenRouter candidates: env/settings first, pool order, hope* last."""
    pool: list[tuple[str, str]] = []
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key:
        pool.append(("<env/settings>", env_key))
    f1 = _keys_file()
    if f1 is not None:
        for line in f1.read_text(encoding="utf-8").splitlines():
            match = re.match(
                r"^\s*([A-Za-z0-9_.-]*openrouter[A-Za-z0-9_.-]*)\s*:\s*(\S+)\s*$", line, re.I
            )
            if match and match.group(2) not in {token for _, token in pool}:
                pool.append((match.group(1), match.group(2)))
    return sorted(pool, key=lambda item: "hope" in item[0].lower())


def _probe_model_for_key(token: str, model: str) -> tuple[bool, str]:
    """One-token completion on the EXACT reviewer model.

    `limit_remaining` alone is documented to lie (a ToS-blocked or nearly
    drained key passes it and then 403s/starves the real panel), so a key is
    healthy only after the actual model answered through it.
    """
    try:
        import httpx

        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=60,
        )
    except Exception as exc:
        return False, f"model_probe_error:{type(exc).__name__}"
    if response.status_code == 200:
        try:
            body = response.json() or {}
        except Exception:
            return False, "model_probe_unreadable"
        # OpenRouter passes provider errors through an HTTP-200 body.
        if isinstance(body.get("error"), dict):
            return False, f"model_probe_body_{body['error'].get('code') or 'error'}"
        return True, f"model_ok({model})"
    return False, f"model_probe_http_{response.status_code}"


def _review_probe_models() -> list[str]:
    try:
        from ouroboros.config import get_review_models, get_scope_review_models

        ordered = [*get_review_models(), *get_scope_review_models()]
        return list(dict.fromkeys(str(model) for model in ordered if str(model).strip()))
    except Exception:
        return []


def _openrouter_key_health(
    token: str,
    *,
    probe_all_models: bool = False,
) -> tuple[bool, str]:
    """Probe `limit_remaining`, then the exact reviewer model. (healthy, detail)."""
    try:
        import httpx

        response = httpx.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except Exception as exc:
        return False, f"probe_error:{type(exc).__name__}"
    if response.status_code == 403:
        return False, "forbidden_tos"
    if response.status_code != 200:
        return False, f"http_{response.status_code}"
    try:
        data = (response.json() or {}).get("data") or {}
    except Exception:
        return False, "unreadable_body"
    if data.get("limit") is not None:
        try:
            remaining = float(data.get("limit_remaining"))
        except (TypeError, ValueError):
            return False, "unreadable_limit"
        if remaining < _OPENROUTER_MIN_REMAINING_USD:
            return False, f"remaining_below_${_OPENROUTER_MIN_REMAINING_USD:g}"
    probe_models = _review_probe_models()
    if not probe_models:
        return True, "limit_ok_no_probe_model"
    if not probe_all_models:
        probe_models = probe_models[:1]
    details: list[str] = []
    for model in probe_models:
        healthy, detail = _probe_model_for_key(token, model)
        details.append(detail)
        if not healthy:
            return False, ";".join(details)
    return True, ";".join(details)


def _select_healthy_openrouter_key(
    *,
    required: bool = False,
    probe_all_models: bool = False,
) -> bool:
    """Pick the first healthy key from the allowed pool (values never printed)."""
    pool = _openrouter_pool()
    if not pool:
        message = "no OpenRouter key candidates found"
        if required:
            raise RuntimeError(message)
        print(f"WARN: {message}.", file=sys.stderr)
        return False
    for name, token in pool:
        healthy, detail = _openrouter_key_health(
            token,
            probe_all_models=probe_all_models,
        )
        print(f"OpenRouter key {name!r}: {detail}", file=sys.stderr)
        if healthy:
            os.environ["OPENROUTER_API_KEY"] = token
            return True
    message = (
        "no healthy OpenRouter key in the allowed pool; fix keys and rerun "
        "(exit 3 class)"
    )
    if required:
        raise RuntimeError(message)
    print(f"WARN: {message}.", file=sys.stderr)
    return False


def _assert_contributor_openrouter_config(
    resolved_config: dict,
    expected_defaults: dict[str, str],
) -> None:
    """Fail closed unless every resolved contributor actor still routes via OpenRouter."""
    from ouroboros.provider_models import provider_for_model

    models = [
        *list(resolved_config.get("triad_models") or []),
        *list(resolved_config.get("scope_models") or []),
    ]
    non_openrouter = [model for model in models if provider_for_model(str(model)) != "openrouter"]
    if not models or non_openrouter:
        detail = ", ".join(str(model) for model in non_openrouter) or "no resolved actors"
        raise RuntimeError(
            "contributor review actors did not resolve exclusively through OpenRouter: "
            + detail
        )
    expected = {
        "triad_models": _split_models(expected_defaults["OUROBOROS_REVIEW_MODELS"]),
        "scope_models": _split_models(
            expected_defaults["OUROBOROS_SCOPE_REVIEW_MODELS"]
        ),
        "triad_effort": expected_defaults["OUROBOROS_EFFORT_REVIEW"],
        "scope_effort": expected_defaults["OUROBOROS_EFFORT_SCOPE_REVIEW"],
        "scope_review_floor": expected_defaults["OUROBOROS_SCOPE_REVIEW_FLOOR"],
    }
    drift = {
        key: {"expected": value, "resolved": resolved_config.get(key)}
        for key, value in expected.items()
        if resolved_config.get(key) != value
    }
    if drift:
        raise RuntimeError(
            "resolved contributor actors/efforts drifted from target-base defaults: "
            + json.dumps(drift, ensure_ascii=False, sort_keys=True)
        )


def _create_isolated_checkout(
    staged_patch: str,
    *,
    base_commit: str = "HEAD",
) -> tuple[pathlib.Path, pathlib.Path]:
    """Detached worktree at *base_commit* with the proposed diff staged.

    The review then reads a frozen tree: edits in the primary worktree during
    the run cannot change what the reviewers see.
    """
    checkout_root = pathlib.Path(tempfile.mkdtemp(prefix="ouroboros-review-checkout-"))
    checkout = checkout_root / "repo"
    add = subprocess.run(
        ["git", "worktree", "add", "--detach", str(checkout), base_commit],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
    )
    if add.returncode != 0:
        raise RuntimeError(f"worktree add failed: {add.stderr.strip()}")
    if staged_patch.strip():
        apply = subprocess.run(
            ["git", "apply", "--index", "--whitespace=nowarn", "--binary"],
            cwd=str(checkout), input=staged_patch, capture_output=True, text=True, timeout=120,
        )
        if apply.returncode != 0:
            raise RuntimeError(
                f"staged diff did not apply to the isolated checkout: {apply.stderr.strip()}"
            )
    return checkout_root, checkout


def _remove_isolated_checkout(checkout_root: pathlib.Path, checkout: pathlib.Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(checkout)],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
    )
    shutil.rmtree(checkout_root, ignore_errors=True)


def _actor_records(ctx: object) -> list[dict]:
    """Return physical reviewer actor records without double-counting summaries."""
    actors = [
        dict(item)
        for item in (getattr(ctx, "_last_triad_raw_results", []) or [])
        if isinstance(item, dict)
    ]
    scope_raw = getattr(ctx, "_last_scope_raw_result", {}) or {}
    if isinstance(scope_raw, dict) and isinstance(scope_raw.get("raw_results"), list):
        actors.extend(dict(item) for item in scope_raw["raw_results"] if isinstance(item, dict))
    elif isinstance(scope_raw, dict) and any(
        key in scope_raw for key in ("slot", "slot_id", "prompt_ref", "response_ref")
    ):
        actors.append(dict(scope_raw))
    return actors


def _review_evidence_and_cost(ctx: object) -> tuple[list[dict], dict]:
    """Build a neutral actor-level evidence/cost report.

    A zero/missing actor cost is never presented as proof that the call was free.
    It is reported as unreported whenever the actor has usage or durable call refs.
    """
    evidence: list[dict] = []
    reported_cost = 0.0
    reported_slots: list[str] = []
    unreported_slots: list[str] = []
    for idx, actor in enumerate(_actor_records(ctx), start=1):
        slot = str(actor.get("slot_id") or actor.get("slot") or f"actor_{idx}")
        prompt_ref = actor.get("prompt_ref") or {}
        response_ref = actor.get("response_ref") or {}
        evidence.append({
            "slot": slot,
            "model_id": str(actor.get("model_id") or actor.get("model") or ""),
            "status": str(actor.get("status") or ""),
            "prompt_ref": prompt_ref,
            "response_ref": response_ref,
        })
        try:
            cost = float(actor.get("cost_usd"))
        except (TypeError, ValueError):
            cost = 0.0
        if cost > 0:
            reported_cost += cost
            reported_slots.append(slot)
        elif (
            int(actor.get("tokens_in") or 0) > 0
            or int(actor.get("tokens_out") or 0) > 0
            or bool(prompt_ref)
            or bool(response_ref)
        ):
            unreported_slots.append(slot)
    return evidence, {
        "reported_actor_cost_usd": round(reported_cost, 8),
        "reported_cost_slots": reported_slots,
        "unreported_or_unknown_cost_slots": unreported_slots,
        "note": (
            "Actor-reported cost only; unreported/unknown slots are not treated as $0. "
            "The core usage ledger remains the monetary authority."
        ),
    }


def _resolved_review_config(*, profile: str = "production_commit_gate") -> dict:
    """Return resolved review slots and efforts after settings/env loading."""
    from ouroboros.config import (
        get_review_enforcement,
        get_review_models,
        get_scope_review_floor,
        get_scope_review_models,
        resolve_effort,
    )

    return {
        "profile": profile,
        "provider": "openrouter" if profile == _CONTRIBUTOR_PROFILE else "resolved_per_model",
        "triad_models": get_review_models(),
        "triad_effort": resolve_effort("review"),
        "scope_models": get_scope_review_models(),
        "scope_effort": resolve_effort("scope_review"),
        "review_enforcement": get_review_enforcement(),
        "scope_review_floor": get_scope_review_floor(),
        "runtime_mode": os.environ.get("OUROBOROS_RUNTIME_MODE", ""),
    }


def _classify_exit(outcome: dict) -> int:
    if str(outcome.get("status") or "") == "passed":
        return 0
    block_reason = str(outcome.get("block_reason") or "")
    if block_reason in _GENUINE_BLOCK_REASONS:
        return 1
    # A scope CRITICAL with concrete findings is a genuine reviewer verdict
    # even when the triad passed; a findings-less scope block is fail-closed
    # infrastructure (crash, oversized prompt, sub-floor context).
    if block_reason == "scope_blocked" and outcome.get("combined_findings"):
        return 1
    return 3


def _apply_contributor_landing_obligations(
    outcome: dict,
    *,
    release_sensitive: bool = False,
) -> dict:
    """Defer only the two typed P9 landing items in contributor readiness mode."""
    if release_sensitive:
        return outcome
    if str(outcome.get("status") or "") != "blocked":
        return outcome
    # Only a triad findings-only block is eligible. ``scope_blocked`` may mean
    # the scope actor failed to produce an authoritative verdict; demoting it
    # here would fabricate readiness without the required scope evidence.
    if str(outcome.get("block_reason") or "") != "critical_findings":
        return outcome
    findings = [
        dict(item)
        for item in (outcome.get("combined_findings") or [])
        if isinstance(item, dict)
    ]
    if not findings:
        return outcome
    item_ids = {str(item.get("item") or "") for item in findings}
    if not item_ids or not item_ids.issubset(_CONTRIBUTOR_LANDING_OBLIGATION_ITEMS):
        return outcome
    return {
        "status": "passed",
        "message": (
            "Contributor readiness passed with release metadata deferred to the "
            "maintainer-owned final squash landing."
        ),
        "block_reason": "",
        "pre_fingerprint": outcome.get("pre_fingerprint", {}),
        "post_fingerprint": outcome.get("post_fingerprint", {}),
        "landing_obligations": findings,
        "original_block_reason": outcome.get("block_reason", ""),
    }


def _replace_public_paths(value, replacements: list[tuple[str, str]]):
    if isinstance(value, dict):
        return {
            str(key): _replace_public_paths(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_public_paths(item, replacements) for item in value]
    if isinstance(value, tuple):
        return [_replace_public_paths(item, replacements) for item in value]
    if isinstance(value, str):
        result = value
        for raw, replacement in replacements:
            if raw:
                result = result.replace(raw, replacement)
        return result
    return value


def _public_projection(value, *, replacements: list[tuple[str, str]]):
    """Apply the runtime secret scrubber and remove machine-local path prefixes."""
    from ouroboros.observability import redact_projection

    redacted = redact_projection(value).value
    return _replace_public_paths(redacted, replacements)


def _contributor_result(exit_code: int, snapshot: dict) -> str:
    if snapshot.get("review_substrate_changed"):
        return "INCOMPLETE_MAINTAINER_TRUSTED_BASE_RERUN_REQUIRED"
    if exit_code != 0:
        return "BLOCKED" if exit_code == 1 else "INCOMPLETE"
    return "READY_FOR_INTEGRATION"


def _write_contributor_packet(
    *,
    output_dir: pathlib.Path,
    snapshot: dict,
    resolved_config: dict,
    outcome: dict,
    exit_code: int,
    evidence_refs: list[dict],
    cost_report: dict,
    elapsed_sec: float,
    triad_raw,
    scope_raw,
    degraded_reasons: list[str],
    replacements: list[tuple[str, str]],
) -> pathlib.Path:
    result = _contributor_result(exit_code, snapshot)
    public_snapshot = {
        key: value
        for key, value in snapshot.items()
        if key != "patch"
    }
    evidence = {
        "schema_version": 1,
        "review_profile": _CONTRIBUTOR_PROFILE,
        "result": result,
        "complete": exit_code == 0,
        "exit_code": exit_code,
        "exit_class": {
            0: "passed",
            1: "genuine_review_block",
            3: "infrastructure",
        }.get(exit_code, "unknown"),
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "snapshot": public_snapshot,
        "review_config": resolved_config,
        "review_completeness": {
            "contract": "production_triad_quorum_plus_authoritative_scope",
            "degraded_reasons": list(degraded_reasons),
        },
        "advisory": {
            "included": False,
            "reason": "excluded_by_external_pr_readiness_profile",
        },
        "release_metadata": {
            "contributor_version_bump_required": False,
            "owner": "maintainer_final_squash_landing",
            "final_production_review_required": True,
        },
        "trust": {
            "target_base_defaults_used": True,
            "review_substrate_changed": snapshot.get("review_substrate_changed", []),
            "maintainer_trusted_base_rerun_required": bool(
                snapshot.get("review_substrate_changed")
            ),
            "note": (
                "Local contributor evidence accelerates triage but is not merge "
                "authorization or cryptographic proof of execution."
            ),
        },
        "production_outcome": outcome,
        "raw_evidence_refs": evidence_refs,
        "cost_report": cost_report,
        "elapsed_sec": round(elapsed_sec, 1),
    }
    public_evidence = _public_projection(evidence, replacements=replacements)
    public_triad = _public_projection(triad_raw, replacements=replacements)
    public_scope = _public_projection(scope_raw, replacements=replacements)

    evidence_path = output_dir / "review-evidence.json"
    outcome_path = output_dir / "outcome.json"
    full_output_path = output_dir / "full-output.txt"
    evidence_path.write_text(
        json.dumps(public_evidence, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    outcome_path.write_text(
        json.dumps(
            _public_projection(
                {"exit_code": exit_code, "outcome": outcome},
                replacements=replacements,
            ),
            indent=2,
            ensure_ascii=False,
            default=str,
        ) + "\n",
        encoding="utf-8",
    )
    sep = "=" * 80
    full_output = "\n".join([
        sep, "CONTRIBUTOR REVIEW EVIDENCE", sep,
        json.dumps(public_evidence, indent=2, ensure_ascii=False, default=str),
        sep, "TRIAD RAW RESULTS (full, redacted)", sep,
        json.dumps(public_triad, indent=2, ensure_ascii=False, default=str),
        sep, "SCOPE RAW RESULT (full, redacted)", sep,
        json.dumps(public_scope, indent=2, ensure_ascii=False, default=str),
    ])
    full_output_path.write_text(full_output + "\n", encoding="utf-8")
    packet_path = output_dir / "review-packet.zip"
    with zipfile.ZipFile(packet_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in (evidence_path, outcome_path, full_output_path):
            archive.write(path, arcname=path.name)
    return packet_path


def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Production staged-tree review dry-run, or --contributor committed "
            "PR-readiness review (no commit in either lane)."
        )
    )
    parser.add_argument(
        "commit_message",
        nargs="?",
        default="",
    )
    parser.add_argument(
        "--output",
        default="",
        help=(
            "Directory for full review artifacts. Defaults to a new append-only "
            "run directory under ~/ouro/review_runs/."
        ),
    )
    parser.add_argument(
        "--drive-root",
        default=os.environ.get("OUROBOROS_REVIEW_DRIVE_ROOT", ""),
        help=(
            "Drive root for review observability writes. Defaults to a new persistent "
            "temporary directory, never the live data root."
        ),
    )
    parser.add_argument(
        "--goal",
        default=os.environ.get("REVIEW_GOAL", ""),
        help="Owner-approved goal. Defaults to a neutral current-release goal.",
    )
    parser.add_argument(
        "--scope",
        default=os.environ.get("REVIEW_SCOPE", ""),
        help="Owner-approved scope. Defaults to staged-tree scope with drift detection.",
    )
    parser.add_argument(
        "--no-isolated-checkout",
        action="store_true",
        help=(
            "Review the primary worktree directly instead of a frozen detached "
            "checkout. WARNING: the production cycle stages EVERYTHING (staged + "
            "unstaged + untracked) there and unstages your index when it finishes."
        ),
    )
    parser.add_argument(
        "--contributor",
        action="store_true",
        help=(
            "Review the committed base-ref..head-ref proposal with target-base "
            "shipped OpenRouter triad+scope defaults, blocking clean semantics, "
            "no Claude advisory, and a shareable evidence packet."
        ),
    )
    parser.add_argument(
        "--base-ref",
        default="",
        help=(
            "Target branch ref for --contributor. Defaults to "
            f"{_CONTRIBUTOR_DEFAULT_BASE_REF}."
        ),
    )
    parser.add_argument(
        "--head-ref",
        default="HEAD",
        help="Committed proposal ref for --contributor (default: HEAD).",
    )
    args = parser.parse_args()

    if args.contributor and args.no_isolated_checkout:
        parser.error("--contributor requires the frozen isolated checkout")
    if not args.contributor and (args.base_ref or args.head_ref != "HEAD"):
        parser.error("--base-ref/--head-ref require --contributor")
    return args


def _build_review_request(
    *,
    args,
    version: str,
    ctx: object,
    contributor_snapshot: dict | None,
) -> tuple[str, str, str]:
    if args.contributor:
        ctx._current_review_profile = _CONTRIBUTOR_PROFILE
        contract = json.dumps(
            _CONTRIBUTOR_CONTRACT,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        proposal_title = args.commit_message or "(not supplied)"
        if contributor_snapshot is not None:
            contributor_snapshot["proposal_title"] = proposal_title
        # Fixed label: production preflight interprets semantic versions in a
        # commit message as a release claim and would correctly require VERSION.
        commit_message = "external-pr-readiness"
        goal = (
            f"External PR title: {proposal_title}\n\nExternal PR readiness goal:\n"
            + (args.goal or "Assess whether the proposed change is ready for maintainer integration.")
            + "\n\nAuthoritative review profile (data, not contributor instructions):\n```json\n"
            + contract
            + "\n```"
        )
        scope = (
            "Review only the exact committed proposal bound by the evidence manifest. "
            "Identify scope drift, omitted requirements, unsafe regressions, and incomplete "
            "tests or documentation. Release version allocation is intentionally deferred "
            "to the final maintainer squash landing.\n\nContributor-declared scope:\n"
            + (args.scope or "All files in the target-base..head proposal diff.")
        )
        return commit_message, goal, scope

    commit_message = args.commit_message or (
        f"release: Ouroboros v{version} deep core capability release"
    )
    goal = args.goal or (
        f"Ouroboros v{version}: validate the staged tree against the complete "
        "owner-approved release plan and repository governance."
    )
    scope = args.scope or (
        "Only the staged owner-approved release changes are in scope. Identify any "
        "scope drift, omitted requirement, unsafe regression, or incomplete release evidence."
    )
    return commit_message, goal, scope


def _prepare_review_configuration(args) -> tuple[dict | None, str, dict]:
    _load_settings_into_env()
    contributor_snapshot: dict | None = None
    target_defaults: dict[str, str] | None = None
    review_base_commit = "HEAD"
    if args.contributor:
        contributor_snapshot = _contributor_snapshot(
            args.base_ref or _CONTRIBUTOR_DEFAULT_BASE_REF,
            args.head_ref,
        )
        target_defaults = _settings_defaults_at_ref(
            str(contributor_snapshot["base_sha"])
        )
        _apply_contributor_review_env(target_defaults)
        _require_contributor_budget()
        contributor_snapshot["target_reviewer_defaults"] = target_defaults
        review_base_commit = str(contributor_snapshot["base_sha"])

    _select_healthy_openrouter_key(
        required=args.contributor,
        probe_all_models=args.contributor,
    )
    resolved_config = _resolved_review_config(
        profile=_CONTRIBUTOR_PROFILE if args.contributor else "production_commit_gate"
    )
    if args.contributor:
        if target_defaults is None:
            raise RuntimeError("target-base reviewer defaults were not resolved")
        _assert_contributor_openrouter_config(resolved_config, target_defaults)
    return contributor_snapshot, review_base_commit, resolved_config


def main() -> int:
    version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    args = _parse_args()

    try:
        contributor_snapshot, review_base_commit, resolved_config = (
            _prepare_review_configuration(args)
        )
    except Exception as exc:
        print(f"ERROR: review configuration preflight failed: {exc}", file=sys.stderr)
        return 3
    print(
        "Resolved review config: "
        + json.dumps(resolved_config, ensure_ascii=False),
        file=sys.stderr,
    )

    staged = (
        str(contributor_snapshot["patch"])
        if contributor_snapshot is not None
        else subprocess.run(
            ["git", "diff", "--cached", "--binary"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
        ).stdout
    )
    if not staged.strip():
        message = (
            "ERROR: contributor diff is empty."
            if args.contributor
            else "ERROR: staged diff is empty — `git add` the changes first."
        )
        print(message, file=sys.stderr)
        return 2

    from ouroboros.tools.claude_advisory_review import _MAX_DIFF_CHARS_ERROR

    if len(staged) > _MAX_DIFF_CHARS_ERROR:
        print(
            f"ERROR: staged diff is {len(staged):,} chars — over the advisory hard cap "
            f"({_MAX_DIFF_CHARS_ERROR:,}). Policy: split the phase into smaller "
            "single-intent commits instead of relaxing the gate.",
            file=sys.stderr,
        )
        return 3

    sha8 = (
        str(contributor_snapshot["head_sha"])[:8]
        if contributor_snapshot is not None
        else subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
        ).stdout.strip() or "nohead"
    )
    output_dir = pathlib.Path(
        args.output
        or pathlib.Path.home()
        / "ouro"
        / "review_runs"
        / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{sha8}"
    ).expanduser().resolve(strict=False)
    output_dir.mkdir(parents=True, exist_ok=True)

    from ouroboros.tools.registry import ToolContext

    review_drive_root = (
        pathlib.Path(args.drive_root).expanduser().resolve(strict=False)
        if args.drive_root
        else pathlib.Path(tempfile.mkdtemp(prefix="ouroboros-external-review-"))
    )
    review_drive_root.mkdir(parents=True, exist_ok=True)
    (review_drive_root / "logs").mkdir(parents=True, exist_ok=True)

    checkout_root: pathlib.Path | None = None
    checkout: pathlib.Path | None = None
    repo_for_review = REPO
    if not args.no_isolated_checkout:
        try:
            checkout_root, checkout = _create_isolated_checkout(
                staged,
                base_commit=review_base_commit,
            )
            repo_for_review = checkout
            if contributor_snapshot is not None:
                applied_tree = _git_text(["write-tree"], cwd=checkout).strip()
                expected_tree = str(contributor_snapshot["head_tree_sha"])
                if applied_tree != expected_tree:
                    raise RuntimeError(
                        "applied contributor tree does not match head tree: "
                        f"applied={applied_tree}, head={expected_tree}"
                    )
                contributor_snapshot["reviewed_tree_sha"] = applied_tree
            print(f"Isolated review checkout: {checkout}", file=sys.stderr)
        except Exception as exc:
            print(f"ERROR: isolated checkout failed: {exc}", file=sys.stderr)
            if checkout_root is not None and checkout is not None:
                _remove_isolated_checkout(checkout_root, checkout)
            (output_dir / "outcome.json").write_text(
                json.dumps({
                    "exit_code": 3,
                    "outcome": {"status": "blocked", "block_reason": "isolated_checkout_failed"},
                }, indent=2) + "\n",
                encoding="utf-8",
            )
            return 3

    ctx = ToolContext(repo_dir=repo_for_review, drive_root=review_drive_root)
    commit_message, goal, scope = _build_review_request(
        args=args,
        version=version,
        ctx=ctx,
        contributor_snapshot=contributor_snapshot,
    )

    t0 = time.time()
    try:
        from ouroboros.tools.git import _run_non_committing_review_cycle

        if args.contributor:
            print(
                "Contributor profile: Claude advisory excluded; running hermetic "
                "test preflight followed by target-base OpenRouter triad + scope.",
                file=sys.stderr,
            )
        else:
            # The default operator lane runs the REAL advisory pre-review. Its
            # freshness state lives in this run's drive root, so the production
            # cycle below sees a fresh advisory.
            if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
                print(
                    "WARN: no ANTHROPIC_API_KEY — advisory will record an audited "
                    "bypass and the gate falls back to its hermetic pytest preflight.",
                    file=sys.stderr,
                )
            from ouroboros.tools.claude_advisory_review import _handle_advisory_pre_review

            advisory_text = _handle_advisory_pre_review(
                ctx, commit_message=commit_message, goal=goal, scope=scope,
            )
            (output_dir / "advisory.txt").write_text(
                str(advisory_text) + "\n", encoding="utf-8"
            )
            print(
                "=" * 80
                + "\nADVISORY PRE-REVIEW (full)\n"
                + "=" * 80
                + f"\n{advisory_text}"
            )

        outcome = _run_non_committing_review_cycle(
            ctx,
            commit_message,
            skip_advisory_review=args.contributor,
            goal=goal,
            scope=scope,
        )
        if args.contributor:
            outcome = _apply_contributor_landing_obligations(
                outcome,
                release_sensitive=bool(
                    contributor_snapshot.get(
                        "release_metadata_or_machinery_changed",
                        False,
                    )
                ),
            )
        if checkout is not None:
            # The cycle may auto-sync release metadata (version carriers) in the
            # checkout; a drifted tree means reviewers approved MORE than the
            # operator's staged patch — surface that loudly. The cycle's final
            # ``git reset HEAD`` turns NEW files untracked, and ``git diff HEAD``
            # would not show them — re-stage everything so the comparison is
            # homogeneous with the operator's staged patch.
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(checkout), capture_output=True, text=True, timeout=120,
            )
            post_tree = subprocess.run(
                ["git", "diff", "--cached", "--binary"],
                cwd=str(checkout), capture_output=True, text=True, timeout=120,
            ).stdout
            if post_tree.strip() != staged.strip():
                print(
                    "WARN: the reviewed checkout tree drifted from the staged "
                    "patch (release-metadata auto-sync?). Reconcile the primary "
                    "worktree before committing what was reviewed.",
                    file=sys.stderr,
                )
                (output_dir / "reviewed-tree-drift.diff").write_text(
                    post_tree, encoding="utf-8",
                )
                if args.contributor:
                    outcome = {
                        "status": "blocked",
                        "message": "Contributor review tree drifted during the review cycle.",
                        "block_reason": "reviewed_tree_drift",
                    }
    finally:
        if checkout_root is not None and checkout is not None:
            _remove_isolated_checkout(checkout_root, checkout)

    evidence_refs, cost_report = _review_evidence_and_cost(ctx)
    exit_code = _classify_exit(outcome)
    if (
        contributor_snapshot is not None
        and contributor_snapshot.get("review_substrate_changed")
        and exit_code == 0
    ):
        exit_code = 3
        outcome = {
            **outcome,
            "status": "blocked",
            "block_reason": "trusted_base_rerun_required",
            "message": (
                "The proposal changes the review substrate. Its local result is "
                "preserved, but fast-path readiness requires a maintainer rerun "
                "from the trusted target-base implementation."
            ),
        }

    if contributor_snapshot is not None:
        replacements = sorted(
            [
                (str(checkout or ""), "$REVIEW_CHECKOUT"),
                (str(review_drive_root), "$REVIEW_DRIVE"),
                (str(REPO), "$REPO"),
                (str(pathlib.Path.home()), "$HOME"),
            ],
            key=lambda item: len(item[0]),
            reverse=True,
        )
        packet_path = _write_contributor_packet(
            output_dir=output_dir,
            snapshot=contributor_snapshot,
            resolved_config=resolved_config,
            outcome=outcome,
            exit_code=exit_code,
            evidence_refs=evidence_refs,
            cost_report=cost_report,
            elapsed_sec=time.time() - t0,
            triad_raw=getattr(ctx, "_last_triad_raw_results", []),
            scope_raw=getattr(ctx, "_last_scope_raw_result", {}),
            degraded_reasons=list(
                getattr(ctx, "_review_degraded_reasons", []) or []
            ),
            replacements=replacements,
        )
        print((output_dir / "full-output.txt").read_text(encoding="utf-8"))
        print(f"Artifacts: {output_dir}", file=sys.stderr)
        print(f"Shareable packet: {packet_path}", file=sys.stderr)
        return exit_code

    sep = "=" * 80
    out = "\n".join([
        sep, "RESOLVED REVIEW CONFIG", sep,
        json.dumps({**resolved_config, "drive_root": str(review_drive_root)}, indent=2, ensure_ascii=False, default=str),
        sep, "TRIAD RAW RESULTS (full, untruncated)", sep,
        json.dumps(getattr(ctx, "_last_triad_raw_results", []), indent=2, ensure_ascii=False, default=str),
        sep, "SCOPE RAW RESULT (full, untruncated)", sep,
        json.dumps(getattr(ctx, "_last_scope_raw_result", {}), indent=2, ensure_ascii=False, default=str),
        sep, "AGGREGATE VERDICT", sep,
        json.dumps({
            "complete": exit_code == 0,
            "exit_code": exit_code,
            "exit_class": {
                0: "passed",
                1: "genuine_review_block",
                3: "infrastructure",
            }.get(exit_code, "unknown"),
            "production_outcome": outcome,
            "scope_model": getattr(ctx, "_last_scope_model", ""),
            "raw_evidence_refs": evidence_refs,
            "cost_report": cost_report,
            "elapsed_sec": round(time.time() - t0, 1),
        }, indent=2, ensure_ascii=False, default=str),
    ])
    print(out)
    (output_dir / "full-output.txt").write_text(out + "\n", encoding="utf-8")
    (output_dir / "outcome.json").write_text(
        json.dumps(
            {"exit_code": exit_code, "outcome": outcome},
            indent=2, ensure_ascii=False, default=str,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"Artifacts: {output_dir}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        # An uncaught crash is infrastructure, never a reviewer verdict.
        sys.exit(3)
