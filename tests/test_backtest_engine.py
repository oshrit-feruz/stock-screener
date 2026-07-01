"""Unit tests for product/backtest/engine.py — PIT Top-100 universe rebuild,
idle-cash (Fed Funds) yield accrual, and monthly-membership entry filtering.

Only covers the code introduced/modified in this PR:
  - _load_backtest_data: monthly PIT Top-100 universe build, prefetch,
    VALIDATION_UNIVERSE fallback, fedfunds loading, error handling.
  - _simulate: idle-cash yield accrual and month-membership entry filtering.

Synthetic data only — no network calls. All external dependencies
(PriceData, get_universe, get_universe_top_n, prefetch_pit_market_caps,
load_fedfunds) are mocked at the product.backtest.engine module level.
"""
from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import product.backtest.engine as engine
from config.tickers import VALIDATION_UNIVERSE
from core.data.fundamentals import FundamentalSnapshot

# ── helpers ───────────────────────────────────────────────────────────────────

_PASSING_SNAP = FundamentalSnapshot(
    statement_date=date(2022, 9, 30),
    revenue_growth_yoy=0.10,
    debt_to_equity=1.0,
    roe=0.15,
    net_margin=0.10,
)


def _valid_ohlcv(n: int = 260, start: str = "2018-01-01") -> pd.DataFrame:
    """>=252-row OHLCV so it survives the length filter in _load_backtest_data."""
    idx = pd.bdate_range(start, periods=n)
    closes = pd.Series(np.linspace(100.0, 150.0, n), index=idx)
    return pd.DataFrame({
        "Open": closes, "High": closes * 1.005, "Low": closes * 0.995,
        "Close": closes, "Volume": pd.Series(1_000_000.0, index=idx),
    })


def _spy_df(start: str, end: str) -> pd.DataFrame:
    idx = pd.bdate_range(start, end)
    closes = pd.Series(np.linspace(300.0, 320.0, len(idx)), index=idx)
    return pd.DataFrame({
        "Open": closes, "High": closes, "Low": closes,
        "Close": closes, "Volume": pd.Series(1_000_000.0, index=idx),
    })


def _scored_df(dates, closes, composite, opens=None):
    idx = pd.DatetimeIndex(dates)
    return pd.DataFrame({
        "Open": opens if opens is not None else list(closes),
        "Close": list(closes),
        "composite_score": list(composite),
    }, index=idx)


# ─────────────────────────────────────────────────────────────────────────────
# _load_backtest_data
# ─────────────────────────────────────────────────────────────────────────────

def test_load_backtest_data_builds_monthly_pit_universe(monkeypatch):
    """Universe is rebuilt on the first trading day of every calendar month and
    prefetch is warmed once for the union of get_universe() over those dates."""
    spy = _spy_df("2020-01-01", "2020-02-28")
    mock_prices = MagicMock()

    def get_prices_side_effect(ticker, start, end):
        if ticker == "SPY":
            return spy
        return _valid_ohlcv()

    mock_prices.get_prices.side_effect = get_prices_side_effect
    monkeypatch.setattr(engine, "PriceData", MagicMock(return_value=mock_prices))

    mock_get_universe = MagicMock(return_value=["AAA", "BBB", "CCC"])
    monkeypatch.setattr(engine, "get_universe", mock_get_universe)

    def top_n_side_effect(d, n):
        return {"2020-01-01": ["AAA", "BBB"], "2020-02-03": ["BBB", "CCC"]}[d]

    mock_top_n = MagicMock(side_effect=top_n_side_effect)
    monkeypatch.setattr(engine, "get_universe_top_n", mock_top_n)

    mock_prefetch = MagicMock()
    monkeypatch.setattr(engine, "prefetch_pit_market_caps", mock_prefetch)

    mock_fedfunds = pd.Series([0.05], index=[pd.Timestamp("2020-01-01")])
    monkeypatch.setattr(engine, "load_fedfunds", MagicMock(return_value=mock_fedfunds))

    result = engine._load_backtest_data(
        end_date=date(2020, 2, 28),
        quality_start_year=2020, quality_end_year=2020,
        start_date=date(2020, 1, 1),
    )

    # First trading day of Jan and Feb 2020 drove the rebuild — 2020-01-01 is a
    # Wednesday, 2020-02-03 the first business day of Feb (Feb 1-2 fall on a weekend).
    mock_top_n.assert_any_call("2020-01-01", engine._UNIVERSE_N)
    mock_top_n.assert_any_call("2020-02-03", engine._UNIVERSE_N)
    assert mock_top_n.call_count == 2

    assert result["month_members"] == {
        (2020, 1): {"AAA", "BBB"},
        (2020, 2): {"BBB", "CCC"},
    }

    mock_prefetch.assert_called_once_with(
        ["AAA", "BBB", "CCC"], ["2020-01-01", "2020-02-03"]
    )

    # Universe fetched for scoring = union of all months' PIT membership.
    assert set(result["scored_data"].keys()) == {"AAA", "BBB", "CCC"}
    assert result["fedfunds"] is mock_fedfunds
    assert set(result.keys()) == {
        "scored_data", "spy_ohlcv", "fundamentals", "month_members", "fedfunds",
    }


def test_load_backtest_data_falls_back_to_validation_universe_when_no_calendar(monkeypatch):
    """Empty SPY calendar → no monthly rebuild dates → month_members stays empty
    and the legacy VALIDATION_UNIVERSE is scanned instead."""
    mock_prices = MagicMock()

    def get_prices_side_effect(ticker, start, end):
        if ticker == "SPY":
            return pd.DataFrame()
        return _valid_ohlcv()

    mock_prices.get_prices.side_effect = get_prices_side_effect
    monkeypatch.setattr(engine, "PriceData", MagicMock(return_value=mock_prices))

    mock_get_universe = MagicMock()
    mock_top_n = MagicMock()
    mock_prefetch = MagicMock()
    monkeypatch.setattr(engine, "get_universe", mock_get_universe)
    monkeypatch.setattr(engine, "get_universe_top_n", mock_top_n)
    monkeypatch.setattr(engine, "prefetch_pit_market_caps", mock_prefetch)
    monkeypatch.setattr(engine, "load_fedfunds", MagicMock(return_value=None))

    result = engine._load_backtest_data(
        end_date=date(2020, 2, 28),
        quality_start_year=2020, quality_end_year=2020,
        start_date=date(2020, 1, 1),
    )

    assert mock_get_universe.call_count == 0
    assert mock_top_n.call_count == 0
    assert mock_prefetch.call_count == 0
    assert result["month_members"] == {}
    assert set(result["scored_data"].keys()) == set(VALIDATION_UNIVERSE)


def test_load_backtest_data_swallows_get_universe_exception(monkeypatch):
    """A failing get_universe() must not prevent the per-month Top-N rebuild —
    only the prefetch warm-up (which needs the full membership pool) is skipped."""
    spy = _spy_df("2020-01-01", "2020-01-31")
    mock_prices = MagicMock()

    def get_prices_side_effect(ticker, start, end):
        return spy if ticker == "SPY" else _valid_ohlcv()

    mock_prices.get_prices.side_effect = get_prices_side_effect
    monkeypatch.setattr(engine, "PriceData", MagicMock(return_value=mock_prices))

    monkeypatch.setattr(engine, "get_universe", MagicMock(side_effect=RuntimeError("boom")))
    mock_top_n = MagicMock(return_value=["AAA", "BBB"])
    monkeypatch.setattr(engine, "get_universe_top_n", mock_top_n)
    mock_prefetch = MagicMock()
    monkeypatch.setattr(engine, "prefetch_pit_market_caps", mock_prefetch)
    monkeypatch.setattr(engine, "load_fedfunds", MagicMock(return_value=None))

    result = engine._load_backtest_data(
        end_date=date(2020, 1, 31),
        quality_start_year=2020, quality_end_year=2020,
        start_date=date(2020, 1, 1),
    )

    assert mock_prefetch.call_count == 0
    assert mock_top_n.call_count == 1
    assert result["month_members"] == {(2020, 1): {"AAA", "BBB"}}
    assert set(result["scored_data"].keys()) == {"AAA", "BBB"}


def test_load_backtest_data_per_month_top_n_failure_yields_empty_set(monkeypatch):
    """A get_universe_top_n() failure for one month must not affect other months —
    the failing month gets an empty membership set."""
    spy = _spy_df("2020-01-01", "2020-02-28")
    mock_prices = MagicMock()

    def get_prices_side_effect(ticker, start, end):
        return spy if ticker == "SPY" else _valid_ohlcv()

    mock_prices.get_prices.side_effect = get_prices_side_effect
    monkeypatch.setattr(engine, "PriceData", MagicMock(return_value=mock_prices))
    monkeypatch.setattr(engine, "get_universe", MagicMock(return_value=["AAA", "CCC"]))
    monkeypatch.setattr(engine, "prefetch_pit_market_caps", MagicMock())

    def top_n_side_effect(d, n):
        if d == "2020-01-01":
            raise RuntimeError("rank failure")
        return ["CCC"]

    monkeypatch.setattr(engine, "get_universe_top_n", MagicMock(side_effect=top_n_side_effect))
    monkeypatch.setattr(engine, "load_fedfunds", MagicMock(return_value=None))

    result = engine._load_backtest_data(
        end_date=date(2020, 2, 28),
        quality_start_year=2020, quality_end_year=2020,
        start_date=date(2020, 1, 1),
    )

    assert result["month_members"] == {(2020, 1): set(), (2020, 2): {"CCC"}}
    assert set(result["scored_data"].keys()) == {"CCC"}


def test_load_backtest_data_fedfunds_exception_returns_none(monkeypatch):
    """A load_fedfunds() failure must not crash the loader — fedfunds falls back
    to None (idle cash then earns 0%)."""
    mock_prices = MagicMock()
    mock_prices.get_prices.return_value = pd.DataFrame()
    monkeypatch.setattr(engine, "PriceData", MagicMock(return_value=mock_prices))
    monkeypatch.setattr(engine, "get_universe", MagicMock())
    monkeypatch.setattr(engine, "get_universe_top_n", MagicMock())
    monkeypatch.setattr(engine, "prefetch_pit_market_caps", MagicMock())
    monkeypatch.setattr(engine, "load_fedfunds", MagicMock(side_effect=RuntimeError("no network")))

    result = engine._load_backtest_data(
        end_date=date(2020, 1, 31),
        quality_start_year=2020, quality_end_year=2020,
        start_date=date(2020, 1, 1),
    )

    assert result["fedfunds"] is None


# ─────────────────────────────────────────────────────────────────────────────
# _simulate — idle-cash yield accrual
# ─────────────────────────────────────────────────────────────────────────────

def test_simulate_idle_cash_accrues_at_fedfunds_rate(monkeypatch):
    """Cash sitting idle (no positions opened) grows by (1+r)**(days/365) between
    trading bars, using the Fed Funds rate ffilled onto the trading calendar."""
    monkeypatch.setattr(engine, "_MIN_SIGNALS", 0)

    dates = pd.bdate_range("2021-01-04", periods=3)  # Mon, Tue, Wed — 1-day gaps
    spy = pd.DataFrame({"Close": [100.0, 101.0, 102.0]}, index=dates)
    # composite always below entry_threshold → never buys → cash stays idle
    scored = _scored_df(dates, closes=[10.0, 10.0, 10.0], composite=[0.0, 0.0, 0.0])

    fedfunds = pd.Series([0.05], index=[pd.Timestamp("2021-01-01")])

    preloaded = {
        "scored_data": {"T": scored},
        "spy_ohlcv": spy,
        "fundamentals": MagicMock(),
        "month_members": {},
        "fedfunds": fedfunds,
    }
    params = {
        "start_date": "2021-01-04", "end_date": "2021-01-06",
        "entry_threshold": 0.6, "max_positions": 10, "position_size_pct": 10,
    }
    result = engine._simulate(preloaded, params)

    assert "error" not in result
    assert result["trades"] == []
    expected_final = 100_000.0 * (1.05 ** (1 / 365)) ** 2
    assert result["summary"]["final_portfolio"] == int(round(expected_final))
    assert result["summary"]["final_portfolio"] > 100_000


def test_simulate_idle_cash_flat_without_fedfunds(monkeypatch):
    """fedfunds=None → idle cash earns 0% (unchanged legacy behavior)."""
    monkeypatch.setattr(engine, "_MIN_SIGNALS", 0)

    dates = pd.bdate_range("2021-01-04", periods=3)
    spy = pd.DataFrame({"Close": [100.0, 101.0, 102.0]}, index=dates)
    scored = _scored_df(dates, closes=[10.0, 10.0, 10.0], composite=[0.0, 0.0, 0.0])

    preloaded = {
        "scored_data": {"T": scored},
        "spy_ohlcv": spy,
        "fundamentals": MagicMock(),
        "month_members": {},
        "fedfunds": None,
    }
    params = {
        "start_date": "2021-01-04", "end_date": "2021-01-06",
        "entry_threshold": 0.6, "max_positions": 10, "position_size_pct": 10,
    }
    result = engine._simulate(preloaded, params)

    assert "error" not in result
    assert result["trades"] == []
    assert result["summary"]["final_portfolio"] == 100_000


def test_simulate_idle_cash_no_accrual_on_first_bar(monkeypatch):
    """A single-day backtest has no prior bar to accrue against — cash must be
    unchanged regardless of the fedfunds rate."""
    monkeypatch.setattr(engine, "_MIN_SIGNALS", 0)

    dates = pd.bdate_range("2021-01-04", periods=1)
    spy = pd.DataFrame({"Close": [100.0]}, index=dates)
    scored = _scored_df(dates, closes=[10.0], composite=[0.0])
    fedfunds = pd.Series([0.05], index=[pd.Timestamp("2021-01-01")])

    preloaded = {
        "scored_data": {"T": scored},
        "spy_ohlcv": spy,
        "fundamentals": MagicMock(),
        "month_members": {},
        "fedfunds": fedfunds,
    }
    params = {
        "start_date": "2021-01-04", "end_date": "2021-01-04",
        "entry_threshold": 0.6, "max_positions": 10, "position_size_pct": 10,
    }
    result = engine._simulate(preloaded, params)

    assert result["summary"]["final_portfolio"] == 100_000


# ─────────────────────────────────────────────────────────────────────────────
# _simulate — monthly PIT membership filtering of entry candidates
# ─────────────────────────────────────────────────────────────────────────────

def _membership_setup(month_members):
    dates = pd.bdate_range("2021-03-01", periods=5)  # all within March 2021
    spy = pd.DataFrame({"Close": [100.0, 100.5, 101.0, 101.5, 102.0]}, index=dates)
    # Both tickers fire a BUY-eligible composite on the same day (index 2).
    composite = [0.0, 0.0, 0.9, 0.0, 0.0]
    good = _scored_df(dates, closes=[10.0] * 5, composite=composite)
    bad = _scored_df(dates, closes=[10.0] * 5, composite=composite)

    mock_fundamentals = MagicMock()
    mock_fundamentals.get_snapshot.return_value = _PASSING_SNAP

    preloaded = {
        "scored_data": {"GOOD": good, "BAD": bad},
        "spy_ohlcv": spy,
        "fundamentals": mock_fundamentals,
        "month_members": month_members,
        "fedfunds": None,
    }
    params = {
        "start_date": "2021-03-01", "end_date": "2021-03-05",
        "entry_threshold": 0.6, "max_positions": 10, "position_size_pct": 10,
    }
    return preloaded, params


def test_simulate_only_buys_tickers_in_month_membership(monkeypatch):
    """When a PIT membership map is present, tickers outside that month's Top-N
    set must never be bought — even if their signal otherwise qualifies."""
    monkeypatch.setattr(engine, "_MIN_SIGNALS", 0)
    preloaded, params = _membership_setup({(2021, 3): {"GOOD"}})

    result = engine._simulate(preloaded, params)

    assert result["summary"]["n_signals"] == 1
    tickers_traded = {t["ticker"] for t in result["trades"]}
    assert tickers_traded == {"GOOD"}


def test_simulate_missing_month_key_disables_filtering(monkeypatch):
    """month_members present but with NO entry for the current trading month
    (dict.get returns None) → legacy full-universe scan applies for that month."""
    monkeypatch.setattr(engine, "_MIN_SIGNALS", 0)
    # Membership map only covers April — March (the trading month) is absent.
    preloaded, params = _membership_setup({(2021, 4): {"GOOD"}})

    result = engine._simulate(preloaded, params)

    assert result["summary"]["n_signals"] == 2
    tickers_traded = {t["ticker"] for t in result["trades"]}
    assert tickers_traded == {"GOOD", "BAD"}


def test_simulate_empty_membership_set_blocks_all_candidates(monkeypatch):
    """An explicit empty membership set for the trading month blocks every
    candidate that month (distinct from the 'key missing' no-filter case)."""
    monkeypatch.setattr(engine, "_MIN_SIGNALS", 0)
    preloaded, params = _membership_setup({(2021, 3): set()})

    result = engine._simulate(preloaded, params)

    assert result["summary"]["n_signals"] == 0
    assert result["trades"] == []


def test_simulate_empty_membership_map_means_legacy_full_scan(monkeypatch):
    """An empty month_members dict (e.g. no monthly rebuild dates at all) must
    behave like the pre-PIT-universe code path — no filtering applied."""
    monkeypatch.setattr(engine, "_MIN_SIGNALS", 0)
    preloaded, params = _membership_setup({})

    result = engine._simulate(preloaded, params)

    assert result["summary"]["n_signals"] == 2
    tickers_traded = {t["ticker"] for t in result["trades"]}
    assert tickers_traded == {"GOOD", "BAD"}