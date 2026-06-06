from __future__ import annotations

import asyncio
import contextlib
import io
import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from devtools.benchmarks.common.official_commands import programbench_eval_cmd, swebench_eval_cmd
from devtools.benchmarks.osworld.normalize_logs import normalize_bundle
from devtools.benchmarks.programbench.programbench_adapter import (
    build_ouroboros_task_body,
    create_submission_tarball,
    preflight_cleanroom_container,
)
from devtools.benchmarks.swe_bench.presets import resolve_preset


REPO_ROOT = Path(__file__).resolve().parents[1]
_BASH_CAPTURE_AVAILABLE = sys.platform != "win32" and shutil.which("bash") is not None


def _git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "app.py").write_text("print('base')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def test_runtime_core_does_not_import_devtools():
    runtime_paths = [REPO_ROOT / "ouroboros", REPO_ROOT / "server.py"]
    offenders: list[str] = []
    for root in runtime_paths:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "import devtools" in text or "from devtools" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders


def test_official_command_builders_do_not_replace_scoring():
    assert programbench_eval_cmd(Path("/runs/pb")) == ["programbench", "eval", "/runs/pb"]
    assert swebench_eval_cmd("princeton-nlp/SWE-bench_Verified", Path("/runs/predictions.jsonl"), "ouroboros", 2) == [
        "python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        "princeton-nlp/SWE-bench_Verified",
        "--predictions_path",
        "/runs/predictions.jsonl",
        "--max_workers",
        "2",
        "--run_id",
        "ouroboros",
    ]


def test_pyproject_does_not_package_devtools_runtime_assets():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"devtools*"' not in pyproject
    assert "devtools = [" not in pyproject
    assert '"benchmarks/**/*.sh"' not in pyproject
    assert '"benchmarks/**/*.md"' not in pyproject


def test_executable_devtools_entrypoints_support_direct_help():
    scripts = [
        "devtools/benchmarks/programbench/run_programbench.py",
        "devtools/benchmarks/terminal_bench/run_harbor_smoke.py",
        "devtools/benchmarks/swe_bench/swebench_predictions.py",
        "devtools/benchmarks/swe_bench_pro/grade_pro.py",
        "devtools/benchmarks/swe_bench_pro/pro_predictions.py",
        "devtools/benchmarks/osworld/normalize_logs.py",
        "devtools/benchmarks/osworld/osworld_adapter_skeleton.py",
    ]
    for rel in scripts:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / rel), "--help"],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
        assert proc.returncode == 0, f"{rel} failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        assert "usage:" in proc.stdout.lower()


def test_programbench_task_body_sets_executor_and_protected_policy(tmp_path):
    workspace = tmp_path / "workspace"
    _git_repo(workspace)

    body = build_ouroboros_task_body(
        instruction="solve",
        workspace_host_path=workspace,
        container_name="pb-cleanroom",
        protected_backend_paths=["/workspace/executable"],
    )

    assert body["allowed_resources"] == {"web": False, "network": False, "internet": False}
    assert body["actor_id"] == "programbench"
    assert body["source"] == "programbench"
    assert "actor_id" not in body["metadata"]
    assert body["executor_ref"]["type"] == "docker_exec"
    assert body["executor_ref"]["network"] == "none"
    protected = body["resource_policy"]["protected_artifacts"][0]
    assert protected["role"] == "black_box_reference"
    assert protected["allow"] == ["execute"]
    assert {"read_bytes", "hash", "static_introspection", "dynamic_trace", "debug"} <= set(protected["deny"])


def test_programbench_git_workspace_does_not_commit_protected_reference(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "executable").write_text("protected-bytes\n", encoding="utf-8")

    build_ouroboros_task_body(
        instruction="solve",
        workspace_host_path=workspace,
        container_name="pb-cleanroom",
        protected_backend_paths=["/workspace/executable"],
    )

    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    show = subprocess.run(["git", "show", "HEAD:executable"], cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert head.returncode != 0
    assert show.returncode != 0


def test_programbench_submission_tarball_excludes_repo_noise(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    (workspace / ".ouroboros").mkdir()
    (workspace / ".ouroboros" / "trace.json").write_text("{}\n", encoding="utf-8")
    (workspace / "node_modules" / "pkg").mkdir(parents=True)
    (workspace / "node_modules" / "pkg" / "index.js").write_text("junk\n", encoding="utf-8")
    (workspace / "build").mkdir()
    (workspace / "build" / "out.o").write_text("junk\n", encoding="utf-8")
    (workspace / "dist").mkdir()
    (workspace / "dist" / "bundle.js").write_text("junk\n", encoding="utf-8")
    (workspace / "executable").write_text("protected\n", encoding="utf-8")
    (workspace / "solution.py").write_text("print('ok')\n", encoding="utf-8")

    tar_path = create_submission_tarball(
        workspace,
        tmp_path / "submission.tar.gz",
        protected_paths=["/workspace/executable", "executable"],
    )

    with tarfile.open(tar_path, "r:gz") as tar:
        names = set(tar.getnames())
    assert "solution.py" in names
    assert ".git/HEAD" not in names
    assert ".ouroboros/trace.json" not in names
    assert "node_modules/pkg/index.js" not in names
    assert "build/out.o" not in names
    assert "dist/bundle.js" not in names
    assert "executable" not in names


def test_programbench_instance_path_stays_under_run_root(tmp_path):
    from devtools.benchmarks.common.run_roots import safe_join_under

    root = tmp_path / "programbench-run"
    assert safe_join_under(root, "cheat/cheat") == root.resolve(strict=False) / "cheat" / "cheat"
    with pytest.raises(ValueError, match="escapes run root"):
        safe_join_under(root, "../escape")
    with pytest.raises(ValueError, match="escapes run root"):
        safe_join_under(root, "/tmp/escape")


def test_programbench_cleanroom_preflight_requires_task_cleanroom_and_no_network(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps([
                {
                    "Config": {"Image": "ghcr.io/facebookresearch/programbench/foo:task_cleanroom"},
                    "HostConfig": {"NetworkMode": "none"},
                }
            ]),
            stderr="",
        )

    import devtools.benchmarks.programbench.programbench_adapter as adapter

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    assert preflight_cleanroom_container("pb") == {
        "image": "ghcr.io/facebookresearch/programbench/foo:task_cleanroom",
        "network": "none",
    }
    assert calls[0][:2] == ["docker", "inspect"]


def test_swe_verified_preset_uses_official_dataset_name():
    assert resolve_preset("verified") == "princeton-nlp/SWE-bench_Verified"
    assert resolve_preset("SWE-bench/SWE-bench_Verified") == "princeton-nlp/SWE-bench_Verified"


def test_terminal_bench_harbor_adapter_is_optional_import():
    spec = importlib.util.spec_from_file_location(
        "tb_harbor_adapter",
        REPO_ROOT / "devtools" / "benchmarks" / "terminal_bench" / "harbor_installed_agent.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.OuroborosTerminalBenchAgent.name() == "Ouroboros Installed"


def test_terminal_bench_adapter_does_not_commit_target_workspace():
    adapter = (REPO_ROOT / "devtools" / "benchmarks" / "terminal_bench" / "harbor_installed_agent.py").read_text(encoding="utf-8")
    assert "git add -A" not in adapter
    assert "git commit --allow-empty" not in adapter


def test_terminal_bench_source_copy_excludes_secret_shaped_files(tmp_path):
    import devtools.benchmarks.terminal_bench.harbor_installed_agent as tb_agent

    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "module.py").write_text("print('ok')\n", encoding="utf-8")
    secret_names = (
        ".env",
        ".env.example",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "aws-credentials.json",
        "credentials.json",
        "gcp-service-account.json",
        "id_rsa",
        "repo.bundle",
        "repo_bundle_manifest.json",
        "secrets.json",
        "service-account.json",
    )
    for name in secret_names:
        (source / name).write_text("secret\n", encoding="utf-8")
    (source / "cert.pem").write_text("secret\n", encoding="utf-8")
    (source / "python-standalone").mkdir()
    (source / "python-standalone" / "python").write_text("binary\n", encoding="utf-8")

    tb_agent._copy_clean_source(source, target)

    assert (target / "module.py").exists()
    for name in (*secret_names, "cert.pem", "python-standalone"):
        assert not (target / name).exists()


def test_terminal_bench_network_preflight_uses_configured_provider(tmp_path, monkeypatch):
    import devtools.benchmarks.terminal_bench.harbor_installed_agent as tb_agent

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    class Env:
        def __init__(self) -> None:
            self.command = ""

        async def exec(self, *, command, timeout_sec=None, env=None, cwd=None):
            self.command = command
            script = command.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            stdout = io.StringIO()
            code = 0
            try:
                with contextlib.redirect_stdout(stdout):
                    exec(script, {})
            except SystemExit as exc:
                code = int(exc.code or 0)
            return SimpleNamespace(return_code=code, stdout=stdout.getvalue(), stderr="")

    from types import SimpleNamespace

    env = Env()
    agent = tb_agent.OuroborosTerminalBenchAgent(logs_dir=tmp_path)

    asyncio.run(agent._network_preflight(env, {"OPENAI_API_KEY": "sk-test"}))

    assert "api.openai.com" in env.command
    assert "openrouter.ai" not in env.command
    assert "urllib.error.HTTPError" in env.command
    assert "openai_preflight_status 401" in (tmp_path / "network-preflight.txt").read_text(encoding="utf-8")


def test_terminal_bench_adapter_forwards_gigachat_and_preflights_direct_provider(tmp_path, monkeypatch):
    import devtools.benchmarks.terminal_bench.harbor_installed_agent as tb_agent

    monkeypatch.setenv("OUROBOROS_BENCH_ALLOW_CONTAINER_SECRETS", "1")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "gigachat-test-credentials")
    monkeypatch.setenv("GIGACHAT_BASE_URL", "https://gigachat.example.invalid/api/v1")

    class Env:
        def __init__(self) -> None:
            self.command = ""

        async def exec(self, *, command, timeout_sec=None, env=None, cwd=None):
            self.command = command
            script = command.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            stdout = io.StringIO()
            code = 0
            try:
                with contextlib.redirect_stdout(stdout):
                    exec(script, {})
            except SystemExit as exc:
                code = int(exc.code or 0)
            return SimpleNamespace(return_code=code, stdout=stdout.getvalue(), stderr="")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    agent = tb_agent.OuroborosTerminalBenchAgent(logs_dir=tmp_path)
    injected = agent._container_env()
    env = Env()

    asyncio.run(agent._network_preflight(env, injected))

    assert injected["GIGACHAT_CREDENTIALS"] == "gigachat-test-credentials"
    assert "gigachat.example.invalid/api/v1/models" in env.command
    assert "gigachat_preflight_status 401" in (tmp_path / "network-preflight.txt").read_text(encoding="utf-8")


def test_terminal_bench_adapter_refuses_container_secret_injection_by_default(tmp_path, monkeypatch):
    import devtools.benchmarks.terminal_bench.harbor_installed_agent as tb_agent

    monkeypatch.delenv("OUROBOROS_BENCH_ALLOW_CONTAINER_SECRETS", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-container-secret")
    agent = tb_agent.OuroborosTerminalBenchAgent(logs_dir=tmp_path)
    injected = agent._container_env()

    assert "OPENROUTER_API_KEY" not in injected
    with pytest.raises(RuntimeError, match="refuses to inject long-lived provider credentials"):
        agent._enforce_container_secret_policy(injected)


def test_terminal_bench_task_body_uses_top_level_actor_id():
    adapter = (REPO_ROOT / "devtools" / "benchmarks" / "terminal_bench" / "harbor_installed_agent.py").read_text(encoding="utf-8")
    assert '"actor_id": "harbor-terminal-bench"' in adapter
    assert '"metadata": {{"source": "terminal-bench", "delegation_role": "root"}}' in adapter
    assert '"metadata": {{"actor_id": "harbor-terminal-bench"' not in adapter


@pytest.mark.skipif(not _BASH_CAPTURE_AVAILABLE, reason="capture_patch.sh is a POSIX shell helper; Python wrappers are covered separately")
def test_swe_pro_capture_keeps_untracked_text_and_drops_binary(tmp_path):
    repo = tmp_path / "repo"
    base = _git_repo(repo)
    (repo / "new_file.py").write_text("print('new')\n", encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"\x00\x01\x02\x03")
    (repo / "build").mkdir()
    (repo / "build" / "out.txt").write_text("junk\n", encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist" / "out.txt").write_text("junk\n", encoding="utf-8")
    (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
    capture = REPO_ROOT / "devtools" / "benchmarks" / "swe_bench_pro" / "capture_patch.sh"
    out = tmp_path / "patch.diff"

    subprocess.run(["bash", str(capture), str(repo), base, str(out)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    patch = out.read_text(encoding="utf-8")

    assert "new_file.py" in patch
    assert "app.py" in patch
    assert "binary.bin" not in patch
    assert "build/out.txt" not in patch
    assert "dist/out.txt" not in patch


@pytest.mark.skipif(not _BASH_CAPTURE_AVAILABLE, reason="capture_patch.sh is a POSIX shell helper; Python wrappers are covered separately")
def test_swe_pro_capture_requires_valid_base_and_external_output(tmp_path):
    repo = tmp_path / "repo"
    base = _git_repo(repo)
    (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
    capture = REPO_ROOT / "devtools" / "benchmarks" / "swe_bench_pro" / "capture_patch.sh"

    missing_output = subprocess.run(["bash", str(capture), str(repo), base], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    bad_base = subprocess.run(
        ["bash", str(capture), str(repo), "not-a-commit", str(tmp_path / "bad.diff")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    internal_output = REPO_ROOT / "devtools" / "should-not-write.diff"
    internal_dir = REPO_ROOT / "_test_rejected_capture_output_dir"
    nested_internal_output = internal_dir / "out.diff"
    shutil.rmtree(internal_dir, ignore_errors=True)
    try:
        repo_internal = subprocess.run(
            ["bash", str(capture), str(repo), base, str(internal_output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        nested_repo_internal = subprocess.run(
            ["bash", str(capture), str(repo), base, str(nested_internal_output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        internal_output.unlink(missing_ok=True)
        shutil.rmtree(internal_dir, ignore_errors=True)

    assert missing_output.returncode != 0
    assert bad_base.returncode != 0
    assert repo_internal.returncode != 0
    assert "outside the Ouroboros repo" in repo_internal.stderr
    assert nested_repo_internal.returncode != 0
    assert "outside the Ouroboros repo" in nested_repo_internal.stderr
    assert not internal_dir.exists()


def test_swe_pro_grade_runs_official_eval_with_raw_sample(tmp_path, monkeypatch):
    import devtools.benchmarks.swe_bench_pro.grade_pro as grade_pro

    eval_repo = tmp_path / "SWE-bench_Pro-os"
    helper = eval_repo / "helper_code"
    helper.mkdir(parents=True)
    raw_sample = helper / "sweap_eval_full_v2.jsonl"
    raw_sample.write_text(json.dumps({"instance_id": "x", "FAIL_TO_PASS": [], "PASS_TO_PASS": []}) + "\n", encoding="utf-8")
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(json.dumps({"instance_id": "x", "model_patch": "diff --git a/a b/a\n", "model_name_or_path": "m"}) + "\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(grade_pro.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "grade_pro.py",
            "--predictions",
            str(predictions),
            "--out-dir",
            str(tmp_path / "out"),
            "--eval-repo",
            str(eval_repo),
        ],
    )

    assert grade_pro.main() == 0
    assert "--raw_sample_path" in captured["cmd"]
    assert str(raw_sample) in captured["cmd"]
    assert captured["cwd"] == str(eval_repo)


def test_swe_pro_grade_rejects_repo_internal_output(tmp_path, monkeypatch):
    import devtools.benchmarks.swe_bench_pro.grade_pro as grade_pro

    eval_repo = tmp_path / "SWE-bench_Pro-os"
    helper = eval_repo / "helper_code"
    helper.mkdir(parents=True)
    raw_sample = helper / "sweap_eval_full_v2.jsonl"
    raw_sample.write_text(json.dumps({"instance_id": "x", "FAIL_TO_PASS": [], "PASS_TO_PASS": []}) + "\n", encoding="utf-8")
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(json.dumps({"instance_id": "x", "model_patch": "diff --git a/a b/a\n", "model_name_or_path": "m"}) + "\n", encoding="utf-8")
    internal_out = REPO_ROOT / "_test_rejected_grade_output_dir"
    shutil.rmtree(internal_out, ignore_errors=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "grade_pro.py",
            "--predictions",
            str(predictions),
            "--out-dir",
            str(internal_out),
            "--eval-repo",
            str(eval_repo),
            "--skip-run",
        ],
    )
    try:
        with pytest.raises(ValueError, match="under repo"):
            grade_pro.main()
        assert not internal_out.exists()
    finally:
        shutil.rmtree(internal_out, ignore_errors=True)


def test_swe_pro_prediction_capture_rejects_empty_patch(tmp_path, monkeypatch):
    import devtools.benchmarks.swe_bench_pro.pro_predictions as pro_predictions

    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "empty.diff"

    def fake_run(cmd, **kwargs):
        out.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(pro_predictions.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="empty patch"):
        pro_predictions._capture_patch(repo, "HEAD", out)


def test_swe_predictions_rejects_unsafe_instance_id_before_logs_escape(tmp_path, monkeypatch):
    import devtools.benchmarks.swe_bench.swebench_predictions as swe_predictions

    input_jsonl = tmp_path / "instances.jsonl"
    output_jsonl = tmp_path / "predictions.jsonl"
    logs_dir = tmp_path / "logs"
    input_jsonl.write_text(
        json.dumps({"instance_id": "../escape", "workspace_root": "/missing", "problem_statement": "fix"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swebench_predictions.py",
            "--input",
            str(input_jsonl),
            "--output",
            str(output_jsonl),
            "--logs-dir",
            str(logs_dir),
            "--continue-on-error",
        ],
    )

    assert swe_predictions.main() == 0
    errors = json.loads((tmp_path / "predictions.jsonl.errors.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert errors["reason_code"] == "invalid_instance_id"
    assert not (tmp_path / "escape").exists()


def test_swe_pro_predictions_rejects_unsafe_instance_id_before_patch_path(tmp_path, monkeypatch):
    import devtools.benchmarks.swe_bench_pro.pro_predictions as pro_predictions

    repo = tmp_path / "repo"
    repo.mkdir()
    input_jsonl = tmp_path / "instances.jsonl"
    output_jsonl = tmp_path / "predictions.jsonl"
    patch_dir = tmp_path / "patches"
    input_jsonl.write_text(
        json.dumps({"instance_id": "../escape", "repo_dir": str(repo), "base_commit": "HEAD"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pro_predictions, "_capture_patch", lambda *a, **k: pytest.fail("unsafe id should fail before capture"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pro_predictions.py",
            "--input",
            str(input_jsonl),
            "--output",
            str(output_jsonl),
            "--patch-dir",
            str(patch_dir),
        ],
    )

    with pytest.raises(ValueError, match="single safe path component"):
        pro_predictions.main()
    assert not (tmp_path / "escape").exists()


def test_benchmark_output_helpers_reject_repo_internal_outputs(tmp_path, monkeypatch):
    import devtools.benchmarks.swe_bench.swebench_predictions as swe_predictions
    import devtools.benchmarks.terminal_bench.run_harbor_smoke as harbor_smoke

    input_jsonl = tmp_path / "instances.jsonl"
    input_jsonl.write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["swebench_predictions.py", "--input", str(input_jsonl), "--output", str(REPO_ROOT / "devtools" / "bad.jsonl")])
    with pytest.raises(ValueError, match="benchmark run output must not be under repo"):
        swe_predictions.main()

    monkeypatch.setattr(sys, "argv", ["run_harbor_smoke.py", "--run-root", str(REPO_ROOT / "devtools" / "bad_run")])
    with pytest.raises(ValueError, match="benchmark run output must not be under repo"):
        harbor_smoke.main()


def test_osworld_logs_only_normalizer(tmp_path):
    bundle = tmp_path / "osworld_logs"
    (bundle / "sample1").mkdir(parents=True)
    (bundle / "SUMMARY.json").write_text(json.dumps({"count": 1}), encoding="utf-8")
    (bundle / "sample_manifest.json").write_text(json.dumps({"samples": ["sample1"]}), encoding="utf-8")
    (bundle / "trace_manifest.json").write_text(json.dumps({"traces": ["sample1/traj.jsonl"]}), encoding="utf-8")
    (bundle / "sample1" / "traj.jsonl").write_text(
        json.dumps({"type": "start"}) + "\n" + json.dumps({"type": "end"}) + "\n",
        encoding="utf-8",
    )

    normalized = normalize_bundle(bundle)

    assert normalized["traj_count"] == 1
    assert normalized["traces"][0]["events"] == 2
    assert normalized["traces"][0]["last_type"] == "end"


def test_osworld_logs_only_normalizer_accepts_nested_trace_manifests(tmp_path):
    bundle = tmp_path / "osworld_logs"
    sample = bundle / "chrome" / "sample1"
    (sample / "traces").mkdir(parents=True)
    (bundle / "SUMMARY.json").write_text(json.dumps({"count": 1}), encoding="utf-8")
    (bundle / "sample_manifest.json").write_text(json.dumps({"samples": ["sample1"]}), encoding="utf-8")
    (sample / "traces" / "trace_manifest.json").write_text(json.dumps({"trace": "sample1"}), encoding="utf-8")
    (sample / "traj.jsonl").write_text(json.dumps({"event": "done"}) + "\n", encoding="utf-8")

    normalized = normalize_bundle(bundle)

    assert normalized["trace_manifest"]["trace_manifest_paths"] == ["chrome/sample1/traces/trace_manifest.json"]
    assert normalized["traj_count"] == 1


def test_terminal_bench_adapter_quotes_hostile_workspace_dir(tmp_path):
    from devtools.benchmarks.terminal_bench.harbor_installed_agent import OuroborosTerminalBenchAgent

    class FakeResult:
        return_code = 0
        stdout = '{"return_code": 0}\n'
        stderr = ""

    class FakeEnvironment:
        def __init__(self):
            self.calls = []

        async def exec(self, **kwargs):
            self.calls.append(kwargs)
            return FakeResult()

    hostile = "/tmp/ws'; touch /tmp/pwn; echo '"
    agent = OuroborosTerminalBenchAgent(logs_dir=tmp_path, workspace_dir=hostile)
    environment = FakeEnvironment()

    asyncio.run(agent._resolve_workspace_dir(environment))
    asyncio.run(agent._ensure_workspace_git_root(environment))
    summary = asyncio.run(agent._run_ouroboros_task(environment, {}))

    assert summary["return_code"] == 0
    quoted = shlex.quote(hostile)
    assert environment.calls[0]["command"] == f"test -d {quoted}"
    git_command = environment.calls[1]["command"]
    assert f"workspace_dir={quoted}" in git_command
    assert "cd \"$workspace_dir\"" in git_command
    runner_command = environment.calls[-1]["command"]
    runner = runner_command.split("cat > /tmp/run_ouroboros_task.py <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    assert f'"workspace_root": {json.dumps(hostile)}' in runner
    compile(runner, "run_ouroboros_task.py", "exec")
