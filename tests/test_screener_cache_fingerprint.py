"""The screener disk cache must not serve results from a different universe.

Validating the universe list before the cache lookup is necessary but not
sufficient: at a month boundary the daily run can cache a result under the old
list minutes before the new one lands, and every later read that day would then
serve results for a superseded universe — valid-looking, silently wrong. The
cached payload therefore carries a fingerprint of the universe it was computed
under, and a mismatch forces a recompute.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

import product.screener.daily_screener as ds
from product.screener.daily_screener import (
    ScreenerResult,
    ScreenerRow,
    _load_disk_cache,
    _save_disk_cache,
    _universe_fingerprint,
)
from product.screener.universe_list import UniverseList

_AS_OF = date(2026, 8, 3)


def _ulist(tickers, as_of=_AS_OF) -> UniverseList:
    return UniverseList(tickers=list(tickers), as_of=as_of, age_days=0, is_late=False)


def _result() -> ScreenerResult:
    row = ScreenerRow(
        ticker="AAPL", current_price=1.0, high_52w=2.0, drawdown_pct=0.5,
        dip_score=0.8, momentum_score=0.7, volume_score=0.6,
        composite_score=0.75, gate=True, signal="BUY",
    )
    return ScreenerResult(as_of_date=_AS_OF, buy_signals=[row], full_ranking=[row])


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_CACHE_DIR", tmp_path)


# ── fingerprint ───────────────────────────────────────────────────────────────

def test_fingerprint_is_stable_and_order_independent():
    a = _universe_fingerprint(_ulist(["AAPL", "MSFT"]))
    b = _universe_fingerprint(_ulist(["MSFT", "AAPL"]))
    assert a == b


def test_fingerprint_changes_with_membership():
    assert _universe_fingerprint(_ulist(["AAPL", "MSFT"])) != _universe_fingerprint(
        _ulist(["AAPL", "NVDA"])
    )


def test_fingerprint_changes_with_as_of():
    """Same tickers, different month — still a different universe."""
    assert _universe_fingerprint(_ulist(["AAPL"], as_of=date(2026, 8, 3))) != (
        _universe_fingerprint(_ulist(["AAPL"], as_of=date(2026, 9, 1)))
    )


# ── cache round-trip and invalidation ─────────────────────────────────────────

def test_cache_hits_for_the_same_universe():
    fp = _universe_fingerprint(_ulist(["AAPL", "MSFT"]))
    _save_disk_cache(_result(), fp)
    assert _load_disk_cache(_AS_OF, fp) is not None


def test_cache_misses_when_the_universe_changed():
    """The month-boundary regression: same date, new list -> must recompute."""
    _save_disk_cache(_result(), _universe_fingerprint(_ulist(["AAPL", "MSFT"])))
    new_fp = _universe_fingerprint(_ulist(["AAPL", "NVDA"]))
    assert _load_disk_cache(_AS_OF, new_fp) is None


def test_legacy_cache_without_fingerprint_is_discarded(tmp_path):
    """Pre-existing caches written before this field existed carry no
    provenance, so they cannot be trusted and must be recomputed."""
    (tmp_path / f"{_AS_OF.isoformat()}.json").write_text(json.dumps({
        "as_of_date": _AS_OF.isoformat(),
        "buy_signals": [],
        "full_ranking": [],
    }))
    assert _load_disk_cache(_AS_OF, _universe_fingerprint(_ulist(["AAPL"]))) is None


def test_absent_cache_returns_none():
    assert _load_disk_cache(_AS_OF, _universe_fingerprint(_ulist(["AAPL"]))) is None
