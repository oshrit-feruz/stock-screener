"""Unit tests for the 8-K veto integration in scripts/run_portfolio_sim.simulate
(veto_fn / veto_log params).

Only the veto wiring introduced by this PR is in scope here — simulate()'s
pre-existing sizing/exit/skip-reason behaviour is exercised only incidentally,
as the minimum needed to observe entries being blocked. Synthetic
single-ticker calendars are used; no network access.
"""
import pandas as pd
import pytest

from scripts.run_portfolio_sim import _INITIAL_CAP, simulate


def _make_calendar(n=5):
    return pd.bdate_range("2023-01-02", periods=n)


def _base_args(master_cal, ticker="AAA", signal_day_idx=0, comp=0.80, price=100.0):
    prices_wide = pd.DataFrame(
        {ticker: [price + i for i in range(len(master_cal))]}, index=master_cal
    )
    crossings_by_ticker = {ticker: [(master_cal[signal_day_idx], comp, price, -0.30)]}
    return crossings_by_ticker, prices_wide


# ── default behaviour: veto_fn=None reproduces the no-veto baseline ─────────

def test_no_veto_fn_opens_position_as_before():
    master_cal = _make_calendar()
    crossings_by_ticker, prices_wide = _base_args(master_cal)
    result = simulate(crossings_by_ticker, prices_wide, master_cal, pct=0.10, max_pos=10)
    assert len(result["trades"]) == 1
    assert result["trades"][0]["ticker"] == "AAA"
    assert not any(s["reason"] == "veto" for s in result["skipped"])


# ── veto_fn blocks the entry ────────────────────────────────────────────────

def test_veto_fn_blocking_prevents_entry_and_records_skip_reason():
    master_cal = _make_calendar()
    crossings_by_ticker, prices_wide = _base_args(master_cal)

    def veto_fn(ticker, iso_date):
        return True, f"distress filing for {ticker} on {iso_date}"

    result = simulate(
        crossings_by_ticker, prices_wide, master_cal, pct=0.10, max_pos=10, veto_fn=veto_fn
    )
    assert result["trades"] == []
    veto_skips = [s for s in result["skipped"] if s["reason"] == "veto"]
    assert len(veto_skips) == 1
    assert veto_skips[0]["ticker"] == "AAA"
    # No position ever opens, so the portfolio value must stay flat.
    assert (result["daily_values"].values == _INITIAL_CAP).all()


def test_veto_fn_blocking_populates_veto_log():
    master_cal = _make_calendar()
    crossings_by_ticker, prices_wide = _base_args(master_cal, comp=0.80)
    veto_log = []

    def veto_fn(ticker, iso_date):
        return True, "8-K Item 4.02 filed: Non-Reliance on Previously Issued Financial Statements"

    simulate(
        crossings_by_ticker, prices_wide, master_cal, pct=0.10, max_pos=10,
        veto_fn=veto_fn, veto_log=veto_log,
    )
    assert len(veto_log) == 1
    sig_date, ticker, comp, reason = veto_log[0]
    assert sig_date == master_cal[0].date()
    assert ticker == "AAA"
    assert comp == pytest.approx(0.80)
    assert "4.02" in reason


def test_veto_fn_blocking_without_veto_log_does_not_raise():
    master_cal = _make_calendar()
    crossings_by_ticker, prices_wide = _base_args(master_cal)

    def veto_fn(ticker, iso_date):
        return True, "blocked"

    result = simulate(
        crossings_by_ticker, prices_wide, master_cal, pct=0.10, max_pos=10, veto_fn=veto_fn
    )
    assert result["trades"] == []


# ── veto_fn allowing the entry (explicit False) behaves like no veto ───────

def test_veto_fn_returning_false_allows_entry():
    master_cal = _make_calendar()
    crossings_by_ticker, prices_wide = _base_args(master_cal)
    veto_log = []

    def veto_fn(ticker, iso_date):
        return False, ""

    result = simulate(
        crossings_by_ticker, prices_wide, master_cal, pct=0.10, max_pos=10,
        veto_fn=veto_fn, veto_log=veto_log,
    )
    assert len(result["trades"]) == 1
    assert veto_log == []


# ── selective veto: only the flagged ticker is blocked ─────────────────────

def test_veto_fn_blocks_selectively_across_tickers():
    master_cal = _make_calendar()
    prices_wide = pd.DataFrame(
        {"AAA": [100.0] * len(master_cal), "BBB": [50.0] * len(master_cal)},
        index=master_cal,
    )
    crossings_by_ticker = {
        "AAA": [(master_cal[0], 0.90, 100.0, -0.30)],
        "BBB": [(master_cal[0], 0.80, 50.0, -0.20)],
    }
    veto_log = []

    def veto_fn(ticker, iso_date):
        return (ticker == "AAA"), ("distress" if ticker == "AAA" else "")

    result = simulate(
        crossings_by_ticker, prices_wide, master_cal, pct=0.10, max_pos=10,
        veto_fn=veto_fn, veto_log=veto_log,
    )
    tickers_traded = {t["ticker"] for t in result["trades"]}
    assert tickers_traded == {"BBB"}
    assert len(veto_log) == 1
    assert veto_log[0][1] == "AAA"


# ── veto_fn is called with the signal date, not an arbitrary date ──────────

def test_veto_fn_called_with_signal_day_iso_string():
    master_cal = _make_calendar()
    crossings_by_ticker, prices_wide = _base_args(master_cal, signal_day_idx=1)
    calls = []

    def veto_fn(ticker, iso_date):
        calls.append((ticker, iso_date))
        return False, ""

    simulate(crossings_by_ticker, prices_wide, master_cal, pct=0.10, max_pos=10, veto_fn=veto_fn)
    assert calls == [("AAA", master_cal[1].date().isoformat())]


# ── veto acts only at the entry-commit point (upstream gates must still
#    short-circuit before the veto ever fires) ──────────────────────────────

def test_veto_fn_not_called_when_ticker_not_in_universe():
    master_cal = _make_calendar()
    crossings_by_ticker, prices_wide = _base_args(master_cal)
    month_members = {(ts.year, ts.month): set() for ts in master_cal}  # AAA excluded
    calls = []

    def veto_fn(ticker, iso_date):
        calls.append(ticker)
        return False, ""

    result = simulate(
        crossings_by_ticker, prices_wide, master_cal, pct=0.10, max_pos=10,
        month_members=month_members, veto_fn=veto_fn,
    )
    assert result["trades"] == []
    assert calls == []


def test_veto_fn_not_called_when_at_capacity():
    master_cal = _make_calendar()
    prices_wide = pd.DataFrame(
        {"AAA": [100.0] * len(master_cal), "BBB": [50.0] * len(master_cal)},
        index=master_cal,
    )
    crossings_by_ticker = {
        "AAA": [(master_cal[0], 0.90, 100.0, -0.30)],
        "BBB": [(master_cal[0], 0.80, 50.0, -0.20)],
    }
    calls = []

    def veto_fn(ticker, iso_date):
        calls.append(ticker)
        return False, ""

    # max_pos=1: only the highest-composite signal (AAA) can open; BBB must be
    # skipped for capacity BEFORE the veto is ever consulted.
    result = simulate(
        crossings_by_ticker, prices_wide, master_cal, pct=0.10, max_pos=1, veto_fn=veto_fn
    )
    assert calls == ["AAA"]
    assert {t["ticker"] for t in result["trades"]} == {"AAA"}
    assert any(s["reason"] == "capacity" and s["ticker"] == "BBB" for s in result["skipped"])