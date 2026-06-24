"""Tests for the decoupled Clay-push decision (wider/lower bar than Slack alerts)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from signal_agent.scoring.scorer import should_push_to_clay


def _make_signal(signal_type="job_posting.ml_platform", raw_score=3.0):
    sig = MagicMock()
    sig.signal_type = signal_type
    sig.raw_score = raw_score
    return sig


def _make_rollup(cumulative=8.0):
    r = MagicMock()
    r.cumulative_score = cumulative
    return r


def _make_company(last_clay_pushed_at=None):
    c = MagicMock()
    c.id = 1
    c.last_clay_pushed_at = last_clay_pushed_at
    return c


def _apply_settings(s_mock):
    s_mock.clay_push_score_threshold = 6.0
    s_mock.clay_push_cooldown_days = 30


def test_below_clay_threshold_not_pushed():
    sig = _make_signal(raw_score=2.0)
    rollup = _make_rollup(cumulative=4.0)  # below 6.0 bar
    company = _make_company()
    with patch("signal_agent.scoring.scorer.settings") as s:
        _apply_settings(s)
        d = should_push_to_clay(rollup, sig, company)
    assert d.should_push is False
    assert d.reason == "below_clay_threshold"


def test_cumulative_above_bar_first_push():
    sig = _make_signal(raw_score=2.0)
    rollup = _make_rollup(cumulative=7.0)  # cumulative clears the bar
    company = _make_company(last_clay_pushed_at=None)
    with patch("signal_agent.scoring.scorer.settings") as s:
        _apply_settings(s)
        d = should_push_to_clay(rollup, sig, company)
    assert d.should_push is True
    assert d.reason == "first_push"


def test_single_signal_above_bar_pushes_even_if_cumulative_low():
    sig = _make_signal(raw_score=9.0)
    rollup = _make_rollup(cumulative=1.0)  # cumulative below bar, single clears it
    company = _make_company()
    with patch("signal_agent.scoring.scorer.settings") as s:
        _apply_settings(s)
        d = should_push_to_clay(rollup, sig, company)
    assert d.should_push is True
    assert d.reason == "first_push"


def test_always_alert_type_pushes_regardless_of_score():
    sig = _make_signal(signal_type="news.exec_hire_ai", raw_score=0.1)
    rollup = _make_rollup(cumulative=0.0)  # nothing clears the bar...
    company = _make_company()
    with patch("signal_agent.scoring.scorer.settings") as s:
        _apply_settings(s)
        d = should_push_to_clay(rollup, sig, company)
    assert d.should_push is True  # ...but always-alert types still push
    assert d.reason == "first_push"


def test_recent_push_in_cooldown_suppressed():
    now = datetime.now(timezone.utc)
    sig = _make_signal(raw_score=9.0)
    rollup = _make_rollup(cumulative=20.0)
    company = _make_company(last_clay_pushed_at=now - timedelta(days=5))  # < 30d
    with patch("signal_agent.scoring.scorer.settings") as s:
        _apply_settings(s)
        d = should_push_to_clay(rollup, sig, company, now=now)
    assert d.should_push is False
    assert d.reason == "in_cooldown"


def test_push_cooldown_expired_repushes():
    now = datetime.now(timezone.utc)
    sig = _make_signal(raw_score=9.0)
    rollup = _make_rollup(cumulative=20.0)
    company = _make_company(last_clay_pushed_at=now - timedelta(days=45))  # > 30d
    with patch("signal_agent.scoring.scorer.settings") as s:
        _apply_settings(s)
        d = should_push_to_clay(rollup, sig, company, now=now)
    assert d.should_push is True
    assert d.reason == "cooldown_expired"
