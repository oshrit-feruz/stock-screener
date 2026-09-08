"""A scan that scored too little of its universe must not be saved or served.

Every per-ticker failure in run_screener is caught and skipped — correctly, one
bad ticker must not sink the run. But that meant a day where EVERY ticker failed
produced an empty ranking indistinguishable from a quiet market: saved, published,
served as a 200, and the workflow went green. Nothing compared what was scored
against what was asked for.

The guard raises ScreenerDegraded BEFORE _save_disk_cache. That ordering is the
point and is asserted here directly: after a degraded scan there must be no
file on disk, because a file is what gets published.

Same stubbing seam as test_daily_screener_veto.py; no network.
"""
from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

import product.screener.daily_screener as ds
from product.screener.daily_screener import ScreenerDegraded, run_screener
from product.screener.universe_list import UniverseList

_AS_OF = date(2024, 1, 3)


def _scored() -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=3)
    return pd.DataFrame({
        "Open": [100.0, 101.0, 102.0], "Close": [100.0, 101.0, 102.0],
        "high_52w": [150.0] * 3, "drawdown_52w": [-0.3] * 3,
        "dip_score": [0.5] * 3, "momentum_score": [0.5] * 3,
        "volume_score": [0.5] * 3, "composite_score": [0.5] * 3,
    }, index=idx)


def _wire(monkeypatch, tmp_path, universe: list[str], failing: set[str]):
    """Universe of `universe`; every ticker in `failing` returns no price data."""
    monkeypatch.setattr(ds, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        ds, "load_universe_list",
        lambda **_k: UniverseList(tickers=list(universe), as_of=date(2024, 1, 1),
                                  age_days=2, is_late=False),
    )
    monkeypatch.setattr(ds, "compute_recovery_signals", lambda ohlcv: _scored())
    monkeypatch.setattr(ds, "passes_quality_gate", lambda snap: True)
    monkeypatch.setattr(ds, "is_vetoed", lambda *a, **k: (False, None), raising=False)

    class _Prices:
        def get_prices(self, ticker, start, end):
            if ticker in failing:
                return pd.DataFrame()          # "no price data returned"
            idx = pd.bdate_range("2023-01-01", periods=260)
            return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0,
                                 "Close": 1.0, "Volume": 1.0}, index=idx)

    class _Funds:
        def get_snapshot(self, ticker, as_of):
            return object()

    monkeypatch.setattr(ds, "PriceData", _Prices)
    monkeypatch.setattr(ds, "EdgarFundamentals", lambda **_k: _Funds())


def _files(tmp_path) -> list[str]:
    return sorted(p.name for p in tmp_path.glob("*.json"))


# ── the regression ──────────────────────────────────────────────────────────

def test_every_ticker_failing_raises_and_writes_nothing(tmp_path, monkeypatch):
    """The day that used to go green. All five fail: no ranking, no file."""
    universe = ["A", "B", "C", "D", "E"]
    _wire(monkeypatch, tmp_path, universe, failing=set(universe))
    with pytest.raises(ScreenerDegraded) as exc:
        run_screener(as_of_date=_AS_OF, apply_8k_veto=False)
    assert _files(tmp_path) == [], "a degraded scan must never reach disk"
    msg = str(exc.value)
    assert "0/5" in msg
    assert "A" in msg, "the message must name what was not scored"


def test_one_failure_in_five_is_a_normal_day(tmp_path, monkeypatch):
    """A single bad ticker is routine and must not sink the run. 4/5 = 80%,
    exactly the threshold, and the threshold is inclusive."""
    universe = ["A", "B", "C", "D", "E"]
    _wire(monkeypatch, tmp_path, universe, failing={"E"})
    result = run_screener(as_of_date=_AS_OF, apply_8k_veto=False)
    assert len(result.full_ranking) == 4
    assert _files(tmp_path) == [f"{_AS_OF.isoformat()}.json"]


def test_the_threshold_is_a_ceiling(tmp_path, monkeypatch):
    """Pins math.ceil: with 10 tickers 80% is exactly 8, so 8 scored passes and
    7 raises. A floor would let 7 through."""
    universe = [f"T{i}" for i in range(10)]
    assert math.ceil(ds._MIN_SCAN_COVERAGE * 10) == 8

    _wire(monkeypatch, tmp_path, universe, failing={"T8", "T9"})
    assert len(run_screener(as_of_date=_AS_OF, apply_8k_veto=False).full_ranking) == 8

    _wire(monkeypatch, tmp_path, universe, failing={"T7", "T8", "T9"})
    for f in tmp_path.glob("*.json"):
        f.unlink()
    with pytest.raises(ScreenerDegraded):
        run_screener(as_of_date=_AS_OF, apply_8k_veto=False)
    assert _files(tmp_path) == []


def test_the_message_says_where_to_look(tmp_path, monkeypatch):
    """A red run is only useful if the log says what to check."""
    universe = ["A", "B", "C"]
    _wire(monkeypatch, tmp_path, universe, failing=set(universe))
    with pytest.raises(ScreenerDegraded, match="EODHD_API_KEY"):
        run_screener(as_of_date=_AS_OF, apply_8k_veto=False)
