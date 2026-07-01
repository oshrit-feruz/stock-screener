"""Unit tests for the 8-K veto wired into product/screener/daily_screener.py.

A would-be BUY whose ticker is flagged by data.sec_8k_veto.is_vetoed must be
downgraded to signal "VETO", excluded from buy_signals, surfaced in result.vetoed
with its reason, and must never reach the alert path. Fully mocked — no network.
"""
from datetime import date

import numpy as np
import pandas as pd
import pytest

import product.screener.daily_screener as ds
from product.screener.daily_screener import run_screener


def _buy_eligible_scored() -> pd.DataFrame:
    """A scored frame whose last row is a BUY (composite >= threshold)."""
    idx = pd.bdate_range("2024-01-01", periods=3)
    return pd.DataFrame({
        "Open":            [100.0, 101.0, 102.0],
        "Close":           [100.0, 101.0, 102.0],
        "high_52w":        [150.0, 150.0, 150.0],
        "drawdown_52w":    [-0.32, -0.31, -0.30],
        "dip_score":       [0.8, 0.8, 0.8],
        "momentum_score":  [0.7, 0.7, 0.7],
        "volume_score":    [0.6, 0.6, 0.6],
        "composite_score": [0.75, 0.75, 0.75],
    }, index=idx)


@pytest.fixture(autouse=True)
def _wire_mocks(tmp_path, monkeypatch):
    # Isolate disk cache; stub the whole pipeline so no network/data is needed.
    monkeypatch.setattr(ds, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(ds, "get_universe_top_n", lambda d, n: ["GOOD", "BAD"])
    monkeypatch.setattr(ds, "compute_recovery_signals", lambda ohlcv: _buy_eligible_scored())
    monkeypatch.setattr(ds, "passes_quality_gate", lambda snap: True)

    class _Prices:
        def get_prices(self, ticker, start, end):
            idx = pd.bdate_range("2023-01-01", periods=260)
            return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0,
                                 "Close": 1.0, "Volume": 1.0}, index=idx)

    class _Funds:
        def get_snapshot(self, ticker, as_of):
            return object()

    monkeypatch.setattr(ds, "PriceData", _Prices)
    monkeypatch.setattr(ds, "EdgarFundamentals", lambda **_k: _Funds())


def test_vetoed_ticker_downgraded_and_excluded(monkeypatch, caplog):
    def fake_veto(ticker, as_of_date, **kw):
        if ticker == "BAD":
            return True, "8-K Item 1.03 filed 2024-01-02: Bankruptcy or Receivership"
        return False, ""
    monkeypatch.setattr(ds, "is_vetoed", fake_veto)

    with caplog.at_level("INFO"):
        result = run_screener(as_of_date=date(2024, 1, 3))

    buys = {r.ticker for r in result.buy_signals}
    vetoed = {r.ticker for r in result.vetoed}
    assert buys == {"GOOD"}                       # BAD is blocked
    assert vetoed == {"BAD"}
    bad = next(r for r in result.full_ranking if r.ticker == "BAD")
    assert bad.signal == "VETO"
    assert "1.03" in (bad.veto_reason or "")
    good = next(r for r in result.full_ranking if r.ticker == "GOOD")
    assert good.signal == "BUY" and good.veto_reason is None
    assert any("8-K veto: BAD" in r.message for r in caplog.records)


def test_apply_veto_false_leaves_signals_untouched(monkeypatch):
    # If the veto is disabled, is_vetoed must not even be consulted.
    def boom(*a, **k):
        raise AssertionError("is_vetoed must not be called when apply_8k_veto=False")
    monkeypatch.setattr(ds, "is_vetoed", boom)

    result = run_screener(as_of_date=date(2024, 1, 3), apply_8k_veto=False)
    assert {r.ticker for r in result.buy_signals} == {"GOOD", "BAD"}
    assert result.vetoed == []


def test_veto_lookup_error_does_not_block(monkeypatch):
    # Fact-only / fail-safe: a lookup error must NOT veto (returns not-blocked).
    monkeypatch.setattr(ds, "is_vetoed",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("edgar down")))
    result = run_screener(as_of_date=date(2024, 1, 3))
    assert {r.ticker for r in result.buy_signals} == {"GOOD", "BAD"}
    assert result.vetoed == []
