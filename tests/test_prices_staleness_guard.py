"""get_prices must never serve stale closes as current, silently.

The 7.5-week outage surfaced through this exact hole: fetch fails or returns
empty, get_prices falls back to whatever the cache holds — with no age check —
and the caller displays a weeks-old close as the price "now". Pinned here:

  * max_stale_tdays set (current-price contexts): data older than the bound is
    REFUSED — empty frame, WARNING naming ticker and age — mirroring the
    screener's honest-503 pattern;
  * fresh data within the bound is served unchanged;
  * max_stale_tdays omitted (historical windows, backtests, the simulator):
    behavior unchanged — old data IS the request there;
  * the silent fallback branches now log the age they serve even without the
    bound.
"""
from __future__ import annotations

import logging
import pickle

import pandas as pd
import pytest

from core.data.prices import PriceData


def _frame(*dates):
    idx = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    return pd.DataFrame(
        {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": [100.0] * len(dates),
         "Volume": 1_000}, index=idx,
    )


@pytest.fixture
def pd_cache(tmp_path, monkeypatch):
    """PriceData over an isolated cache dir, with the network fetch disabled —
    every test below must be answerable from cache alone."""
    import core.data.prices as pr
    monkeypatch.setattr(pr, "fetch_eod", lambda *a, **k: pd.DataFrame())
    return PriceData(cache_dir=tmp_path)


def _seed(pdata, ticker, start, frame):
    with open(pdata._cache_path(ticker, start), "wb") as f:
        pickle.dump(frame, f)


# ── fresh-serve ───────────────────────────────────────────────────────────────

def test_fresh_data_within_bound_is_served(pd_cache):
    _seed(pd_cache, "AAPL", "2026-08-14", _frame("2026-08-20", "2026-08-21"))
    df = pd_cache.get_prices("AAPL", "2026-08-14", "2026-08-24", max_stale_tdays=7)
    assert not df.empty
    assert float(df["Close"].iloc[-1]) == 100.0


def test_exactly_at_bound_is_served(pd_cache):
    # last bar 2026-08-13, end 2026-08-24 -> 7 trading days behind == limit, not over
    _seed(pd_cache, "AAPL", "2026-08-01", _frame("2026-08-12", "2026-08-13"))
    df = pd_cache.get_prices("AAPL", "2026-08-01", "2026-08-24", max_stale_tdays=7)
    assert not df.empty


# ── stale-refusal ─────────────────────────────────────────────────────────────

def test_stale_beyond_bound_is_refused_with_warning(pd_cache, caplog):
    """The outage shape: cache ends 2026-06-30, 'now' is 2026-08-24 — 39
    trading days stale. Must come back empty, not as a current price."""
    _seed(pd_cache, "MSFT", "2026-08-14", _frame("2026-06-29", "2026-06-30"))
    with caplog.at_level(logging.WARNING, logger="core.data.prices"):
        df = pd_cache.get_prices("MSFT", "2026-08-14", "2026-08-24", max_stale_tdays=7)
    assert df.empty, "stale close must not be served as current"
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "MSFT" in msg and "trading days behind" in msg


def test_refusal_names_the_actual_age(pd_cache, caplog):
    _seed(pd_cache, "MSFT", "2026-08-14", _frame("2026-06-30"))
    with caplog.at_level(logging.WARNING, logger="core.data.prices"):
        pd_cache.get_prices("MSFT", "2026-08-14", "2026-08-24", max_stale_tdays=7)
    assert any("39 trading days" in r.getMessage() for r in caplog.records)


def test_empty_result_stays_empty_not_an_error(pd_cache):
    df = pd_cache.get_prices("NOPE", "2026-08-14", "2026-08-24", max_stale_tdays=7)
    assert df.empty


# ── historical windows unchanged ──────────────────────────────────────────────

def test_historical_window_without_bound_serves_old_data(pd_cache):
    """The simulator/backtest contract: no bound passed -> a years-old window
    is served exactly as before. Old data IS the request there."""
    _seed(pd_cache, "AAPL", "2020-01-01", _frame("2020-06-01", "2020-06-02"))
    df = pd_cache.get_prices("AAPL", "2020-01-01", "2026-08-24")
    assert not df.empty


def test_fallback_serving_stale_cache_logs_the_age(pd_cache, caplog):
    """(b): even without the bound, the silent fallback branch must now say
    what it served. Force the fallback: cache too short for `end`, fetch empty."""
    _seed(pd_cache, "AAPL", "2026-08-01", _frame("2026-08-04", "2026-08-05"))
    with caplog.at_level(logging.WARNING, logger="core.data.prices"):
        df = pd_cache.get_prices("AAPL", "2026-08-01", "2026-08-24")
    assert not df.empty                      # behavior unchanged: still served
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "serving cached data ending" in msg and "2026-08-05" in msg
