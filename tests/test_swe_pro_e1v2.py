"""Focused tests for the SWE-bench-Pro e1v2 harness producer/consumer contracts.

Split out of tests/test_devtools_benchmarks.py to keep that module focused and
small. Covers the run_pro -> auto_run timeline handoff (infra-flag persistence
and stop/skip semantics), the `--cadence off` settings contract, and the
build_predictions leaderboard-shaped output schema.
"""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_e1v2_timeline_row_persists_infra_flags():
    """Producer side of the run_pro -> auto_run handoff: the timeline row MUST carry
    the infra non-execution markers, else auto_run cannot stop on a secret refusal."""
    from devtools.benchmarks.swe_bench_pro.e1v2.run_pro import build_timeline_row

    res = {"model_patch": "", "timed_out": False, "infra_suspect": True,
           "secret_opt_in_required": True, "libc_skip": "musl:vol", "health_rollback": False,
           "api_errors": 0, "api_ctx": 0, "refl_line": "", "quiet_line": "",
           "selfedit": {}, "evolution_degraded": False, "absorb_reason": ""}
    row = build_timeline_row(1, "inst", res, 0.0, ["INFRA"])
    assert row["infra_suspect"] is True
    assert row["secret_opt_in_required"] is True
    assert row["libc_skip"] == "musl:vol"


def test_e1v2_auto_run_one_stops_on_secret_and_skips_infra(tmp_path, monkeypatch):
    """Consumer side: a secret-opt-in refusal hard-stops; an infra skip is non-LEGIT
    (patch_bytes=None), never snapshotted as a completed last-good."""
    import types as _types
    from devtools.benchmarks.swe_bench_pro.e1v2 import auto_run

    args = _types.SimpleNamespace(total_budget=10.0, per_task_cost=5.0)

    def _write_timeline(payload):
        (tmp_path / "timeline.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    # secret-injection refusal -> hard stop (config error, not a transient)
    monkeypatch.setattr(auto_run.subprocess, "run",
                        lambda *a, **k: _write_timeline(
                            {"patch_bytes": 0, "api_errors": 0, "instance_id": "x",
                             "secret_opt_in_required": True}))
    with pytest.raises(SystemExit):
        auto_run.run_one(1, tmp_path, args)

    # generic infra skip -> non-LEGIT (pb=None), so it is retried/stopped not counted ok
    monkeypatch.setattr(auto_run.subprocess, "run",
                        lambda *a, **k: _write_timeline(
                            {"patch_bytes": 0, "api_errors": 0, "instance_id": "y",
                             "infra_suspect": True}))
    pb, _ae, _iid, _degraded = auto_run.run_one(1, tmp_path, args)
    assert pb is None


def test_e1v2_cadence_off_disables_post_task_evolution(tmp_path):
    """`--cadence off` must disable evolution via the documented POST_TASK_EVOLUTION
    contract (false), not leave it 'true' relying on a downstream cadence guard."""
    from devtools.benchmarks.swe_bench_pro.e1v2.run_pro import derive_run_settings

    base = REPO_ROOT / "devtools" / "benchmarks" / "swe_bench_pro" / "e1v2" / "settings_base.json"
    off_dir = tmp_path / "off"; off_dir.mkdir()
    on_dir = tmp_path / "on"; on_dir.mkdir()
    p_off = derive_run_settings(str(base), off_dir, "m", 10.0, 5.0,
                                post_task_evolution=True, cadence="off")
    p_on = derive_run_settings(str(base), on_dir, "m", 10.0, 5.0,
                               post_task_evolution=True, cadence="every_n:1")
    assert json.loads(p_off.read_text(encoding="utf-8"))["OUROBOROS_POST_TASK_EVOLUTION"] == "false"
    assert json.loads(p_on.read_text(encoding="utf-8"))["OUROBOROS_POST_TASK_EVOLUTION"] == "true"


def test_e1v2_build_predictions_emits_leaderboard_schema(tmp_path, monkeypatch):
    """build_predictions rows must carry the leaderboard-shaped model_name_or_path,
    not just {instance_id, model_patch}, or the artifact is harness-incompatible."""
    import importlib
    bp = importlib.import_module("devtools.benchmarks.swe_bench_pro.e1v2.build_predictions")

    # Point the consolidated run root at a temp tree with one patched instance.
    full = tmp_path / "pro_e1_full"
    (full / "inst__a").mkdir(parents=True)
    (full / "inst__a" / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    csv_path = tmp_path / "order.csv"
    csv_path.write_text("idx,instance_id\n1,inst__a\n", encoding="utf-8")
    out_path = tmp_path / "preds.jsonl"
    monkeypatch.setattr(bp, "FULL", full)
    monkeypatch.setattr(bp, "CSV", csv_path)
    monkeypatch.setattr(
        bp.sys, "argv",
        ["build_predictions.py", "--start", "1", "--end", "1",
         "--out", str(out_path), "--model-name", "ouroboros-e1-pro-test"],
    )
    assert bp.main() == 0
    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows and set(rows[0]) == {"instance_id", "model_name_or_path", "model_patch"}
    assert rows[0]["model_name_or_path"] == "ouroboros-e1-pro-test"
