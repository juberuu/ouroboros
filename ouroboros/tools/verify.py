"""verify_and_record — the host runs the agent's declared verification check and
writes a durable, host-attested receipt (FR3 verify-before-done).

One call runs the check AND attests the result, so it replaces the run the agent
would have done anyway (≈ zero extra rounds). The contract KIND is agent-declared
(LLM-first, P5 — the host never infers from prose whether a machine-checkable
contract exists); the host only executes and attests what it can. Receipts feed
the verification ledger and suppress the receipt_absent transparency flag.
"""

from __future__ import annotations

import pathlib
import subprocess
from typing import Any, List

from ouroboros.outcomes import append_verification_receipt
from ouroboros.platform_layer import bootstrap_process_path
from ouroboros.tools.registry import ToolContext, ToolEntry, active_repo_dir_for
from ouroboros.utils import utc_now_iso

# Durable receipt evidence is bounded but the truncation is DISCLOSED (BIBLE P1, never
# silent); the tool-result preview is bounded separately for transport.
_RECEIPT_OUTPUT_CAP = 20000
_TOOL_OUTPUT_CAP = 4000


def _bounded(text: Any, cap: int) -> str:
    t = str(text or "").strip()
    if len(t) <= cap:
        return t
    return t[:cap] + f"\n…[truncated {len(t) - cap} of {len(t)} chars]"

_CONTRACT_KINDS = (
    "visible_verifier",
    "explicit_command",
    "explicit_metric",
    "artifact_observation",
    "no_visible_machine_contract",
)
_RUN_KINDS = frozenset({"visible_verifier", "explicit_command", "explicit_metric"})


def _normalize_check(check: Any) -> List[str] | None:
    """A verification command as argv, or a shell one-liner wrapped via ``sh -lc``."""
    if isinstance(check, str):
        text = check.strip()
        return ["sh", "-lc", text] if text else None
    if isinstance(check, (list, tuple)):
        argv = [str(part) for part in check if str(part or "").strip()]
        return argv or None
    return None


def _observe_artifacts(ctx: ToolContext, artifact_paths: List[str]) -> tuple[bool, str]:
    """Read-only existence observation for declared deliverable paths. The RESOLVED
    path (whether the input was absolute or relative) must stay inside the active
    workspace, else clear the user_files guards (control-plane/secret and outside-home
    refused) — so a relative `../../etc/passwd` cannot probe arbitrary host files. Never
    reads content."""
    from ouroboros.tool_access import path_is_relative_to, user_files_path_block_reason

    active = pathlib.Path(active_repo_dir_for(ctx)).resolve(strict=False)
    missing: List[str] = []
    seen: List[str] = []
    for raw in artifact_paths:
        text = str(raw or "").strip()
        if not text:
            continue
        p = pathlib.Path(text)
        candidate = (p if p.is_absolute() else (active / text)).resolve(strict=False)
        # Confine the FINAL resolved path: inside the workspace is fine; anything else
        # must pass the user_files guards (refuses control-plane/secret + outside-home).
        within_active = candidate == active or path_is_relative_to(candidate, active)
        if not within_active and user_files_path_block_reason(ctx, candidate):
            return False, f"path refused (outside workspace / control-plane): {text}"
        seen.append(text)
        if not candidate.exists():
            missing.append(text)
    if not seen:
        return False, "no artifact_paths given"
    if missing:
        return False, f"missing: {', '.join(missing[:10])}"
    return True, f"observed {len(seen)} artifact(s): {', '.join(seen[:10])}"


def _verify_and_record(
    ctx: ToolContext,
    contract_kind: str = "",
    check: Any = None,
    expected: str = "",
    artifact_paths: Any = None,
    cwd: str = "",
    timeout_sec: int | None = None,
) -> str:
    kind = str(contract_kind or "").strip()
    if kind not in _CONTRACT_KINDS:
        return f"⚠️ TOOL_ARG_ERROR (verify_and_record): contract_kind must be one of {', '.join(_CONTRACT_KINDS)}."
    task_id = str(getattr(ctx, "task_id", "") or "")
    drive_root = getattr(ctx, "drive_root", None)
    expected_s = str(expected or "").strip()
    receipt: dict[str, Any] = {"tool": "verify_and_record", "contract_kind": kind, "expected": expected_s, "ts": utc_now_iso()}

    if kind in _RUN_KINDS:
        argv = _normalize_check(check)
        if not argv:
            return (
                f"⚠️ TOOL_ARG_ERROR (verify_and_record): contract_kind={kind} requires `check` "
                "(the verification command as argv list or a shell one-liner string)."
            )
        from ouroboros.tools.shell import (
            _RUN_SHELL_DEFAULT_TIMEOUT_SEC,
            _executor_can_run_cwd,
            _resolve_effective_timeout,
            _shell_env_for_cwd,
            _tracked_subprocess_run,
            resolve_shell_cwd,
        )
        from ouroboros.workspace_executor import execute as executor_execute

        try:
            work_dir, _cwd_root, _allowed = resolve_shell_cwd(ctx, cwd)
        except (OSError, ValueError) as exc:
            return f"⚠️ VERIFY_CWD_BLOCKED: check cwd escapes allowed roots: {exc}."
        timeout = _resolve_effective_timeout(_RUN_SHELL_DEFAULT_TIMEOUT_SEC, ctx, override_sec=timeout_sec)
        bootstrap_process_path()  # mirror run_command: ensure the check sees the full PATH
        try:
            if _executor_can_run_cwd(ctx, pathlib.Path(work_dir)):
                # Route the check through the host-owned executor backend (e.g. docker_exec
                # with NetworkMode=none) EXACTLY like run_command, so the verification runs
                # in the SAME place + isolation as the agent's other commands — not on the
                # host while the work lives in a container.
                res = executor_execute(ctx, argv, pathlib.Path(work_dir), timeout)
            else:
                run_env = _shell_env_for_cwd(ctx, pathlib.Path(work_dir))
                res = _tracked_subprocess_run(
                    argv, cwd=str(work_dir),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout,
                    **({"env": run_env} if run_env is not None else {}),
                )
        except subprocess.TimeoutExpired:
            receipt.update({"status": "fail", "returncode": None, "check": " ".join(argv), "summary": f"check timed out after {timeout}s"})
            append_verification_receipt(drive_root, task_id, receipt)
            return f"verify_and_record [{kind}] FAIL: check timed out after {timeout}s. Receipt recorded."
        # Full output captured in-handler BEFORE any transport truncation.
        out = (res.stdout or "") + (("\n" + res.stderr) if res.stderr else "")
        rc = res.returncode
        passed = (rc == 0) and (not expected_s or expected_s in out)
        receipt.update({"status": "pass" if passed else "fail", "returncode": rc, "check": " ".join(argv), "summary": _bounded(out, _RECEIPT_OUTPUT_CAP)})
        append_verification_receipt(drive_root, task_id, receipt)
        verdict = "PASS" if passed else "FAIL"
        exp_note = f" expected={expected_s!r}" if expected_s else ""
        return f"verify_and_record [{kind}] {verdict}: exit={rc}{exp_note}. Host-attested receipt recorded.\n\n{_bounded(out, _TOOL_OUTPUT_CAP)}"

    if kind == "artifact_observation":
        paths = [str(p) for p in (artifact_paths or []) if str(p or "").strip()]
        ok, detail = _observe_artifacts(ctx, paths)
        receipt.update({"status": "observed" if ok else "fail", "paths": paths[:20], "summary": detail})
        append_verification_receipt(drive_root, task_id, receipt)
        verdict = "OBSERVED" if ok else "FAIL"
        return f"verify_and_record [artifact_observation] {verdict}: {detail}. Host-attested receipt recorded."

    # no_visible_machine_contract: an honest escape hatch — no host run, the agent's
    # best proxy + residual risk is recorded as a receipt and judged by a reviewer.
    receipt.update({"status": "declared", "check": str(check or ""), "summary": (expected_s or str(check or ""))[:1000]})
    append_verification_receipt(drive_root, task_id, receipt)
    return (
        "verify_and_record [no_visible_machine_contract] DECLARED: no host-checkable contract; "
        "your stated proxy + residual risk recorded as a receipt for review."
    )


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("verify_and_record", {
            "name": "verify_and_record",
            "description": (
                "Verify your deliverable BEFORE claiming it is done, and record a durable host-attested "
                "receipt. The host RUNS your declared check and attests the result — one call replaces the "
                "verification run you would do anyway. Pick contract_kind: visible_verifier / explicit_command "
                "(run `check`, pass on exit 0 and, if given, `expected` substring present) · explicit_metric "
                "(run `check`, pass when the `expected` metric string appears) · artifact_observation (the host "
                "confirms the declared artifact_paths exist) · no_visible_machine_contract (honest escape hatch: "
                "no machine check exists; your best proxy + risk is recorded for review). Recording a receipt "
                "suppresses the receipt_absent transparency flag on a clean turn."
            ),
            "parameters": {"type": "object", "properties": {
                "contract_kind": {"type": "string", "enum": list(_CONTRACT_KINDS), "description": "How the deliverable is verifiable — you declare it (the host never guesses)."},
                "check": {"description": "The verification command: an argv list (['pytest','-q']) or a shell one-liner string. Required for visible_verifier/explicit_command/explicit_metric.", "type": ["array", "string"], "items": {"type": "string"}},
                "expected": {"type": "string", "default": "", "description": "Optional expected substring/metric in the check output (explicit_command/explicit_metric)."},
                "artifact_paths": {"type": "array", "items": {"type": "string"}, "description": "Deliverable paths the host confirms exist (artifact_observation)."},
                "cwd": {"type": "string", "default": "", "description": "Working directory for `check` (same roots as run_command)."},
                "timeout_sec": {"type": "integer", "description": "Optional check timeout override."},
            }, "required": ["contract_kind"]},
        }, _verify_and_record, is_code_tool=True, timeout_sec=900, mutates_worktree=True),
    ]
