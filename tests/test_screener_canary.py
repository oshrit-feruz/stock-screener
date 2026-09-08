"""The canary's rules, tested without a network.

scripts/screener_canary.py probes the live /api/screener and decides whether it
is serving what a user needs today. `check` is pure — it takes the response
pieces and the calendar's answer — so every rule is pinned here against fake
responses. The freshness rule is the one that matters: it is deliberately
stricter than the service's own four-trading-day window, because a canary that
only checked the status code would fire days after the pipeline died.
"""
from __future__ import annotations

from datetime import date

import pytest

from scripts.screener_canary import check

_TODAY = date(2026, 9, 8)       # a Tuesday
_YESTERDAY = "2026-09-04"       # the previous trading day (Labor Day between)


def _good(computed_on: str = "2026-09-08") -> dict:
    return {"computed_on": computed_on, "full_ranking": [{"ticker": "AAPL"}],
            "buy_signals": []}


# ── healthy ─────────────────────────────────────────────────────────────────

def test_todays_result_on_a_trading_day_is_healthy():
    assert check(200, _good(), _TODAY, trading_day=True) is None


def test_a_recent_result_on_a_closed_day_is_healthy():
    """Sunday: nothing could have been published today, so the service's own
    window is the right bar and the canary does not second-guess it."""
    assert check(200, _good(_YESTERDAY), _TODAY, trading_day=False) is None


# ── each failure names the FIRST thing wrong ────────────────────────────────

def test_a_503_is_reported_with_the_services_reason():
    reason = check(503, {"detail": "No published screener result within the last 4 trading days"},
                   _TODAY, trading_day=True)
    assert reason is not None
    assert reason.startswith("HTTP 503")
    assert "4 trading days" in reason, "the service's own explanation must be carried through"


def test_a_transport_failure_is_reported_as_no_response():
    assert check(0, None, _TODAY, trading_day=True).startswith("HTTP 0")


def test_an_empty_ranking_is_a_failure_even_with_a_200():
    """The exact shape of the outage that motivated this: a scan that scored
    nothing, served as a quiet day."""
    reason = check(200, {"computed_on": "2026-09-08", "full_ranking": []}, _TODAY, trading_day=True)
    assert reason is not None
    assert "empty ranking" in reason


def test_a_missing_ranking_is_a_failure():
    assert check(200, {"computed_on": "2026-09-08"}, _TODAY, trading_day=True) is not None


def test_a_non_object_body_is_a_failure():
    assert check(200, ["not", "an", "object"], _TODAY, trading_day=True) is not None


def test_yesterdays_result_on_a_trading_day_is_stale():
    """The pipeline died and the service is still serving Friday. The service
    itself would keep doing that for four trading days — the canary must not."""
    reason = check(200, _good(_YESTERDAY), _TODAY, trading_day=True)
    assert reason is not None
    assert "stale" in reason
    assert _YESTERDAY in reason, "the reason must say what was served"
    assert _TODAY.isoformat() in reason, "and what was expected"


def test_freshness_is_checked_only_after_the_ranking():
    """Order pins that the reason is the root cause, not a consequence: an
    empty ranking is reported as empty, not as stale."""
    reason = check(200, {"computed_on": _YESTERDAY, "full_ranking": []}, _TODAY, trading_day=True)
    assert "empty ranking" in reason
    assert "stale" not in reason


@pytest.mark.parametrize("status", [301, 404, 500, 502])
def test_any_non_200_is_a_failure(status):
    assert check(status, _good(), _TODAY, trading_day=True) is not None


# ── the probe answers about THIS endpoint ───────────────────────────────────

def test_the_probe_does_not_follow_redirects(monkeypatch):
    """A 301 to some other 200 must reach check() as a 301, not as whatever
    the redirect target said. Pins the kwarg rather than the behaviour so the
    test needs no network."""
    import scripts.screener_canary as canary
    seen = {}

    class _Resp:
        status_code = 200
        text = "{}"
        def json(self):
            return {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return _Resp()

    monkeypatch.setattr(canary.requests, "get", fake_get)
    canary._fetch("https://example.invalid/api/screener")
    assert seen.get("allow_redirects") is False
