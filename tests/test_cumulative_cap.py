"""Tests for the per-signal-type cap in cumulative scoring."""
from unittest.mock import MagicMock

from signal_agent.scoring.scorer import _cap_and_sum


def _sig(id, signal_type, raw_score):
    s = MagicMock()
    s.id = id
    s.signal_type = signal_type
    s.raw_score = raw_score
    return s


def test_caps_per_signal_type_and_keeps_highest():
    # 5 articles about one event, same signal_type — cap at 3 keeps the top 3.
    rows = [_sig(i, "news.ai_product_launch", 5.0) for i in range(1, 6)]
    total, contributing = _cap_and_sum(rows, cap=3)
    assert total == 15.0
    assert len(contributing) == 3


def test_cap_is_per_type_not_global():
    rows = [
        _sig(1, "news.ai_product_launch", 5.0),
        _sig(2, "news.ai_product_launch", 5.0),
        _sig(3, "news.ai_product_launch", 5.0),
        _sig(4, "news.ai_product_launch", 5.0),   # dropped (4th of its type)
        _sig(5, "job_posting.ai_governance", 8.0),  # different type, kept
    ]
    total, contributing = _cap_and_sum(rows, cap=3)
    assert total == 23.0  # 3*5 + 8
    assert 4 not in contributing
    assert 5 in contributing


def test_keeps_highest_scoring_within_type():
    rows = [
        _sig(1, "news.exec_hire_ai", 2.0),
        _sig(2, "news.exec_hire_ai", 9.0),
        _sig(3, "news.exec_hire_ai", 5.0),
    ]
    total, contributing = _cap_and_sum(rows, cap=2)
    assert total == 14.0        # 9 + 5, the 2.0 dropped
    assert set(contributing) == {2, 3}


def test_cap_zero_disables_cap():
    rows = [_sig(i, "news.ai_product_launch", 5.0) for i in range(1, 6)]
    total, contributing = _cap_and_sum(rows, cap=0)
    assert total == 25.0
    assert len(contributing) == 5
