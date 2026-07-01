"""Unit tests for the 8-K veto integration in
research/run_combined_clean_universe.simulate (veto_fn / veto_log params).

Only the veto wiring introduced by this PR is in scope here — simulate()'s
pre-existing sizing/cash/regime behaviour is exercised only incidentally, as
the minimum needed to observe entries being blocked. Synthetic single-ticker
calendars are used; no network access.
"""
import numpy as np
import pandas as pd
import pytest

from research.run_combined_clean_universe import simulate

_INITIAL_CAP = 100_000.0


def _make_calendar(n=5):
    return pd.bdate_range("2023-01-02", periods=n)


def _base_kwargs(master_cal, ticker="AAA", signal_day_idx=0, comp=0.80, price=100.0):
    prices_wide = pd.DataFrame(
        {ticker: [price + i for i in range(len(master_cal))]}, index=master_cal
    )
    crossings_by_ticker = {ticker: [(master_cal[signal_day_idx], comp, price, -0.30)]}
    month_members = {
        (ts.year, ts.month): {ticker} for ts in master_cal
    }
    rate_on_cal = np.zeros(len(master_cal))
    return dict(
        crossings_by_ticker=crossings_by_ticker,
        prices_wide=prices_wide,
        master_cal=master_cal,
        sizing_mode="flat",
        cash_mode="zero",
        rate_on_cal=rate_on_cal,
        month_members=month_members,
    )


# ── default behaviour: veto_fn=None reproduces the no-veto baseline ─────────

def test_no_veto_fn_opens_position_as_before():
    master_cal = _make_calendar()
    kwargs = _base_kwargs(master_cal)
    result = simulate(**kwargs)
    assert len(result["trades"]) == 1
    assert result["trades"][0]["ticker"] == "AAA"


# ── veto_fn blocks the entry ────────────────────────────────────────────────

def test_veto_fn_blocking_prevents_entry():
    master_cal = _make_calendar()
    kwargs = _base_kwargs(master_cal)
    veto_log = []

    def veto_fn(ticker, iso_date):
        return True, f"distress filing for {ticker} on {iso_date}"

    result = simulate(**kwargs, veto_fn=veto_fn, veto_log=veto_log)
    assert result["trades"] == []
    # No position ever opens, so the portfolio value must stay flat at the
    # initial capital for every day in the calendar.
    assert (result["daily_values"].values == _INITIAL_CAP).all()


def test_veto_fn_blocking_populates_veto_log():
    master_cal = _make_calendar()
    kwargs = _base_kwargs(master_cal, comp=0.80)
    veto_log = []

    def veto_fn(ticker, iso_date):
        return True, "8-K Item 1.03 filed: Bankruptcy or Receivership"

    simulate(**kwargs, veto_fn=veto_fn, veto_log=veto_log)
    assert len(veto_log) == 1
    sig_date, ticker, comp, reason = veto_log[0]
    assert sig_date == master_cal[0].date()
    assert ticker == "AAA"
    assert comp == pytest.approx(0.80)
    assert "1.03" in reason


def test_veto_fn_blocking_without_veto_log_does_not_raise():
    master_cal = _make_calendar()
    kwargs = _base_kwargs(master_cal)

    def veto_fn(ticker, iso_date):
        return True, "blocked"

    # veto_log intentionally omitted (defaults to None) — must not raise.
    result = simulate(**kwargs, veto_fn=veto_fn)
    assert result["trades"] == []


# ── veto_fn allowing the entry (explicit False) behaves like no veto ───────

def test_veto_fn_returning_false_allows_entry():
    master_cal = _make_calendar()
    kwargs = _base_kwargs(master_cal)
    veto_log = []

    def veto_fn(ticker, iso_date):
        return False, ""

    result = simulate(**kwargs, veto_fn=veto_fn, veto_log=veto_log)
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
    month_members = {(ts.year, ts.month): {"AAA", "BBB"} for ts in master_cal}
    rate_on_cal = np.zeros(len(master_cal))
    veto_log = []

    def veto_fn(ticker, iso_date):
        return (ticker == "AAA"), ("distress" if ticker == "AAA" else "")

    result = simulate(
        crossings_by_ticker=crossings_by_ticker,
        prices_wide=prices_wide,
        master_cal=master_cal,
        sizing_mode="flat",
        cash_mode="zero",
        rate_on_cal=rate_on_cal,
        month_members=month_members,
        veto_fn=veto_fn,
        veto_log=veto_log,
    )
    tickers_traded = {t["ticker"] for t in result["trades"]}
    assert tickers_traded == {"BBB"}
    assert len(veto_log) == 1
    assert veto_log[0][1] == "AAA"


# ── veto_fn is called with the signal date, not an arbitrary date ──────────

def test_veto_fn_called_with_signal_day_iso_string():
    master_cal = _make_calendar()
    kwargs = _base_kwargs(master_cal, signal_day_idx=1)
    calls = []

    def veto_fn(ticker, iso_date):
        calls.append((ticker, iso_date))
        return False, ""

    simulate(**kwargs, veto_fn=veto_fn)
    assert calls == [("AAA", master_cal[1].date().isoformat())]


# ── veto acts only at the entry-commit point (position-capacity / gating
#    checks upstream of the veto must still short-circuit before it fires) ──

def test_veto_fn_not_called_when_ticker_not_in_universe():
    master_cal = _make_calendar()
    prices_wide = pd.DataFrame({"AAA": [100.0] * len(master_cal)}, index=master_cal)
    crossings_by_ticker = {"AAA": [(master_cal[0], 0.80, 100.0, -0.30)]}
    # AAA is NOT a member of the point-in-time universe this month.
    month_members = {(ts.year, ts.month): set() for ts in master_cal}
    rate_on_cal = np.zeros(len(master_cal))
    calls = []

    def veto_fn(ticker, iso_date):
        calls.append(ticker)
        return False, ""

    result = simulate(
        crossings_by_ticker=crossings_by_ticker,
        prices_wide=prices_wide,
        master_cal=master_cal,
        sizing_mode="flat",
        cash_mode="zero",
        rate_on_cal=rate_on_cal,
        month_members=month_members,
        veto_fn=veto_fn,
    )
    assert result["trades"] == []
    assert calls == []