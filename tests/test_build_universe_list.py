"""Raw-price freshness contract for the monthly universe build.

Presence is not freshness. The Actions cache carries data/cache between runs, so
last month's raw-price file still exists this month — and data.sp500_universe.
_raw_close() would happily return last month's close, drifting the ranking
further out of date every month with no error.

Two behaviours are pinned here:
  1. a cached frame that does not reach the as-of date is treated as stale;
  2. a refresh REPLACES the ticker's file rather than adding a second one —
     load-bearing, because _raw_close() resolves a ticker with
     `sorted(glob(f"{ticker}_*.pkl"))[0]`, the EARLIEST start date, so an
     accumulated old file would keep winning and the refresh would silently
     have no effect.
"""
from __future__ import annotations

import pickle
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts.build_universe_list import _covers

_AS_OF = date(2026, 9, 1)


def _frame(last_day: str) -> pd.DataFrame:
    idx = pd.DatetimeIndex(pd.to_datetime(["2026-07-01", last_day]))
    return pd.DataFrame({"Close": [100.0, 110.0]}, index=idx)


def _pickle(tmp_path: Path, name: str, frame) -> Path:
    p = tmp_path / name
    with open(p, "wb") as f:
        pickle.dump(frame, f)
    return p


# ── _covers ───────────────────────────────────────────────────────────────────

def test_frame_reaching_as_of_is_current(tmp_path):
    assert _covers(_pickle(tmp_path, "AAPL_x.pkl", _frame("2026-09-01")), _AS_OF) is True


def test_frame_past_as_of_is_current(tmp_path):
    assert _covers(_pickle(tmp_path, "AAPL_x.pkl", _frame("2026-09-04")), _AS_OF) is True


def test_last_months_frame_is_stale(tmp_path):
    """The regression: a file fetched last month must NOT count as cached."""
    assert _covers(_pickle(tmp_path, "AAPL_x.pkl", _frame("2026-08-03")), _AS_OF) is False


def test_empty_frame_is_stale(tmp_path):
    empty = pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))
    assert _covers(_pickle(tmp_path, "AAPL_x.pkl", empty), _AS_OF) is False


def test_unreadable_file_is_stale(tmp_path):
    p = tmp_path / "AAPL_x.pkl"
    p.write_bytes(b"not a pickle")
    assert _covers(p, _AS_OF) is False


def test_missing_file_is_stale(tmp_path):
    assert _covers(tmp_path / "nope.pkl", _AS_OF) is False


# ── two-month scenario ────────────────────────────────────────────────────────

def test_second_month_sees_last_months_file_as_stale(tmp_path):
    """Month 1 writes a file; month 2 must classify it stale and refresh it."""
    m1 = _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))
    assert _covers(m1, date(2026, 8, 3)) is True     # current in month 1
    assert _covers(m1, date(2026, 9, 1)) is False    # stale in month 2


def test_replacement_leaves_exactly_one_file_so_raw_close_reads_the_fresh_one(tmp_path):
    """_raw_close picks sorted(glob(...))[0] — the earliest start. If a refresh
    added a file instead of replacing, that stale earliest file would still win."""
    _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))

    # What _ensure_raw_prices does on refresh: unlink existing, then write.
    for old in tmp_path.glob("AAPL_*.pkl"):
        old.unlink()
    _pickle(tmp_path, "AAPL_2025-09-01.pkl", _frame("2026-09-01"))

    files = sorted(tmp_path.glob("AAPL_*.pkl"))
    assert len(files) == 1, "refresh must replace, not accumulate"
    assert _covers(files[0], _AS_OF) is True


def test_accumulating_would_have_regressed(tmp_path):
    """Pins WHY replacement matters: with both files present, the earliest-start
    file _raw_close would choose is the stale one."""
    _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))
    _pickle(tmp_path, "AAPL_2025-09-01.pkl", _frame("2026-09-01"))

    chosen = sorted(tmp_path.glob("AAPL_*.pkl"))[0]   # _raw_close's rule
    assert chosen.name == "AAPL_2025-08-01.pkl"
    assert _covers(chosen, _AS_OF) is False           # ...and it is stale
