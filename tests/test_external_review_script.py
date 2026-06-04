from types import SimpleNamespace
from pathlib import Path

from scripts.run_external_review import _scope_review_skipped


def test_external_review_script_marks_budget_exceeded_scope_as_skipped():
    source = Path("scripts/run_external_review.py").read_text(encoding="utf-8")
    assert "v6.10.0" not in source
    assert "Google Colab" not in source
    assert _scope_review_skipped(SimpleNamespace(status="budget_exceeded"), []) is True
    assert _scope_review_skipped(
        SimpleNamespace(status="responded"),
        [{"item": "scope_review_skipped", "severity": "advisory"}],
    ) is True
    assert _scope_review_skipped(SimpleNamespace(status="responded"), []) is False
