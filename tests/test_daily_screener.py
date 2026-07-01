"""Unit tests for product/screener/daily_screener.py — PIT Top-100 universe
sourcing (data.sp500_universe.get_universe_top_n replacing VALIDATION_UNIVERSE).

Only covers the code introduced/modified in this PR: the universe build step in
run_screener() (lookup, error handling, and the per-ticker scan loop now driven
by that universe). Synthetic data only — no network calls.
"""
from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import product.screener.daily_screener as daily_screener
from core.data.fundamentals import FundamentalSnapshot
from product.screener.daily_screener import run_screener

_PASSING_SNAP = FundamentalSnapshot(
    statement_date=date(2022, 9, 30),
    revenue_growth_yoy=0.10,
    debt_to_equity=1.0,
    roe=0.15,
    net_margin=0.10,
)


def _make_ohlcv(n: int = 300, start: str = "2016-01-01") -> pd.DataFrame:
    """>=252-row OHLCV so compute_recovery_signals produces a non-NaN composite."""
    idx = pd.bdate_range(start, periods=n)
    closes = pd.Series(np.linspace(100.0, 130.0, n), index=idx)
    return pd.DataFrame({
        "Open": closes, "High": closes * 1.005, "Low": closes * 0.995,
        "Close": closes, "Volume": pd.Series(1_000_000.0, index=idx),
    })


@pytest.fixture(autouse=True)
def _isolate_disk_cache(tmp_path, monkeypatch):
    """Route the screener's disk cache to a fresh temp dir so tests never read a
    stale cached result or write into the repo's real cache directory."""
    monkeypatch.setattr(daily_screener, "_CACHE_DIR", tmp_path)


def _mock_prices_fundamentals(tickers):
    mock_prices = MagicMock()
    mock_prices.get_prices.return_value = _make_ohlcv()
    mock_fundamentals = MagicMock()
    mock_fundamentals.get_snapshot.return_value = _PASSING_SNAP
    return mock_prices, mock_fundamentals


# ─────────────────────────────────────────────────────────────────────────────
# Universe sourcing
# ─────────────────────────────────────────────────────────────────────────────

def test_run_screener_uses_pit_top_n_universe(monkeypatch):
    """run_screener must scan exactly the tickers returned by get_universe_top_n,
    called with the run date and the frozen universe size."""
    as_of = date(2024, 6, 3)
    mock_top_n = MagicMock(return_value=["AAPL", "MSFT"])
    monkeypatch.setattr(daily_screener, "get_universe_top_n", mock_top_n)

    mock_prices, mock_fundamentals = _mock_prices_fundamentals(["AAPL", "MSFT"])

    result = run_screener(as_of_date=as_of, prices=mock_prices, fundamentals=mock_fundamentals)

    mock_top_n.assert_called_once_with("2024-06-03", daily_screener._UNIVERSE_N)
    scanned_tickers = {c.args[0] for c in mock_prices.get_prices.call_args_list}
    assert scanned_tickers == {"AAPL", "MSFT"}
    assert {r.ticker for r in result.full_ranking} == {"AAPL", "MSFT"}
    assert result.as_of_date == as_of


def test_universe_size_is_frozen_at_100():
    """Regression guard: the PIT universe size must stay Top-100."""
    assert daily_screener._UNIVERSE_N == 100


def test_run_screener_universe_lookup_failure_returns_empty_result(monkeypatch, caplog):
    """A get_universe_top_n() failure must not crash the run — an empty
    ScreenerResult is returned and no price/fundamentals lookups occur."""
    monkeypatch.setattr(
        daily_screener, "get_universe_top_n",
        MagicMock(side_effect=RuntimeError("network down")),
    )
    mock_prices = MagicMock()
    mock_fundamentals = MagicMock()

    with caplog.at_level("WARNING"):
        result = run_screener(
            as_of_date=date(2024, 6, 3), prices=mock_prices, fundamentals=mock_fundamentals,
        )

    assert result.buy_signals == []
    assert result.full_ranking == []
    assert mock_prices.get_prices.call_count == 0
    assert any("universe lookup failed" in rec.message for rec in caplog.records)


def test_run_screener_empty_universe_is_not_an_error(monkeypatch):
    """get_universe_top_n() returning an empty (but valid) list must also produce
    an empty result, without treating it as a failure."""
    monkeypatch.setattr(daily_screener, "get_universe_top_n", MagicMock(return_value=[]))
    mock_prices = MagicMock()
    mock_fundamentals = MagicMock()

    result = run_screener(
        as_of_date=date(2024, 6, 3), prices=mock_prices, fundamentals=mock_fundamentals,
    )

    assert result.buy_signals == []
    assert result.full_ranking == []
    assert mock_prices.get_prices.call_count == 0


def test_run_screener_skips_only_the_failing_ticker(monkeypatch):
    """An unexpected exception for one universe ticker must not abort the scan
    of the remaining PIT-universe tickers."""
    monkeypatch.setattr(
        daily_screener, "get_universe_top_n",
        MagicMock(return_value=["AAPL", "BAD", "MSFT"]),
    )

    def get_prices_side_effect(ticker, start, end):
        if ticker == "BAD":
            raise RuntimeError("fetch failed")
        return _make_ohlcv()

    mock_prices = MagicMock()
    mock_prices.get_prices.side_effect = get_prices_side_effect
    mock_fundamentals = MagicMock()
    mock_fundamentals.get_snapshot.return_value = _PASSING_SNAP

    result = run_screener(
        as_of_date=date(2024, 6, 3), prices=mock_prices, fundamentals=mock_fundamentals,
    )

    assert {r.ticker for r in result.full_ranking} == {"AAPL", "MSFT"}


def test_run_screener_does_not_scan_tickers_outside_universe(monkeypatch):
    """Tickers not returned by get_universe_top_n must never be looked up."""
    monkeypatch.setattr(daily_screener, "get_universe_top_n", MagicMock(return_value=["NVDA"]))
    mock_prices, mock_fundamentals = _mock_prices_fundamentals(["NVDA"])

    run_screener(as_of_date=date(2024, 6, 3), prices=mock_prices, fundamentals=mock_fundamentals)

    called_tickers = {c.args[0] for c in mock_prices.get_prices.call_args_list}
    assert called_tickers == {"NVDA"}