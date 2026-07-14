"""Monotonic lifecycle guard for write_task_result (v6.7.0-rc.1).

Pins the "ghost subagent" / status-corruption protections:
- a stale scheduled/running mirror cannot overwrite a cancel-intent or terminal
- a terminal status is sticky against a *different* terminal status
- cancel_requested still advances to the real terminal
- normal forward progress and same-status enrichment are unaffected
"""

import pytest

from ouroboros import task_results as tr


@pytest.fixture()
def drive(tmp_path):
    return tmp_path


def _status(drive, tid):
    return tr.load_task_result(drive, tid)["status"]


def test_terminal_not_regressed_by_running(drive):
    tr.write_task_result(drive, "t", tr.STATUS_CANCELLED, result="cancelled")
    tr.write_task_result(drive, "t", tr.STATUS_RUNNING, result="stale mirror")
    assert _status(drive, "t") == tr.STATUS_CANCELLED


def test_terminal_is_sticky_against_other_terminal(drive):
    tr.write_task_result(drive, "t", tr.STATUS_CANCELLED)
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED, result="late completion")
    assert _status(drive, "t") == tr.STATUS_CANCELLED

    tr.write_task_result(drive, "u", tr.STATUS_COMPLETED)
    tr.write_task_result(drive, "u", tr.STATUS_FAILED)
    assert _status(drive, "u") == tr.STATUS_COMPLETED


def test_terminal_sticky_against_unknown_status(drive):
    # A typo / future / unranked status must NOT overwrite a terminal one.
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED, result="done")
    tr.write_task_result(drive, "t", "weird_unranked_status")
    assert _status(drive, "t") == tr.STATUS_COMPLETED


def test_same_terminal_status_enrichment_allowed(drive):
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED, result="first")
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED, result="enriched", trace_summary="trace")
    data = tr.load_task_result(drive, "t")
    assert data["status"] == tr.STATUS_COMPLETED
    assert data["result"] == "enriched"
    assert data["trace_summary"] == "trace"


def test_cancel_requested_blocks_running_but_allows_cancelled(drive):
    tr.write_task_result(drive, "t", tr.STATUS_CANCEL_REQUESTED)
    tr.write_task_result(drive, "t", tr.STATUS_RUNNING)
    assert _status(drive, "t") == tr.STATUS_CANCEL_REQUESTED
    tr.write_task_result(drive, "t", tr.STATUS_CANCELLED, result="done")
    assert _status(drive, "t") == tr.STATUS_CANCELLED


def test_cancel_requested_not_masked_by_late_completion(drive):
    # A worker finishing just after the cancel latch must NOT flip the task to
    # "completed" — the requested cancel wins.
    tr.write_task_result(drive, "t", tr.STATUS_CANCEL_REQUESTED)
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED, result="late success")
    assert _status(drive, "t") == tr.STATUS_CANCEL_REQUESTED
    # ...but a real teardown crash (failed) or the cancellation itself may land.
    tr.write_task_result(drive, "t", tr.STATUS_CANCELLED)
    assert _status(drive, "t") == tr.STATUS_CANCELLED


def test_normal_forward_progress_and_retry(drive):
    tr.write_task_result(drive, "t", tr.STATUS_SCHEDULED)
    tr.write_task_result(drive, "t", tr.STATUS_RUNNING)
    tr.write_task_result(drive, "t", tr.STATUS_INTERRUPTED)  # pre-requeue
    tr.write_task_result(drive, "t", tr.STATUS_RUNNING)      # retry
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED)
    assert _status(drive, "t") == tr.STATUS_COMPLETED


def test_updated_at_is_written(drive):
    tr.write_task_result(drive, "t", tr.STATUS_SCHEDULED)
    assert tr.load_task_result(drive, "t").get("updated_at")


def test_llm_project_name_uses_cleaned_model_title():
    """v6.40: the real LLM naming path returns the model's title run through clean_model_title
    (lexical clean — strips wrapping quotes, P5), not the raw model string."""
    from ouroboros import project_naming

    class _FakeClient:
        def chat(self, **kw):
            return ({"content": '"Cyber Racing Arena"'}, {"cost": 0.0})

    name = project_naming.llm_project_name(
        "build me a top-down neon racing game", llm_client=_FakeClient(),
    )
    assert name == "Cyber Racing Arena", f"expected cleaned title, got {name!r}"


def test_read_paths_do_not_create_task_results_dir(tmp_path):
    """v6.40.0: a READ/LIST scan of a never-provisioned root must NOT materialise the
    ``task_results`` directory (regression: an unguarded scan created stray dirs)."""
    root = tmp_path / "never_provisioned"
    assert tr.list_task_results(root) == []
    assert tr.load_task_result(root, "missing") is None
    assert not (root / "task_results").exists(), "read must not create the dir"
    # WRITE still provisions it.
    tr.write_task_result(root, "t", tr.STATUS_SCHEDULED)
    assert (root / "task_results").is_dir()


def test_proactive_namer_persists_name_on_already_terminal_task(tmp_path, monkeypatch):
    """v6.40.0 #1: the proactive namer must persist ``suggested_name`` even when the task
    already raced to a terminal status — it enriches under the CURRENT status instead of a
    regressing RUNNING write (which the monotonic guard would drop, losing the convert-reuse
    name)."""
    import threading
    import time

    from ouroboros import project_naming

    tr.write_task_result(tmp_path, "t", tr.STATUS_COMPLETED, result="fast done")
    monkeypatch.setattr(project_naming, "llm_project_name", lambda *a, **k: "Nice Title")
    project_naming.spawn_proactive_namer(tmp_path, "t", "build me a thing")
    for _ in range(100):  # join the daemon namer thread (best-effort, bounded)
        if not any(th.name == "namer-t" for th in threading.enumerate()):
            break
        time.sleep(0.02)
    r = tr.load_task_result(tmp_path, "t")
    assert r["status"] == tr.STATUS_COMPLETED, "namer must NOT regress a terminal task to running"
    assert r.get("suggested_name") == "Nice Title", "suggested_name must survive on a terminal task"


def test_proactive_namer_late_settlement_refreshes_cost_without_late_name(tmp_path, monkeypatch):
    """A provider thread outliving the cosmetic deadline still closes accounting only."""
    import threading
    import time

    from ouroboros import agent_task_pipeline, project_naming, usage_accounting

    tr.write_task_result(
        tmp_path,
        "late-root",
        tr.STATUS_COMPLETED,
        root_task_id="late-root",
        cost_usd=0.0,
        cost_final=True,
        root_phase_checkpoint={"post_task_synthesis": "completed"},
    )
    entered = threading.Event()
    release = threading.Event()

    def late_paid_name(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        attempt = usage_accounting.reserve_attempt(usage_accounting.AttemptRequest(
            model="openai/gpt-5.2",
            provider="openai",
            reservation_usd=0.25,
            drive_root=tmp_path,
            task_id="late-root",
            root_task_id="late-root",
            global_limit_usd=5.0,
            root_limit_usd=5.0,
        ))
        usage_accounting.mark_dispatched(attempt)
        usage_accounting.settle_attempt(attempt, {}, cost_usd=0.25, cost_final=True)
        return "Too Late"

    monkeypatch.setattr(project_naming, "llm_project_name", late_paid_name)
    monkeypatch.setattr(project_naming, "_naming_timeout_sec", lambda: -29.98)
    broadcasts = []
    project_naming.spawn_proactive_namer(
        tmp_path, "late-root", "build a thing", broadcast=broadcasts.append,
    )
    assert entered.wait(1)
    for _ in range(100):
        if not any(th.name == "namer-late-root" for th in threading.enumerate()):
            break
        time.sleep(0.01)
    assert not any(th.name == "namer-late-root" for th in threading.enumerate())

    release.set()
    for _ in range(200):
        stored = tr.load_task_result(tmp_path, "late-root")
        if stored.get("cost_usd") == 0.25:
            break
        time.sleep(0.01)
    stored = tr.load_task_result(tmp_path, "late-root")
    assert stored["cost_usd"] == 0.25
    assert stored["cost_usd_with_children"] == 0.25
    assert stored["cost_final"] is True
    assert stored.get("suggested_name") is None
    assert broadcasts == []
    assert agent_task_pipeline._root_post_task_already_completed(
        type("Env", (), {"drive_root": tmp_path})(),
        {"id": "late-root", "root_task_id": "late-root"},
    )


def test_read_with_stub_root_leaks_no_cwd_dir(tmp_path, monkeypatch):
    """The exact pollution repro: a MagicMock-derived root (``MagicMock/mock``) reaching a
    READ scan must not create a ``MagicMock`` tree in the cwd."""
    import pathlib
    from unittest.mock import MagicMock

    monkeypatch.chdir(tmp_path)
    stub_root = pathlib.Path(MagicMock()).parent  # == Path("MagicMock/mock")
    assert tr.list_task_results(stub_root) == []
    assert tr.load_task_result(stub_root, "x") is None
    leaked = [p.name for p in pathlib.Path(".").iterdir() if "MagicMock" in p.name]
    assert leaked == [], f"read scan leaked mock-named paths: {leaked}"
