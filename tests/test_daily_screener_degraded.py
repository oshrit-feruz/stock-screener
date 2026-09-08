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

import json
import math
from datetime import date

import pandas as pd
import pytest

import product.screener.daily_screener as ds
from product.screener.daily_screener import (
    ScreenerDegraded,
    _load_disk_cache,
    _universe_fingerprint,
    run_screener,
)
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


# ── the cache is a second reader, and must apply the same bar ──────────────
#
# run_screener returns a disk-cached result BEFORE its own guard runs, and
# /api/screener's published-result walk never goes through run_screener at
# all. So the guard has to live in _load_disk_cache, the one chokepoint every
# reader shares — a guard on the producer alone leaves every consumer trusting
# whatever is on disk.


def _write_cache(tmp_path, as_of: date, fp: str, n_rows: int) -> None:
    rows = [{"ticker": f"T{i}", "current_price": 1.0, "high_52w": 2.0,
             "drawdown_pct": 0.5, "dip_score": 0.5, "momentum_score": 0.5,
             "volume_score": 0.5, "composite_score": 0.5, "gate": True,
             "signal": "HOLD"} for i in range(n_rows)]
    (tmp_path / f"{as_of.isoformat()}.json").write_text(json.dumps({
        "as_of_date": as_of.isoformat(), "universe_fingerprint": fp,
        "buy_signals": [], "full_ranking": rows,
    }))


def test_an_under_covered_cache_is_not_returned_by_run_screener(tmp_path, monkeypatch):
    """CodeRabbit's regression: a cache file written by the OLD code with too
    few rows, matching fingerprint, for today. run_screener must not hand it
    back — it must fall through to a fresh scan, which here fails coverage too,
    so ScreenerDegraded proves the scan actually ran."""
    universe = ["A", "B", "C", "D", "E"]
    _wire(monkeypatch, tmp_path, universe, failing=set(universe))
    ulist = UniverseList(tickers=universe, as_of=date(2024, 1, 1), age_days=2, is_late=False)
    _write_cache(tmp_path, _AS_OF, _universe_fingerprint(ulist), n_rows=1)

    with pytest.raises(ScreenerDegraded):
        run_screener(as_of_date=_AS_OF, apply_8k_veto=False)


def test_a_full_cache_is_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_CACHE_DIR", tmp_path)
    _write_cache(tmp_path, _AS_OF, "fp", n_rows=100)
    result = _load_disk_cache(_AS_OF, "fp", universe_size=100)
    assert result is not None, "a fully-covered cache must be returned"
    assert len(result.full_ranking) == 100


def test_an_under_covered_cache_reads_as_absent(tmp_path, monkeypatch):
    """Skipped exactly as a fingerprint mismatch is — so the serving path walks
    on to the next date rather than answering 200 with half a universe."""
    monkeypatch.setattr(ds, "_CACHE_DIR", tmp_path)
    _write_cache(tmp_path, _AS_OF, "fp", n_rows=50)
    assert _load_disk_cache(_AS_OF, "fp", universe_size=100) is None


def test_the_cache_bar_is_the_same_bar(tmp_path, monkeypatch):
    """Not a second, softer rule: 80 of 100 passes, 79 does not — the same
    ceiling the producer guard applies before saving."""
    monkeypatch.setattr(ds, "_CACHE_DIR", tmp_path)
    _write_cache(tmp_path, _AS_OF, "fp", n_rows=80)
    assert _load_disk_cache(_AS_OF, "fp", universe_size=100) is not None
    _write_cache(tmp_path, _AS_OF, "fp", n_rows=79)
    assert _load_disk_cache(_AS_OF, "fp", universe_size=100) is None


def test_the_denominator_is_the_current_universe_not_the_file(tmp_path, monkeypatch):
    """An empty file written under the old code is judged against TODAY's
    universe. That is what makes it fail: nothing in the file can vouch for
    itself."""
    monkeypatch.setattr(ds, "_CACHE_DIR", tmp_path)
    _write_cache(tmp_path, _AS_OF, "fp", n_rows=0)
    assert _load_disk_cache(_AS_OF, "fp", universe_size=100) is None
