"""Tests for the satellite-sleeve overlay policy and its wiring.

Covers product/satellite_policy.py (regime, actionability, target exit date),
the exit tracker sharing the same hold constant, the screener publishing the
overlay fields (incl. disk-cache round-trip and old-format cache files), and a
mirror of the shift-app reader's contract so the payload stays additive.
Fully mocked — no network.
"""
from __future__ import annotations

import json
import re
from datetime import date

import numpy as np
import pandas as pd
import pytest

import product.satellite_policy as sp
from product.exit import exit_tracker
from product.screener import daily_screener as ds

# ── helpers ──────────────────────────────────────────────────────────────────

def _spy_series(n: int, dd: float) -> pd.Series:
    """`n` bars ending in a close `dd` below the window high (last bar is low)."""
    idx = pd.bdate_range("2023-01-02", periods=n)
    vals = np.full(n, 100.0)
    vals[-1] = 100.0 * (1 - dd)
    return pd.Series(vals, index=idx)


class _Prices:
    """PriceData-like: SPY at a controllable drawdown, tickers flat."""

    def __init__(self, spy_dd: float | None = 0.0):
        self.spy_dd = spy_dd
        self.calls: list[tuple] = []

    def get_prices(self, ticker, start, end):
        self.calls.append((ticker, start, end))
        if ticker == "SPY":
            if self.spy_dd is None:
                return pd.DataFrame()
            s = _spy_series(300, self.spy_dd)
            return pd.DataFrame({"Open": s, "High": s, "Low": s, "Close": s, "Volume": 1.0})
        idx = pd.bdate_range("2023-01-01", periods=260)
        return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0},
                            index=idx)


def _buy_scored() -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=3)
    return pd.DataFrame({
        "Open": [100.0, 101.0, 102.0], "Close": [100.0, 101.0, 102.0],
        "high_52w": [150.0] * 3, "drawdown_52w": [-0.32, -0.31, -0.30],
        "dip_score": [0.8] * 3, "momentum_score": [0.7] * 3, "volume_score": [0.6] * 3,
        "composite_score": [0.75] * 3,
    }, index=idx)


# ── policy: regime ───────────────────────────────────────────────────────────

def test_regime_dislocation_when_dd_at_or_above_gate():
    r = sp.regime_from_series(_spy_series(300, 0.12), date(2024, 3, 1))
    assert r is not None
    assert r["in_dislocation"] is True
    assert r["spy_dd_from_high"] == pytest.approx(0.12, abs=1e-4)
    assert r["gate_dd"] == sp.GATE_DD


def test_regime_calm_below_gate():
    r = sp.regime_from_series(_spy_series(300, 0.04), date(2024, 3, 1))
    assert r["in_dislocation"] is False


def test_regime_gate_boundary_is_inclusive():
    assert sp.regime_from_series(_spy_series(300, sp.GATE_DD), date(2024, 3, 1))["in_dislocation"]


def test_regime_uses_only_bars_on_or_before_as_of():
    """A crash AFTER as_of must not leak into the regime."""
    s = _spy_series(300, 0.0)
    s.iloc[-1] = 50.0                       # a -50% bar on the last date
    as_of = s.index[-2].date()              # ask for the day before it
    r = sp.regime_from_series(s, as_of)
    assert r["in_dislocation"] is False
    assert r["as_of"] == as_of.isoformat()


def test_regime_none_when_no_data():
    assert sp.regime_from_series(pd.Series(dtype=float), date(2024, 3, 1)) is None
    assert sp.regime_from_series(_spy_series(10, 0.0), date(2000, 1, 1)) is None


def test_market_regime_fetches_spy_with_warmup_and_nulls_on_failure():
    p = _Prices(spy_dd=0.2)
    r = sp.market_regime(p, date(2024, 3, 1), "2016-01-01")
    assert r["in_dislocation"] is True
    assert p.calls[0] == ("SPY", "2016-01-01", "2024-03-01")
    assert sp.market_regime(_Prices(spy_dd=None), date(2024, 3, 1), "2016-01-01") is None

    class _Boom:
        def get_prices(self, *a):
            raise RuntimeError("network")
    assert sp.market_regime(_Boom(), date(2024, 3, 1), "2016-01-01") is None


# ── policy: actionability + exit date ────────────────────────────────────────

def test_is_active_matrix():
    hot, calm = {"in_dislocation": True}, {"in_dislocation": False}
    assert sp.is_active("BUY", hot) is True
    assert sp.is_active("BUY", calm) is False
    assert sp.is_active("BUY", None) is None          # regime unknown → unknown
    for sig in ("WATCH", "SKIP", "VETO", "INSUFFICIENT_DATA", None):
        assert sp.is_active(sig, hot) is False


def test_target_exit_date_matches_exit_tracker_counting():
    """The published target must be the day the tracker fires: same weekday
    arithmetic, HOLD_TRADING_DAYS apart."""
    entry = date(2024, 3, 1)
    target = sp.target_exit_date(entry)
    assert target > entry
    counted = exit_tracker.ExitTracker._count_trading_days(None, entry, target)
    assert counted == sp.HOLD_TRADING_DAYS == 504


def test_exit_tracker_shares_the_policy_hold():
    assert exit_tracker._EXIT_HOLD_DAYS == sp.HOLD_TRADING_DAYS
    assert exit_tracker._REMINDER_WINDOW_START == sp.HOLD_TRADING_DAYS - exit_tracker._REMINDER_DAYS


def test_policy_dict_is_the_frozen_config():
    p = sp.policy_dict()
    assert p == {
        "hold_trading_days": 504, "exit_rule": "hold_2y_no_tp_no_sl", "gate_dd": 0.10,
        "gate_lookback_days": 252, "sleeve_pct_of_budget": 10, "max_sleeves": 10,
    }


# ── screener wiring ──────────────────────────────────────────────────────────

@pytest.fixture
def screener(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(ds, "get_universe_top_n", lambda d, n: ["AAA", "BBB"])
    monkeypatch.setattr(ds, "compute_recovery_signals", lambda ohlcv: _buy_scored())
    monkeypatch.setattr(ds, "passes_quality_gate", lambda snap: True)
    monkeypatch.setattr(ds, "is_vetoed", lambda t, d, **k: (False, ""))

    class _Funds:
        def get_snapshot(self, ticker, as_of):
            return object()
    monkeypatch.setattr(ds, "EdgarFundamentals", lambda **_k: _Funds())
    return tmp_path


def _run(spy_dd, as_of=date(2024, 3, 1)):
    return ds.run_screener(as_of_date=as_of, prices=_Prices(spy_dd=spy_dd))


def test_screener_publishes_regime_policy_active_and_target(screener):
    res = _run(spy_dd=0.15)
    assert res.market_regime["in_dislocation"] is True
    assert res.satellite_policy == sp.policy_dict()
    assert len(res.buy_signals) == 2
    for row in res.buy_signals:
        assert row.signal == "BUY"
        assert row.active is True
        assert row.target_exit_date == sp.target_exit_date(date(2024, 3, 1)).isoformat()


def test_screener_calm_market_keeps_signal_but_inactive(screener):
    """Regime gating never suppresses a BUY — it only sets active=False."""
    res = _run(spy_dd=0.03)
    assert res.market_regime["in_dislocation"] is False
    assert [r.signal for r in res.buy_signals] == ["BUY", "BUY"]
    assert all(r.active is False for r in res.buy_signals)


def test_screener_unknown_regime_is_null_not_invented(screener):
    res = _run(spy_dd=None)
    assert res.market_regime is None
    assert [r.signal for r in res.buy_signals] == ["BUY", "BUY"]
    assert all(r.active is None for r in res.buy_signals)


def test_disk_cache_round_trips_overlay_fields(screener):
    first = _run(spy_dd=0.15)
    cached = ds.run_screener(as_of_date=date(2024, 3, 1), prices=_Prices(spy_dd=0.0))
    # second call hits the disk cache → identical overlay context, not recomputed
    assert cached.market_regime == first.market_regime
    assert cached.satellite_policy == first.satellite_policy
    assert [r.active for r in cached.buy_signals] == [True, True]
    assert [r.target_exit_date for r in cached.buy_signals] == \
           [r.target_exit_date for r in first.buy_signals]


def test_old_format_cache_file_still_loads(screener, tmp_path):
    """A cache written before the overlay existed has no regime/policy and no
    per-row active/target — it must load, with the gaps honestly None."""
    old = {
        "as_of_date": "2024-03-01",
        "buy_signals": [{"ticker": "OLD", "current_price": 1.0, "high_52w": 2.0,
                         "drawdown_pct": 0.5, "dip_score": 1.0, "momentum_score": 1.0,
                         "volume_score": 1.0, "composite_score": 0.9, "gate": True,
                         "signal": "BUY", "veto_reason": None}],
        "full_ranking": [],
    }
    (tmp_path / "2024-03-01.json").write_text(json.dumps(old))
    res = ds.run_screener(as_of_date=date(2024, 3, 1), prices=_Prices())
    assert res.market_regime is None
    assert res.satellite_policy == sp.policy_dict()
    assert res.buy_signals[0].active is None
    assert res.buy_signals[0].target_exit_date is None


# ── API payload: additive, and readable by the shift-app mirror parser ───────

def test_api_payload_is_additive_and_matches_shift_app_reader(screener):
    """Mirrors app/src/data/recoveryDetector.ts rules: an object with
    `buy_signals`/`full_ranking` arrays and a YYYY-MM-DD `computed_on`/`as_of`;
    rows keyed by the names its mapSignal picks. Extra keys must be ignorable."""
    main = pytest.importorskip("product.api.main")
    res = _run(spy_dd=0.15)
    body = main._screener_payload(res)

    # Pre-existing keys keep their exact meaning.
    assert body["as_of"] == "2024-03-01"
    assert isinstance(body["buy_signals"], list) and isinstance(body["full_ranking"], list)
    # readMirror's date gate
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", body["computed_on"])
    # New, additive context
    assert body["schema_version"] == sp.SCHEMA_VERSION
    assert body["market_regime"]["in_dislocation"] is True
    assert body["satellite_policy"] == sp.policy_dict()

    row = body["buy_signals"][0]
    # keys mapSignal picks today
    for k in ("ticker", "price", "high_52w", "drawdown_pct", "composite_score", "signal"):
        assert k in row
    assert row["signal"] == "BUY"
    # keys it can start picking
    assert row["active"] is True
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["target_exit_date"])
    # JSON-serialisable end to end (bools/None/floats only)
    json.dumps(body)
