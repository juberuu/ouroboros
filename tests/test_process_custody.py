"""Process custody: supervised spawn chokepoint, ledger, reaper, lifeline."""

import json
import os
import pathlib
import re
import subprocess
import sys
import time

import pytest

from ouroboros import process_custody
from ouroboros.process_custody import (
    ledger_path,
    reap_orphaned_processes,
    record_process,
    spawn_supervised,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Popen call sites that legitimately bypass spawn_supervised: short-lived
# synchronous helpers (waited within the call), panic/cleanup layers, the
# launcher (custody host), and custody itself. Adding a NEW long-lived spawn
# site requires routing it through spawn_supervised/record_process or
# explicitly justifying it here.
_POPEN_ALLOWLIST = {
    "launcher.py",                        # custody host process (pre-runtime)
    "ouroboros/process_custody.py",       # the chokepoint itself
    "ouroboros/platform_layer.py",        # primitives (hidden_run helpers)
    "ouroboros/packaged_cli.py",          # user-facing CLI wrapper (foreground)
    "ouroboros/cli.py",                   # dev CLI (foreground)
    "ouroboros/server_control.py",        # restart exec path
    "ouroboros/headless.py",              # waited synchronous child
    "ouroboros/preflight_runner.py",      # waited hermetic pytest child
    "ouroboros/tools/shell.py",           # bounded foreground commands (waited + tracked)
    "ouroboros/tools/skill_exec.py",      # bounded skill run (waited + tracked)
    "ouroboros/tools/skill_preflight.py", # waited preflight child
    "ouroboros/marketplace/isolated_deps.py",  # waited installer child
    "ouroboros/gateways/claude_code.py",  # waited readonly child (timeout-bound)
    "ouroboros/extension_process_runner.py",  # waited extension child
    "ouroboros/workspace_executor.py",    # custody write-through added at spawn
    "ouroboros/local_model.py",           # custody record added at spawn
    "ouroboros/extension_companion.py",   # custody write-through added at spawn
    "ouroboros/tools/services.py",        # routed through spawn_supervised
}


def test_popen_sites_are_custodied_or_allowlisted():
    pattern = re.compile(r"subprocess\.Popen\(|[^.\w]Popen\(")
    offenders = []
    for base in ("ouroboros", "supervisor"):
        for path in (REPO_ROOT / base).rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            if pattern.search(text) and rel not in _POPEN_ALLOWLIST:
                offenders.append(rel)
    for name in ("server.py", "launcher.py"):
        path = REPO_ROOT / name
        if path.exists() and pattern.search(path.read_text(encoding="utf-8", errors="replace")):
            if name not in _POPEN_ALLOWLIST:
                offenders.append(name)
    assert not offenders, (
        "New raw Popen call sites outside the custody allowlist: "
        f"{offenders}. Route long-lived spawns through "
        "process_custody.spawn_supervised (or record_process write-through) "
        "or extend the allowlist with a justification comment."
    )


def test_spawn_supervised_records_ledger_entry(tmp_path):
    proc = spawn_supervised(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        drive_root=tmp_path,
        purpose="test-sleeper",
        scope="task",
        owner_task_id="t123",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        lines = ledger_path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[-1])
        assert entry["pid"] == proc.pid
        assert entry["purpose"] == "test-sleeper"
        assert entry["scope"] == "task"
        assert entry["owner_task"] == "t123"
        assert entry["session_id"] == process_custody.current_custody_session_id()
        if os.name != "nt":
            assert entry["fingerprint"]["start_time"]
        assert entry["fingerprint"]["cmd_sha256"]
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_reaper_kills_stale_session_entry(tmp_path, monkeypatch):
    proc = spawn_supervised(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        drive_root=tmp_path,
        purpose="stale-service",
        scope="task",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Simulate a NEW server generation.
        monkeypatch.setattr(process_custody, "_SESSION_ID", "next-generation")
        reaped = reap_orphaned_processes(tmp_path)
        assert proc.pid in reaped
        deadline = time.time() + 5
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.05)
        assert proc.poll() is not None, "stale-session process must be dead"
        events = (tmp_path / "logs" / "supervisor.jsonl").read_text(encoding="utf-8")
        assert "process_reaped" in events
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_reaper_never_kills_fingerprint_mismatch(tmp_path, monkeypatch):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Ledger entry claims this pid but with a FOREIGN fingerprint —
        # models a recycled pid from another install. Strict rule: never kill.
        record = record_process(
            tmp_path, pid=proc.pid, cmd=["sleep", "60"],
            purpose="foreign", scope="task",
        )
        entries = [dict(record, fingerprint={"start_time": "FOREIGN", "cmd_sha256": "deadbeef"})]
        process_custody._rewrite_ledger(tmp_path, entries)
        monkeypatch.setattr(process_custody, "_SESSION_ID", "next-generation")
        reaped = reap_orphaned_processes(tmp_path)
        assert proc.pid not in reaped
        assert proc.poll() is None, "fingerprint-mismatched process must survive"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_reaper_keeps_daemon_and_same_session(tmp_path):
    proc = spawn_supervised(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        drive_root=tmp_path,
        purpose="companion",
        scope="daemon",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc2 = spawn_supervised(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        drive_root=tmp_path,
        purpose="live-session-service",
        scope="session",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        reaped = reap_orphaned_processes(tmp_path)
        assert proc.pid not in reaped
        assert proc2.pid not in reaped
        assert proc.poll() is None
        assert proc2.poll() is None
    finally:
        for p in (proc, proc2):
            p.kill()
            p.wait(timeout=5)


def test_task_scope_reaped_when_owner_task_gone(tmp_path):
    proc = spawn_supervised(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        drive_root=tmp_path,
        purpose="task-service",
        scope="task",
        owner_task_id="finished-task",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        reaped = reap_orphaned_processes(tmp_path, running_task_ids={"some-other-task"})
        assert proc.pid in reaped
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="lifeline is POSIX-only")
def test_lifeline_kills_child_when_parent_dies(tmp_path):
    # Parent spawns a child that starts the lifeline, then the parent exits.
    child_src = (
        "import sys; sys.path.insert(0, %r);"
        "from ouroboros.process_custody import start_parent_lifeline;"
        "start_parent_lifeline(poll_sec=0.2, label='test');"
        "import time; time.sleep(60)"
    ) % str(REPO_ROOT)
    parent_src = (
        "import subprocess, sys, pathlib;"
        f"child = subprocess.Popen([sys.executable, '-c', {child_src!r}]);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid));"
    )
    pid_file = tmp_path / "child_pid"
    subprocess.run(
        [sys.executable, "-c", parent_src, str(pid_file)],
        check=True, timeout=15,
    )
    child_pid = int(pid_file.read_text())
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return  # lifeline fired
        time.sleep(0.2)
    try:
        os.kill(child_pid, 9)
    except ProcessLookupError:
        return
    raise AssertionError("child outlived dead parent despite lifeline")
