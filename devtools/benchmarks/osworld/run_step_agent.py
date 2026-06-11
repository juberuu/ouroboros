#!/usr/bin/env python3
"""External OSWorld step-loop adapter backed by local Ouroboros.

Unlike ``run_installed_agent.py``, this runner does not install Ouroboros inside
the VM. It keeps the official OSWorld rhythm:

    observe VM -> ask Ouroboros for next action(s) -> env.step(action) -> repeat

Every action returned by Ouroboros is passed through ``env.step(...)`` and is
therefore visible in OSWorld's normal trajectory/action history. Screenshots are
saved under ``data/uploads`` so Ouroboros can inspect them with ``vlm_query``.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
import types
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.manifests import benchmark_run_manifest, write_json
from devtools.benchmarks.common.result_index import append_result_index, task_result_row
from devtools.benchmarks.common.run_roots import ensure_outside_repo


_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKSPACE_ROOT = _REPO_ROOT.parent

DEFAULT_OSWORLD_ROOT = os.environ.get("OSWORLD_ROOT", str(_WORKSPACE_ROOT / "OSWorld"))
DEFAULT_VM = os.environ.get("OSWORLD_VM", str(Path(DEFAULT_OSWORLD_ROOT) / "vmware_vm_data" / "Ubuntu0" / "Ubuntu0.vmx"))
DEFAULT_TASK = "evaluation_examples/examples/os/f9be0997-4b7c-45c5-b05c-4612b44a6118.json"
DEFAULT_REPO = str(_REPO_ROOT)
DEFAULT_DATA = os.environ.get("OUROBOROS_OSWORLD_DATA_DIR", str(_WORKSPACE_ROOT / "bench_runs" / "osworld_data"))
DEFAULT_SETTINGS = os.environ.get("OUROBOROS_SETTINGS_PATH", str(_WORKSPACE_ROOT / "data" / "settings.json"))
DEFAULT_OUROBOROS_BIN = os.environ.get("OUROBOROS_BIN", str(_REPO_ROOT / ".venv" / "bin" / "ouroboros"))
VMWARE_FUSION_PATHS = (
    "/Applications/VMware Fusion.app/Contents/Public",
    "/Applications/VMware Fusion.app/Contents/Library",
)
SPECIAL_ACTIONS = {"WAIT", "DONE", "FAIL"}


@dataclass
class StepAgentConfig:
    ouroboros_bin: str
    ouroboros_url: str
    repo_dir: Path
    data_dir: Path
    settings_path: Path
    result_dir: Path
    task_id: str
    model: str
    timeout_sec: int
    max_obs_chars: int
    screenshot_check_only: bool


@dataclass
class TaskRecordConfig:
    run_dir: Path
    result_root: Path
    repo_dir: Path
    settings_path: Path
    example_id: str
    domain: str
    reward: float | None
    steps: int
    status: str
    reason_code: str
    error: str = ""
    extra: dict[str, Any] | None = None


@dataclass
class PreflightConfig:
    osworld_root: Path
    task_path: Path
    path_to_vm: str
    repo_dir: Path
    data_dir: Path
    settings_path: Path
    result_root: Path
    ouroboros_url: str
    model: str


def _install_optional_dependency_stubs() -> None:
    """Avoid heavy optional evaluator imports when a selected task does not use them."""

    if "easyocr" not in sys.modules:
        easyocr = types.ModuleType("easyocr")

        class _UnavailableReader:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("easyocr is not installed; OCR metrics unavailable")

        easyocr.Reader = _UnavailableReader  # type: ignore[attr-defined]
        sys.modules["easyocr"] = easyocr

    if "fastdtw" not in sys.modules:
        fastdtw_mod = types.ModuleType("fastdtw")

        def _fastdtw_unavailable(*_args: Any, **_kwargs: Any) -> tuple[float, list[Any]]:
            raise RuntimeError("fastdtw is not installed; audio metrics unavailable")

        fastdtw_mod.fastdtw = _fastdtw_unavailable  # type: ignore[attr-defined]
        sys.modules["fastdtw"] = fastdtw_mod


def _ensure_vmrun_on_path() -> None:
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    changed = False
    for candidate in VMWARE_FUSION_PATHS:
        if Path(candidate, "vmrun").exists() and candidate not in path_parts:
            path_parts.insert(0, candidate)
            changed = True
    if changed:
        os.environ["PATH"] = os.pathsep.join(path_parts)


def _safe_slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-._")
    return cleaned[:80] or uuid.uuid4().hex[:8]


def _http_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip().startswith("{") else {"raw": raw}


def _preflight(config: PreflightConfig) -> dict[str, Any]:
    failures: list[str] = []
    details: dict[str, Any] = {}
    if not config.osworld_root.is_dir():
        failures.append(f"OSWorld checkout not found: {config.osworld_root}")
    if not (config.osworld_root / "evaluation_examples").exists():
        failures.append(f"OSWorld checkout shape not recognized: {config.osworld_root}")
    if not config.task_path.is_file():
        failures.append(f"task JSON not found: {config.task_path}")
    vm_path = Path(config.path_to_vm).expanduser()
    if not vm_path.exists():
        failures.append(f"VM path not found: {vm_path}")
    if not any((Path(path) / "vmrun").exists() for path in VMWARE_FUSION_PATHS):
        failures.append("vmrun not found in known VMware Fusion paths")
    if not config.repo_dir.is_dir() or not (config.repo_dir / "VERSION").exists():
        failures.append(f"Ouroboros repo shape not recognized: {config.repo_dir}")
    if not config.settings_path.is_file():
        failures.append(f"settings.json not found: {config.settings_path}")
    else:
        try:
            settings = json.loads(config.settings_path.read_text(encoding="utf-8"))
            selected_model = str(config.model or settings.get("OUROBOROS_MODEL") or "")
            details["model"] = selected_model
            from ouroboros.provider_models import PROVIDER_ENV_KEYS, provider_for_model

            provider = provider_for_model(selected_model)
            env_key = PROVIDER_ENV_KEYS.get(provider, "OPENROUTER_API_KEY")
            if not str(os.environ.get(env_key) or settings.get(env_key) or "").strip():
                failures.append(f"{env_key} missing for model provider {provider}")
        except Exception as exc:
            failures.append(f"settings.json unreadable: {type(exc).__name__}: {exc}")
    try:
        ensure_outside_repo(config.data_dir, config.repo_dir)
    except Exception as exc:
        failures.append(f"data dir must be outside repo/live data: {exc}")
    try:
        uploads = config.data_dir / "uploads" / "osworld" / "_preflight"
        uploads.mkdir(parents=True, exist_ok=True)
        probe = uploads / "write_probe.txt"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as exc:
        failures.append(f"data/uploads not writable: {type(exc).__name__}: {exc}")
    try:
        state = _http_json(config.ouroboros_url.rstrip("/") + "/api/state", timeout=5)
        details["ouroboros_state"] = {
            "supervisor_ready": state.get("supervisor_ready"),
            "runtime_mode": state.get("runtime_mode"),
        }
        if not state.get("supervisor_ready"):
            failures.append("Ouroboros server reachable but supervisor_ready is false")
    except Exception as exc:
        failures.append(f"Ouroboros server not reachable: {type(exc).__name__}: {exc}")
    try:
        ensure_outside_repo(config.result_root, config.repo_dir)
    except Exception as exc:
        failures.append(str(exc))
    return {"ok": not failures, "failures": failures, "details": details}


def _json_from_text(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _shell_action(command: str, cwd: str = "", timeout: int = 300) -> str:
    """Render a structured shell action as an OSWorld pyautogui/Python snippet.

    OSWorld records the resulting Python snippet as the official action and runs
    the command through a non-interactive bash. We deliberately do NOT fabricate
    ``~/.bash_history`` entries: writing the command into the history file to
    satisfy a terminal-task evaluator is hidden-verifier-knowledge / answer
    fitting (forbidden by the audit's methodology rules — the command's real
    execution path simply does not produce interactive history).
    """

    command = str(command or "").strip()
    cwd = str(cwd or "").strip()
    try:
        timeout = max(1, int(timeout))
    except Exception:
        timeout = 300
    encoded = base64.b64encode(command.encode("utf-8", errors="replace")).decode("ascii")
    return (
        "import base64, pathlib, subprocess, tempfile\n"
        f"cmd = base64.b64decode({encoded!r}).decode('utf-8', errors='replace')\n"
        f"cwd = {cwd!r} or None\n"
        f"timeout = {timeout!r}\n"
        "with tempfile.NamedTemporaryFile('w', suffix='.sh', delete=False) as script:\n"
        "    script.write('set -e\\n' + cmd + '\\n')\n"
        "    script_path = script.name\n"
        "try:\n"
        "    result = subprocess.run(['/bin/bash', script_path], cwd=cwd, text=True, capture_output=True, timeout=timeout)\n"
        "finally:\n"
        "    pathlib.Path(script_path).unlink(missing_ok=True)\n"
        "print(result.stdout)\n"
        "print(result.stderr)\n"
        "result.check_returncode()\n"
    )


def _click_action(x: Any, y: Any) -> str:
    return (
        "import pyautogui, time\n"
        f"pyautogui.click({int(float(x))}, {int(float(y))})\n"
        "time.sleep(0.5)\n"
    )


def _type_action(text: str, interval: float = 0.01) -> str:
    return (
        "import pyautogui, time\n"
        f"pyautogui.typewrite({str(text or '')!r}, interval={float(interval)!r})\n"
        "time.sleep(0.2)\n"
    )


def _hotkey_action(keys: Any) -> str:
    if isinstance(keys, str):
        key_list = [part.strip() for part in keys.split("+") if part.strip()]
    elif isinstance(keys, list):
        key_list = [str(part).strip() for part in keys if str(part).strip()]
    else:
        key_list = []
    return (
        "import pyautogui, time\n"
        f"pyautogui.hotkey(*{key_list!r})\n"
        "time.sleep(0.3)\n"
    )


def _wait_action(seconds: Any = 1.0) -> str:
    try:
        seconds = max(0.0, float(seconds))
    except Exception:
        seconds = 1.0
    return f"import time\ntime.sleep({seconds!r})\n"


def _initial_observation_with_retries(
    env: Any,
    example: dict[str, Any],
    *,
    startup_timeout_sec: int,
    reset_retries: int,
    wait_after_reset_sec: float,
    retry_sleep_sec: float,
    run_dir: Path,
) -> dict[str, Any]:
    """Reset OSWorld and wait for a usable first observation.

    VM reset, in-VM server readiness, screenshot capture, and accessibility-tree
    availability are startup concerns, not agent reasoning steps. Keep retrying
    them within a dedicated startup budget so transient VM/controller slowness does
    not become a task failure.
    """

    deadline = time.time() + max(1, int(startup_timeout_sec))
    attempts = max(1, int(reset_retries))
    errors: list[str] = []
    last_obs: dict[str, Any] = {}

    for attempt in range(1, attempts + 1):
        if time.time() >= deadline:
            break
        try:
            obs = env.reset(task_config=example)
            if wait_after_reset_sec > 0:
                time.sleep(wait_after_reset_sec)
            while time.time() < deadline:
                try:
                    obs = env._get_obs()
                    last_obs = obs if isinstance(obs, dict) else {}
                    screenshot = last_obs.get("screenshot")
                    if isinstance(screenshot, (bytes, bytearray)) and screenshot:
                        (run_dir / "startup_readiness.json").write_text(
                            json.dumps(
                                {
                                    "ok": True,
                                    "attempt": attempt,
                                    "has_screenshot": True,
                                    "has_accessibility_tree": bool(last_obs.get("accessibility_tree")),
                                },
                                ensure_ascii=False,
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                        return last_obs
                    errors.append(f"attempt {attempt}: observation missing screenshot")
                except Exception as exc:  # noqa: BLE001 - startup retry diagnostics
                    errors.append(f"attempt {attempt}: _get_obs {type(exc).__name__}: {exc}")
                time.sleep(max(0.1, retry_sleep_sec))
            break
        except Exception as exc:  # noqa: BLE001 - reset retry diagnostics
            errors.append(f"attempt {attempt}: reset {type(exc).__name__}: {exc}")
            time.sleep(max(0.1, retry_sleep_sec))

    (run_dir / "startup_readiness.json").write_text(
        json.dumps(
            {
                "ok": False,
                "errors": errors[-20:],
                "last_obs_keys": sorted(last_obs.keys()),
                "startup_timeout_sec": startup_timeout_sec,
                "reset_retries": reset_retries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    raise RuntimeError(
        f"OSWorld startup did not produce a usable screenshot within {startup_timeout_sec}s; "
        f"last errors: {errors[-3:]}"
    )


def _normalize_structured_action(item: Any) -> str:
    """Convert a model action object to one OSWorld action string."""

    if isinstance(item, str):
        text = item.strip()
        return text.upper() if text.upper() in SPECIAL_ACTIONS else text
    if not isinstance(item, dict):
        return ""
    kind = str(item.get("type") or item.get("action") or "").strip().lower()
    if kind in {"done", "finish"}:
        return "DONE"
    if kind in {"fail", "infeasible"}:
        return "FAIL"
    if kind == "wait":
        if "seconds" in item:
            return _wait_action(item.get("seconds"))
        return "WAIT"
    if kind == "shell":
        return _shell_action(
            str(item.get("command") or item.get("cmd") or ""),
            cwd=str(item.get("cwd") or ""),
            timeout=int(item.get("timeout_sec") or item.get("timeout") or 300),
        )
    if kind == "click":
        return _click_action(item.get("x", 0), item.get("y", 0))
    if kind == "type":
        return _type_action(str(item.get("text") or ""), interval=float(item.get("interval") or 0.01))
    if kind == "hotkey":
        return _hotkey_action(item.get("keys") or item.get("key") or "")
    if kind in {"press", "key"}:
        return _hotkey_action([item.get("key") or item.get("keys") or ""])
    if kind == "python":
        return str(item.get("code") or "").strip()
    return ""


class OuroborosStepAgent:
    def __init__(
        self,
        config: StepAgentConfig | None = None,
        **kwargs: Any,
    ) -> None:
        if config is None:
            config = StepAgentConfig(**kwargs)
        self.ouroboros_bin = config.ouroboros_bin
        self.ouroboros_url = config.ouroboros_url
        self.repo_dir = config.repo_dir
        self.data_dir = config.data_dir
        self.settings_path = config.settings_path
        self.result_dir = config.result_dir
        self.model = config.model
        self.timeout_sec = config.timeout_sec
        self.max_obs_chars = config.max_obs_chars
        self.screenshot_check_only = config.screenshot_check_only
        self.step_idx = 0
        self.history: list[dict[str, Any]] = []
        self.notes: list[str] = []

    def reset(self) -> None:
        self.step_idx = 0
        self.history.clear()
        self.notes.clear()

    def _save_screenshot(self, obs: dict[str, Any]) -> tuple[str, str]:
        screenshot = obs.get("screenshot")
        if not isinstance(screenshot, (bytes, bytearray)):
            return "", ""
        self.step_idx += 1
        name = f"step_{self.step_idx:03d}.png"
        local_path = self.result_dir / f"obs_{name}"
        local_path.write_bytes(bytes(screenshot))
        return str(local_path), str(local_path.name)

    @staticmethod
    def _prioritize_a11y(tree: str, budget: int) -> str:
        """Budget-bounded a11y view that PRIORITIZES interactive elements with
        coordinates instead of a blind head-slice (WS-9.6).

        A head-slice (the previous behavior) routinely cut the tree before the
        actionable widgets, so the agent never saw the controls it needed to
        click and resorted to blind/CLI moves. Here, when over budget, lines
        that name an interactive role AND/OR carry coordinates are kept first,
        then the rest in document order until the budget is spent.
        """
        if len(tree) <= budget:
            return tree
        lines = tree.splitlines()
        interactive = ("button", "menu", "entry", "text", "link", "check", "radio",
                       "tab", "combo", "field", "item", "toggle", "slider", "icon", "edit")
        coord_markers = ("coord", "position", "x=", "cp:", "screencoord", "bbox", "point")

        def score(line: str) -> int:
            low = line.lower()
            s = 0
            if any(k in low for k in interactive):
                s += 2
            if any(k in low for k in coord_markers):
                s += 2
            return s

        kept: list[tuple[int, str]] = []
        total = 0
        for _s, idx, line in sorted(((score(ln), i, ln) for i, ln in enumerate(lines)),
                                    key=lambda t: (-t[0], t[1])):
            if _s == 0 and total > 0:
                continue  # only spend budget on signal-bearing lines once we have some
            if total + len(line) + 1 > budget:
                continue
            kept.append((idx, line))
            total += len(line) + 1
        kept.sort()
        body = "\n".join(line for _i, line in kept)
        return body + "\n...[a11y prioritized: interactive/coordinate nodes kept, low-signal nodes dropped]"

    def _prompt(self, instruction: str, obs: dict[str, Any], screenshot_path: str, *, max_steps: int) -> str:
        a11y_tree = self._prioritize_a11y(str(obs.get("accessibility_tree") or ""), self.max_obs_chars)

        history_json = json.dumps(self.history[-12:], ensure_ascii=False, indent=2)
        notes_json = json.dumps(self.notes[-8:], ensure_ascii=False, indent=2)
        screenshot_instruction = (
            f'The current VM screenshot is attached to this Ouroboros run and also saved at "{screenshot_path}". '
            "Use the image directly when choosing GUI actions. If image input is unavailable, "
            "fall back to vlm_query(file_path=that path, prompt='Describe the Ubuntu desktop state and relevant controls')."
            if screenshot_path
            else "No screenshot bytes were available in this observation."
        )
        if self.screenshot_check_only:
            task_directive = (
                "This is a screenshot visibility smoke test. Use vlm_query on the "
                "screenshot path, then return WAIT with a short description of what "
                "you saw."
            )
        else:
            task_directive = (
                f"Choose the next OSWorld action(s). You are on step {self.step_idx} of at most {max_steps}. "
                "Prefer structured actions, not raw "
                "Python. Supported action objects: "
                '{"type":"shell","command":"...","cwd":"/home/user/Desktop"} (runs via non-interactive bash); '
                '{"type":"click","x":100,"y":200}; '
                '{"type":"type","text":"..."}; '
                '{"type":"hotkey","keys":["ctrl","l"]}; '
                '{"type":"wait","seconds":1}; '
                '{"type":"done"}; {"type":"fail"}. '
                'Use {"type":"python","code":"..."} only when no structured action fits. '
                "In app-named tasks, work in the named app first; if you edit files directly, reopen/verify in that app before done. "
                "Use done only after independently checking the evaluator-facing state. "
                "Use fail when demonstrably infeasible (missing hardware/resource, blocked permissions, feature absent); an out-of-app workaround is not success for an in-app task. "
                "Do NOT claim a screenshot or VLM 'confirmed' / 'shows' anything unless you actually called vlm_query (or were given image input) THIS step; otherwise describe only what the accessibility tree and action history establish."
            )

        return f"""You are Ouroboros acting as an external OSWorld step-loop agent.
Return ONLY a JSON object, with no markdown and no prose outside JSON.

JSON schema:
{{"response": "short rationale", "notes": "optional cross-step note for yourself", "actions": [{{"type": "shell", "command": "..."}}]}}

{task_directive}
{screenshot_instruction}

Task:
{instruction}

Recent official OSWorld action history:
{history_json}

Cross-step notes:
{notes_json}

Accessibility tree (may be empty/truncated):
{a11y_tree}
"""

    def predict(self, instruction: str, obs: dict[str, Any], *, max_steps: int) -> tuple[str, list[str], dict[str, Any]]:
        screenshot_path, local_screenshot = self._save_screenshot(obs)
        prompt = self._prompt(instruction, obs, screenshot_path, max_steps=max_steps)
        step = self.step_idx
        (self.result_dir / f"prompt_step_{step:03d}.txt").write_text(prompt, encoding="utf-8")

        env = os.environ.copy()
        env.update({
            "OUROBOROS_REPO_DIR": str(self.repo_dir),
            "OUROBOROS_DATA_DIR": str(self.data_dir),
            "OUROBOROS_SETTINGS_PATH": str(self.settings_path),
            "OUROBOROS_RUNTIME_MODE": "pro",
            "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
            "PYTHONUNBUFFERED": "1",
        })
        if self.model:
            env.update({
                "OUROBOROS_MODEL": self.model,
                "OUROBOROS_MODEL_CODE": self.model,
                "OUROBOROS_MODEL_LIGHT": self.model,
                "OUROBOROS_MODEL_FALLBACK": self.model,
            })

        cmd = [
            self.ouroboros_bin,
            "run",
            "--url",
            self.ouroboros_url,
            "--memory-mode",
            "empty",
            "--quiet",
            *([ "--attach", screenshot_path ] if screenshot_path else []),
            prompt,
        ]
        timed_out = False
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(self.repo_dir),
                env=env,
                text=True,
                capture_output=True,
                timeout=self.timeout_sec,
            )
            returncode = completed.returncode
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = 124
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            stderr = (stderr + "\n" if stderr else "") + (
                f"OSWorld adapter: Ouroboros step timed out after {self.timeout_sec}s"
            )
        (self.result_dir / f"ouroboros_step_{step:03d}.stdout.txt").write_text(stdout, encoding="utf-8")
        (self.result_dir / f"ouroboros_step_{step:03d}.stderr.txt").write_text(stderr, encoding="utf-8")

        payload = _json_from_text(stdout.strip())
        response = str(payload.get("response") or stdout.strip() or stderr.strip() or "")
        note = str(payload.get("notes") or "").strip()
        if note:
            self.notes.append(note[:1000])
        raw_actions = payload.get("actions")
        _known_kinds = {"done", "finish", "fail", "wait", "shell", "click", "type", "hotkey", "key", "python"}
        actions = []
        unknown_kinds: list[str] = []
        if isinstance(raw_actions, list):
            for item in raw_actions:
                translated = _normalize_structured_action(item)
                if translated.strip():
                    actions.append(translated)
                elif isinstance(item, dict):
                    k = str(item.get("type") or item.get("action") or "").strip().lower()
                    if k and k not in _known_kinds:
                        unknown_kinds.append(k)
        if unknown_kinds:
            # Feed unknown/dropped action types back to the model (was a silent
            # drop) so it stops re-emitting them and picks a supported action.
            self.notes.append(
                f"[adapter] dropped unsupported action type(s) {sorted(set(unknown_kinds))}; "
                "use only the supported action objects listed in the directive."
            )
        if returncode != 0:
            response = (
                f"Ouroboros step timed out after {self.timeout_sec}s: {response}"
                if timed_out
                else f"ouroboros exited {returncode}: {response}"
            )
            actions = actions or ["WAIT"]
        actions = [action.upper() if action.upper() in SPECIAL_ACTIONS else action for action in actions]
        actions = actions or ["WAIT"]
        if self.screenshot_check_only and "DONE" not in actions and "FAIL" not in actions:
            actions = ["WAIT"]

        debug = {
            "step": step,
            "returncode": returncode,
            "timed_out": timed_out,
            "screenshot_upload_path": screenshot_path,
            "screenshot_file": local_screenshot,
            "payload": payload,
            "normalized_actions": actions,
        }
        return response, actions, debug

    def record_action(self, *, action: str, response: str, reward: float, done: bool, info: dict[str, Any]) -> None:
        self.history.append({
            "action": action,
            "response": response,
            "reward": reward,
            "done": done,
            "info": info,
        })


def _write_task_records(config: TaskRecordConfig) -> dict[str, Any]:
    details = dict(config.extra or {})
    outcome = {
        "ok": config.status == "completed",
        "task_id": config.example_id,
        "domain": config.domain,
        "reward": config.reward,
        "steps": config.steps,
        "status": config.status,
        "reason_code": config.reason_code,
        "error": config.error,
        "result_dir": str(config.run_dir),
        **details,
    }
    write_json(config.run_dir / "task_outcome.json", outcome)
    write_json(
        config.run_dir / "task_run_manifest.json",
        benchmark_run_manifest(
            benchmark="osworld",
            run_root=config.result_root,
            repo_dir=config.repo_dir,
            requested_task_ids=[config.example_id],
            dataset="OSWorld",
            settings_path=config.settings_path,
            output_paths={
                "task_outcome": str(config.run_dir / "task_outcome.json"),
                "traj": str(config.run_dir / "traj.jsonl"),
                "task_run_manifest": str(config.run_dir / "task_run_manifest.json"),
            },
            harness={
                "adapter": "external_step_loop",
                "official_actions": True,
                "memory_mode": "empty_per_ouroboros_step",
            },
            extra=details,
        ),
    )
    append_result_index(
        config.result_root,
        task_result_row(
            benchmark="osworld",
            instance_id=config.example_id,
            status=config.status,
            reason_code=config.reason_code,
            official_eval_status="completed" if config.reward is not None else "not_run",
            output_paths={
                "task_outcome": str(config.run_dir / "task_outcome.json"),
                "task_run_manifest": str(config.run_dir / "task_run_manifest.json"),
                "traj": str(config.run_dir / "traj.jsonl"),
            },
            error=config.error,
            details={"domain": config.domain, "reward": config.reward, "steps": config.steps, **details},
        ),
    )
    return outcome


def main() -> int:
    _ensure_vmrun_on_path()
    _install_optional_dependency_stubs()
    parser = argparse.ArgumentParser()
    parser.add_argument("--osworld-root", default=DEFAULT_OSWORLD_ROOT)
    parser.add_argument("--provider_name", default="vmware")
    parser.add_argument("--path_to_vm", default=DEFAULT_VM)
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--result_dir", default="results/ouroboros_step_agent")
    parser.add_argument("--repo-dir", default=DEFAULT_REPO)
    parser.add_argument("--data-dir", default=DEFAULT_DATA)
    parser.add_argument("--settings-path", default=DEFAULT_SETTINGS)
    parser.add_argument("--ouroboros-bin", default=DEFAULT_OUROBOROS_BIN)
    parser.add_argument("--ouroboros-url", default="http://127.0.0.1:8765")
    parser.add_argument("--model", default="anthropic/claude-opus-4-7")
    parser.add_argument("--max_steps", type=int, default=50)
    parser.add_argument("--step_timeout_sec", type=int, default=240)
    parser.add_argument("--sleep_after_execution", type=float, default=1.0)
    parser.add_argument("--wait_after_reset_sec", type=float, default=8.0)
    parser.add_argument("--startup_timeout_sec", type=int, default=600)
    parser.add_argument("--reset_retries", type=int, default=3)
    parser.add_argument("--startup_retry_sleep_sec", type=float, default=5.0)
    parser.add_argument("--max_obs_chars", type=int, default=12000)
    parser.add_argument("--screenshot-check-only", action="store_true")
    parser.add_argument("--show-vm", action="store_true")
    args = parser.parse_args()

    osworld_root = Path(args.osworld_root).expanduser().resolve(strict=False)
    sys.path.insert(0, str(osworld_root))
    task_path = Path(args.task).expanduser()
    if not task_path.is_absolute():
        task_path = osworld_root / task_path
    domain = task_path.parent.name
    example_id = task_path.stem
    result_root = Path(args.result_dir).expanduser()
    if not result_root.is_absolute():
        result_root = osworld_root / result_root
    result_root = ensure_outside_repo(result_root, Path(args.repo_dir).expanduser().resolve(strict=False))
    run_dir = result_root / domain / example_id
    run_dir.mkdir(parents=True, exist_ok=True)

    repo_dir = Path(args.repo_dir).expanduser().resolve(strict=False)
    data_dir = Path(args.data_dir).expanduser().resolve(strict=False)
    settings_path = Path(args.settings_path).expanduser().resolve(strict=False)
    preflight = _preflight(PreflightConfig(
        osworld_root=osworld_root,
        task_path=task_path,
        path_to_vm=args.path_to_vm,
        repo_dir=repo_dir,
        data_dir=data_dir,
        settings_path=settings_path,
        result_root=result_root,
        ouroboros_url=args.ouroboros_url,
        model=args.model,
    ))
    write_json(run_dir / "preflight.json", preflight)
    if not preflight["ok"]:
        outcome = _write_task_records(TaskRecordConfig(
            run_dir=run_dir,
            result_root=result_root,
            repo_dir=repo_dir,
            settings_path=settings_path,
            example_id=example_id,
            domain=domain,
            reward=None,
            steps=0,
            status="blocked",
            reason_code="preflight_failed",
            error="; ".join(preflight["failures"]),
            extra={"preflight": preflight},
        ))
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return 2
    example = json.loads(task_path.read_text(encoding="utf-8"))
    example_id = str(example.get("id") or task_path.stem)
    (run_dir / "task.json").write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")
    from desktop_env.desktop_env import DesktopEnv

    env = None
    agent = OuroborosStepAgent(StepAgentConfig(
        ouroboros_bin=args.ouroboros_bin,
        ouroboros_url=args.ouroboros_url,
        repo_dir=repo_dir,
        data_dir=data_dir,
        settings_path=settings_path,
        result_dir=run_dir,
        task_id=example_id,
        model=args.model,
        timeout_sec=args.step_timeout_sec,
        max_obs_chars=args.max_obs_chars,
        screenshot_check_only=args.screenshot_check_only,
    ))

    try:
        env = DesktopEnv(
            provider_name=args.provider_name,
            path_to_vm=args.path_to_vm,
            action_space="pyautogui",
            screen_size=(1920, 1080),
            headless=not args.show_vm,
            os_type="Ubuntu",
            require_a11y_tree=True,
        )
        obs = _initial_observation_with_retries(
            env,
            example,
            startup_timeout_sec=args.startup_timeout_sec,
            reset_retries=args.reset_retries,
            wait_after_reset_sec=max(0.0, args.wait_after_reset_sec),
            retry_sleep_sec=max(0.1, args.startup_retry_sleep_sec),
            run_dir=run_dir,
        )
        agent.reset()
        instruction = str(example["instruction"])
        done = False
        step_idx = 0
        while not done and step_idx < args.max_steps:
            response, actions, debug = agent.predict(instruction, obs, max_steps=args.max_steps)
            (run_dir / f"debug_step_{step_idx + 1:03d}.json").write_text(
                json.dumps(debug, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for action in actions:
                ts = _dt.datetime.now().strftime("%Y%m%d@%H%M%S%f")
                obs, reward, done, info = env.step(action, args.sleep_after_execution)
                agent.record_action(
                    action=action,
                    response=response,
                    reward=float(reward),
                    done=bool(done),
                    info=dict(info or {}),
                )
                with (run_dir / "traj.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "step_num": step_idx + 1,
                        "action_timestamp": ts,
                        "action": action,
                        "response": response,
                        "reward": reward,
                        "done": done,
                        "info": info,
                        **debug,
                    }, ensure_ascii=False) + "\n")
                if done:
                    break
            step_idx += 1
            if args.screenshot_check_only:
                break

        reward = float(env.evaluate())
        (run_dir / "result.txt").write_text(f"{reward}\n", encoding="utf-8")
        outcome = _write_task_records(TaskRecordConfig(
            run_dir=run_dir,
            result_root=result_root,
            repo_dir=repo_dir,
            settings_path=settings_path,
            example_id=example_id,
            domain=domain,
            reward=reward,
            steps=step_idx,
            status="completed",
            reason_code="official_evaluate",
            extra={"screenshot_check_only": bool(args.screenshot_check_only)},
        ))
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - denominator-preserving adapter failure
        error = f"{type(exc).__name__}: {exc}"
        outcome = _write_task_records(TaskRecordConfig(
            run_dir=run_dir,
            result_root=result_root,
            repo_dir=repo_dir,
            settings_path=settings_path,
            example_id=example_id,
            domain=domain,
            reward=None,
            steps=locals().get("step_idx", 0),
            status="adapter_error",
            reason_code=type(exc).__name__,
            error=error,
        ))
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return 1
    finally:
        if env is not None:
            try:
                env.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
