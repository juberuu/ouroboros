"""Tests for module size gate constant (MAX_MODULE_LINES).

Regression: MAX_MODULE_LINES was raised from 1250 to 1600 to accommodate
naturally-growing review pipeline modules without requiring continuous
trivial line-trimming work.
"""
from ouroboros.review import MAX_MODULE_LINES


def test_max_module_lines_value():
    """MAX_MODULE_LINES must be 1600 (raised from 1250)."""
    assert MAX_MODULE_LINES == 1600


def test_max_module_lines_is_positive_int():
    assert isinstance(MAX_MODULE_LINES, int)
    assert MAX_MODULE_LINES > 0
