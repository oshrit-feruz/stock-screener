"""/api/screener must serve the newest PUBLISHED daily result, with provenance.

The daily scan runs in GitHub Actions and is published to the
automation/daily-state branch; Render fetches and serves it (docs/
ARCHITECTURE.md — Actions computes, Render reads). Pinned here:

  * the lookup walks back _DAILY_STATE_LOOKBACK_TDAYS TRADING days and labels
    the result with computed_on — Friday's scan on Sunday is served EXPLICITLY,
    never silently;
  * the window is counted in trading days, not calendar days: a long weekend
    must not spend the budget on days the producer could never have published
    on (that is what took the endpoint down after Labor Day 2026);
  * beyond the window it refuses (503), naming the producer;
  * a published result computed under a superseded universe is discarded by
    the fingerprint check, not served as "slightly stale";
  * the fetch path never triggers a scan.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta

import pytest

import product.api.main as m
import product.screener.daily_screener as ds
from product.screener.daily_screener import ScreenerRow


def _row():
    return ScreenerRow(
        ticker="AAPL", current_price=1.0, high_52w=2.0, drawdown_pct=0.5,
        dip_score=0.8, momentum_score=0.7, volume_score=0.6,
        composite_score=0.75, gate=True, signal="BUY",
    )


def _payload(as_of: date, fp: str) -> dict:
    r = _row()
    return {
        "as_of_date": as_of.isoformat(),
        "universe_fingerprint": fp,
        "buy_signals": [asdict(r)],
        "full_ranking": [asdict(r)],
    }


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(m, "_sc_data", None)
    monkeypatch.setattr(m, "_sc_ts", 0.0)
    monkeypatch.setattr(m, "_sc_warming", False)
    monkeypatch.setattr(m, "_sc_warm_started", 0.0)
    monkeypatch.setattr(m, "_sc_universe_fp", None)
    monkeypatch.setattr(m, "_ALLOW_ONDEMAND_SCAN", False)
    monkeypatch.setattr(m, "load_universe_list", lambda *a, **k: object())
    monkeypatch.setattr(m, "_universe_fingerprint", lambda _u: "fp-live")
    monkeypatch.setattr(
        m, "run_screener",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scan")),
    )
    yield


def _write_local(tmp_path, as_of: date, fp: str = "fp-live"):
    (tmp_path / f"{as_of.isoformat()}.json").write_text(json.dumps(_payload(as_of, fp)))


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _tdays_back(n: int) -> date:
    """The date n trading days before today, through the same predicate the
    code under test walks with — so these tests pin the window's SHAPE and stay
    correct whichever day they are run on, and whether or not the optional NYSE
    calendar is installed (without it the predicate falls back to weekdays)."""
    d = date.today()
    while n > 0:
        d -= timedelta(days=1)
        if m.is_trading_day(d):
            n -= 1
    return d


def test_todays_local_result_served_with_computed_on(tmp_path, monkeypatch):
    _write_local(tmp_path, date.today())
    monkeypatch.setattr(m, "_fetch_published_daily_result", lambda d: False)
    data = m._get_screener_data()
    assert data["computed_on"] == date.today().isoformat()
    assert len(data["full_ranking"]) == 1


def test_gap_serves_the_previous_trading_days_result_labeled(tmp_path, monkeypatch):
    """No file for today (weekend, holiday, pre-run morning): the last session's
    result is served, and computed_on says so. Explicit, never silent."""
    previous = _tdays_back(1)
    _write_local(tmp_path, previous)
    monkeypatch.setattr(m, "_fetch_published_daily_result", lambda d: False)
    data = m._get_screener_data()
    assert data["computed_on"] == previous.isoformat()


def test_missing_locally_is_fetched_from_daily_state(tmp_path, monkeypatch):
    previous = _tdays_back(1)
    fetched = {}
    def fake_get(url, timeout):
        d = url.rsplit("/", 1)[1].removesuffix(".json")
        if d == previous.isoformat():
            fetched["url"] = url
            return _Resp(200, _payload(previous, "fp-live"))
        return _Resp(404)
    monkeypatch.setattr(m.requests, "get", fake_get)
    data = m._get_screener_data()
    assert data["computed_on"] == previous.isoformat()
    assert "data/screener_cache" in fetched["url"]


def test_nothing_within_window_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_fetch_published_daily_result", lambda d: False)
    with pytest.raises(m.ScreenerStateUnavailable):
        m._get_screener_data()
    assert m._sc_warming is False


def test_result_beyond_window_is_not_served(tmp_path, monkeypatch):
    """One trading day past the budget is refused, not served."""
    _write_local(tmp_path, _tdays_back(m._DAILY_STATE_LOOKBACK_TDAYS + 1))
    monkeypatch.setattr(m, "_fetch_published_daily_result", lambda d: False)
    with pytest.raises(m.ScreenerStateUnavailable):
        m._get_screener_data()


def test_oldest_result_inside_the_window_is_served(tmp_path, monkeypatch):
    """The far edge of the budget is INSIDE it — pins the off-by-one that
    separates this test from the one above."""
    oldest = _tdays_back(m._DAILY_STATE_LOOKBACK_TDAYS)
    _write_local(tmp_path, oldest)
    monkeypatch.setattr(m, "_fetch_published_daily_result", lambda d: False)
    assert m._get_screener_data()["computed_on"] == oldest.isoformat()


def test_stale_universe_fingerprint_is_discarded_not_served(tmp_path, monkeypatch):
    """A published result from a superseded universe is wrong, not stale."""
    _write_local(tmp_path, date.today(), fp="fp-last-month")
    monkeypatch.setattr(m, "_fetch_published_daily_result", lambda d: False)
    with pytest.raises(m.ScreenerStateUnavailable):
        m._get_screener_data()


def test_fetch_failure_falls_through_to_refusal_not_scan(tmp_path, monkeypatch):
    def boom(url, timeout):
        raise OSError("network down")
    monkeypatch.setattr(m.requests, "get", boom)
    with pytest.raises(m.ScreenerStateUnavailable):
        m._get_screener_data()   # run_screener autouse-mock would raise if scanned


# ── the window is counted in trading days ───────────────────────────────────
#
# These drive _lookback_dates through an EXPLICIT calendar rather than the real
# one. pandas_market_calendars is an optional dependency — without it
# is_trading_day falls back to a plain weekday check, and a test asserting that
# Labor Day is skipped would then pass in CI and fail on a developer's machine
# for reasons that have nothing to do with the window. The calendar itself is
# covered in tests/test_run_daily.py; what is at stake here is the counting.

_LABOR_DAY = date(2026, 9, 7)


@pytest.fixture
def _nyse(monkeypatch):
    """Weekends closed, plus US Labor Day 2026."""
    monkeypatch.setattr(
        m, "is_trading_day",
        lambda d: d.weekday() < 5 and d != _LABOR_DAY,
    )


def test_lookback_skips_days_the_market_was_closed(_nyse):
    """A long weekend must not consume the budget. Tuesday 2026-09-08 is the
    day after Labor Day: the four sessions behind it are Fri/Thu/Wed/Tue, not
    Mon/Sun/Sat/Fri. Counting calendar days reached back only as far as 09-04 —
    which is exactly how the endpoint 503'd with usable results sitting
    unreachable on 09-03 and 09-02."""
    dates = list(m._lookback_dates(date(2026, 9, 8)))
    assert dates == [
        date(2026, 9, 8),                        # today, always tried first
        date(2026, 9, 4), date(2026, 9, 3),
        date(2026, 9, 2), date(2026, 9, 1),
    ]
    assert _LABOR_DAY not in dates, "a closed day cannot carry a result"
    assert date(2026, 9, 5) not in dates, "Saturday cannot carry a result"
    assert date(2026, 9, 6) not in dates, "Sunday cannot carry a result"


def test_lookback_yields_today_even_when_the_market_is_closed(_nyse):
    """Sunday is still checked first: a file may exist for it, and if one does
    it is the newest thing there is."""
    sunday = date(2026, 9, 6)
    assert list(m._lookback_dates(sunday))[0] == sunday


def test_lookback_over_a_plain_weekend_is_unchanged(_nyse):
    """The common case must not regress: from a Monday the window is the four
    preceding weekdays, exactly what calendar counting gave."""
    assert list(m._lookback_dates(date(2026, 9, 14))) == [
        date(2026, 9, 14),
        date(2026, 9, 11), date(2026, 9, 10),
        date(2026, 9, 9),  date(2026, 9, 8),
    ]


def test_lookback_terminates_if_the_calendar_calls_everything_a_holiday(monkeypatch):
    """The walk behind the budget is bounded. A calendar that never reports an
    open day would otherwise spin forever inside a request."""
    monkeypatch.setattr(m, "is_trading_day", lambda d: False)
    assert list(m._lookback_dates(date(2026, 9, 8))) == [date(2026, 9, 8)]
