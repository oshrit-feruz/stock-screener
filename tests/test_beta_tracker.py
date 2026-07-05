"""Unit tests for product/beta/beta_tracker.py — observation-only beta tracking.

Network-free: position files and market data (PriceData, load_fedfunds) are
stubbed, so the open/closed return + SPY + money-market computations are exact
and deterministic.
"""
from datetime import date

import numpy as np
import pandas as pd
import pytest

import product.beta.beta_tracker as bt


class _FakePrices:
    """Deterministic prices: each ticker has a constant close; SPY ramps +10%."""
    def get_prices(self, ticker, start, end):
        idx = pd.bdate_range(start, end)
        if len(idx) == 0:
            idx = pd.DatetimeIndex([pd.Timestamp(end)])
        if ticker == "SPY":
            close = pd.Series(np.linspace(400.0, 440.0, len(idx)), index=idx)  # +10%
        else:
            close = pd.Series(120.0, index=idx)   # constant → entry 100 ⇒ +20%
        return pd.DataFrame({"Open": close, "High": close, "Low": close,
                             "Close": close, "Volume": 1.0}, index=idx)


@pytest.fixture(autouse=True)
def _stub_market(monkeypatch):
    # Constant 5% Fed Funds so the money-market accrual is deterministic.
    monkeypatch.setattr(bt, "load_fedfunds",
                        lambda: pd.Series([0.05], index=[pd.Timestamp("2000-01-01")]))


def _set_positions(monkeypatch, open_rows, closed_rows):
    monkeypatch.setattr(bt, "_load",
                        lambda path: open_rows if "open" in path.name else closed_rows)


# ── no-positions state ──────────────────────────────────────────────────────

def test_no_positions_state(monkeypatch):
    _set_positions(monkeypatch, [], [])
    data = bt.build_beta_data(as_of_date=date(2026, 7, 6), prices=_FakePrices())
    assert data["summary"] == {"total_opened": 0, "open": 0, "closed": 0, "closed_aggregate": None}
    assert data["beta_start"] is None
    md = bt.render_markdown(data)
    assert "No positions opened yet" in md
    assert md.startswith("# Beta tracking")


# ── open position: current + SPY + money-market ─────────────────────────────

def test_open_position_computes_all_comparisons(monkeypatch):
    _set_positions(monkeypatch,
                   [{"ticker": "GAIN", "entry_date": "2024-01-02", "entry_price": 100.0}], [])
    data = bt.build_beta_data(as_of_date=date(2024, 6, 3), prices=_FakePrices())
    r = data["open_positions"][0]
    assert r["status"] == "open"
    assert r["current_price"] == 120.0
    assert r["return_pct"] == 20.0                 # 120/100 - 1
    assert r["spy_return_pct"] == 10.0             # SPY ramp +10%
    assert r["mm_return_pct"] is not None and 0 < r["mm_return_pct"] < 5  # ~2% over ~5mo @5%
    assert r["vs_spy_pct"] == round(20.0 - 10.0, 2)
    assert r["vs_mm_pct"] == round(20.0 - r["mm_return_pct"], 2)
    assert r["days_held"] > 0 and r["days_remaining"] == bt._HOLD_TARGET - r["days_held"]
    # summary reflects one open, zero closed
    assert data["summary"]["open"] == 1 and data["summary"]["closed"] == 0


# ── closed position: realized + comparisons + aggregate ─────────────────────

def test_closed_position_and_aggregate(monkeypatch):
    _set_positions(monkeypatch, [],
                   [{"ticker": "DONE", "entry_date": "2024-01-02", "entry_price": 100.0,
                     "exit_date": "2024-06-03", "exit_price": 130.0,
                     "realized_return": 0.30, "days_held": 108}])
    data = bt.build_beta_data(as_of_date=date(2026, 7, 6), prices=_FakePrices())
    r = data["closed_positions"][0]
    assert r["status"] == "closed"
    assert r["return_pct"] == 30.0
    assert r["exit_price"] == 130.0 and r["days_held"] == 108
    assert r["spy_return_pct"] == 10.0
    agg = data["summary"]["closed_aggregate"]
    assert agg["count"] == 1 and agg["strategy_return_pct"] == 30.0 and agg["spy_return_pct"] == 10.0
    assert data["summary"]["total_opened"] == 1


# ── same-day guard (no market data needed) ──────────────────────────────────

def test_same_day_close_is_zero(monkeypatch):
    _set_positions(monkeypatch, [],
                   [{"ticker": "ZED", "entry_date": "2026-06-12", "entry_price": 184.1,
                     "exit_date": "2026-06-12", "exit_price": 184.1,
                     "realized_return": 0.0, "days_held": 0}])
    data = bt.build_beta_data(as_of_date=date(2026, 7, 6), prices=_FakePrices())
    r = data["closed_positions"][0]
    assert r["spy_return_pct"] == 0.0 and r["mm_return_pct"] == 0.0 and r["return_pct"] == 0.0


# ── report file + JSON-serializable ─────────────────────────────────────────

def test_write_report_and_json(monkeypatch, tmp_path):
    import json
    _set_positions(monkeypatch,
                   [{"ticker": "GAIN", "entry_date": "2024-01-02", "entry_price": 100.0}], [])
    monkeypatch.setattr(bt, "_REPORT_DIR", tmp_path)
    monkeypatch.setattr(bt, "_REPORT_FILE", tmp_path / "beta_log.md")
    path = bt.write_report(as_of_date=date(2024, 6, 3), prices=_FakePrices())
    text = path.read_text()
    assert "## Open positions" in text and "GAIN" in text and "| Ticker |" in text
    json.dumps(bt.build_beta_data(as_of_date=date(2024, 6, 3), prices=_FakePrices()))
