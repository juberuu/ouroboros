import os
import pathlib
import subprocess
import textwrap
import inspect


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def test_hermetic_pytest_applies_candidate_diff_and_scrubs_live_env(tmp_path, monkeypatch):
    from ouroboros.preflight_runner import run_hermetic_pytest

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "checkout", "-b", "ouroboros")
    (repo / "value.py").write_text("FLAG = False\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_env_and_diff.py").write_text(
        textwrap.dedent(
            """
            import os
            import extra_value
            import value


            def test_candidate_diff_and_env_are_hermetic():
                assert value.FLAG is True
                assert extra_value.FLAG is True
                assert "OUROBOROS_MANAGED_BY_LAUNCHER" not in os.environ
                assert "ouroboros-preflight-" in os.environ["OUROBOROS_DATA_DIR"]
                assert os.environ["OUROBOROS_SETTINGS_PATH"].startswith(os.environ["OUROBOROS_DATA_DIR"])
                assert "ouroboros-preflight-" in os.environ["OUROBOROS_REPO_DIR"]
            """
        ),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "value.py").write_text("FLAG = True\n", encoding="utf-8")
    (repo / "extra_value.py").write_text("FLAG = True\n", encoding="utf-8")

    monkeypatch.setenv("OUROBOROS_MANAGED_BY_LAUNCHER", "1")
    result = run_hermetic_pytest(repo, timeout=30)

    assert result is None


def test_hermetic_pytest_timeout_uses_process_tree_cleanup():
    from ouroboros import preflight_runner

    source = inspect.getsource(preflight_runner.run_hermetic_pytest)
    assert "subprocess.Popen" in source
    assert "kill_process_tree" in source
