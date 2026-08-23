"""Loud-failure contract for the monthly universe list.

These tests exist because the opposite behaviour shipped: the daily screener
caught a universe-lookup failure at WARNING, set `universe = []`, and reported
"0 signals" as a successful run every day for ~7.5 weeks. Every test here
asserts that a bad universe list RAISES rather than degrading quietly.

Stdlib-only (no pandas) so this runs anywhere.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from product.screener.universe_list import (
    MAX_AGE_DAYS,
    UniverseList,
    UniverseListError,
    load_universe_list,
)

_TODAY = date(2026, 8, 23)


def _write(tmp_path: Path, payload) -> Path:
    p = tmp_path / "current.json"
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return p


def _valid(as_of: str = "2026-08-03", tickers=None) -> dict:
    return {"as_of": as_of, "n": 3, "tickers": tickers if tickers is not None else ["AAPL", "MSFT", "NVDA"]}


# ── happy path ────────────────────────────────────────────────────────────────

def test_loads_current_month_list(tmp_path):
    got = load_universe_list(_write(tmp_path, _valid()), today=_TODAY)
    assert isinstance(got, UniverseList)
    assert got.tickers == ["AAPL", "MSFT", "NVDA"]
    assert got.as_of == date(2026, 8, 3)
    assert got.is_late is False


def test_one_missed_rebuild_is_usable_but_flagged_late(tmp_path):
    """A single late monthly rebuild degrades to a WARNING, not an outage — but
    it is never invisible: is_late is True so the caller must log it."""
    got = load_universe_list(_write(tmp_path, _valid(as_of="2026-07-01")), today=_TODAY)
    assert got.tickers  # still usable
    assert got.is_late is True
    assert got.age_days == 53
    assert got.age_days <= MAX_AGE_DAYS


def test_two_missed_rebuilds_raises(tmp_path):
    """62-day cutoff is sized to tolerate one missed rebuild and refuse two."""
    with pytest.raises(UniverseListError, match="stale"):
        load_universe_list(_write(tmp_path, _valid(as_of="2026-06-01")), today=_TODAY)


# ── loud failures: every one of these must RAISE ──────────────────────────────

def test_missing_file_raises(tmp_path):
    with pytest.raises(UniverseListError, match="not found"):
        load_universe_list(tmp_path / "nope.json", today=_TODAY)


def test_empty_ticker_list_raises(tmp_path):
    """The exact production symptom: a list that parses fine but scans nothing."""
    with pytest.raises(UniverseListError):
        load_universe_list(_write(tmp_path, _valid(tickers=[])), today=_TODAY)


def test_stale_list_raises(tmp_path):
    old = (_TODAY - timedelta(days=MAX_AGE_DAYS + 1)).isoformat()
    with pytest.raises(UniverseListError, match="stale"):
        load_universe_list(_write(tmp_path, _valid(as_of=old)), today=_TODAY)


def test_malformed_json_raises(tmp_path):
    with pytest.raises(UniverseListError, match="not valid JSON"):
        load_universe_list(_write(tmp_path, "{not json"), today=_TODAY)


def test_non_object_payload_raises(tmp_path):
    with pytest.raises(UniverseListError, match="JSON object"):
        load_universe_list(_write(tmp_path, ["AAPL"]), today=_TODAY)


def test_missing_as_of_raises(tmp_path):
    with pytest.raises(UniverseListError, match="as_of"):
        load_universe_list(_write(tmp_path, {"tickers": ["AAPL"]}), today=_TODAY)


def test_unparsable_as_of_raises(tmp_path):
    with pytest.raises(UniverseListError, match="unparsable"):
        load_universe_list(_write(tmp_path, _valid(as_of="not-a-date")), today=_TODAY)


def test_tickers_wrong_type_raises(tmp_path):
    with pytest.raises(UniverseListError, match="tickers"):
        load_universe_list(_write(tmp_path, {"as_of": "2026-08-03", "tickers": "AAPL"}), today=_TODAY)


def test_future_dated_list_raises(tmp_path):
    future = (_TODAY + timedelta(days=2)).isoformat()
    with pytest.raises(UniverseListError, match="future"):
        load_universe_list(_write(tmp_path, _valid(as_of=future)), today=_TODAY)


def test_never_returns_empty_on_any_accepted_input(tmp_path):
    """Belt-and-braces: whatever comes back from a successful load is non-empty."""
    got = load_universe_list(_write(tmp_path, _valid()), today=_TODAY)
    assert len(got.tickers) > 0
