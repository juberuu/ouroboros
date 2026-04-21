import threading

from ouroboros.improvement_backlog import (
    append_backlog_items,
    backlog_path,
    format_backlog_digest,
    load_backlog_items,
)


def test_append_and_load_backlog_items(tmp_path):
    added = append_backlog_items(tmp_path, [{
        "summary": "Resolve recurring review blocker: tests_affected",
        "category": "review",
        "source": "review_evidence",
        "task_id": "task-1",
        "evidence": "Fix the missing test before commit",
        "context": "blocked commit",
        "proposed_next_step": "Run plan_task for the narrow fix.",
    }])

    assert added == 1
    path = backlog_path(tmp_path)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "# Improvement Backlog" in text
    assert "Resolve recurring review blocker: tests_affected" in text

    items = load_backlog_items(tmp_path)
    assert len(items) == 1
    assert items[0]["category"] == "review"
    assert items[0]["task_id"] == "task-1"


def test_append_dedupes_by_fingerprint(tmp_path):
    item = {
        "summary": "Reduce recurring task friction around SHELL_EXIT_ERROR",
        "category": "process",
        "source": "execution_reflection",
        "task_id": "task-2",
        "evidence": "SHELL_EXIT_ERROR",
    }
    assert append_backlog_items(tmp_path, [item]) == 1
    assert append_backlog_items(tmp_path, [item]) == 0
    assert len(load_backlog_items(tmp_path)) == 1


def test_append_concurrent_writers_do_not_drop_entries(tmp_path):
    item_a = {
        "summary": "Investigate recurring grep timeout",
        "category": "tooling",
        "source": "execution_reflection",
        "task_id": "task-a",
        "evidence": "TOOL_TIMEOUT",
        "fingerprint": "fp-a",
        "id": "ibl-fp-a",
    }
    item_b = {
        "summary": "Resolve recurring review blocker: tests_affected",
        "category": "review",
        "source": "review_evidence",
        "task_id": "task-b",
        "evidence": "Fix the missing test before commit",
        "fingerprint": "fp-b",
        "id": "ibl-fp-b",
    }

    results = []

    def _append(item):
        results.append(append_backlog_items(tmp_path, [item]))

    t1 = threading.Thread(target=_append, args=(item_a,))
    t2 = threading.Thread(target=_append, args=(item_b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(results) == [1, 1]
    items = load_backlog_items(tmp_path)
    fingerprints = {item["fingerprint"] for item in items}
    assert {"fp-a", "fp-b"}.issubset(fingerprints)


def test_format_backlog_digest_includes_omission_note(tmp_path):
    for idx in range(7):
        append_backlog_items(tmp_path, [{
            "summary": f"Item {idx}",
            "category": "process",
            "source": "execution_reflection",
            "task_id": f"task-{idx}",
            "evidence": f"marker-{idx}",
            "fingerprint": f"fp-{idx}",
            "id": f"ibl-fp-{idx}",
            "created_at": f"2026-04-14T09:0{idx}:00+00:00",
        }])

    digest = format_backlog_digest(tmp_path, limit=3, max_chars=2500)
    assert "## Improvement Backlog" in digest
    assert "open_items: 7" in digest
    assert "⚠️ OMISSION NOTE: 4 additional open backlog items not shown" in digest
