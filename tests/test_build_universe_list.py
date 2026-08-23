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

from scripts.build_universe_list import (
    _covers,
    _frame_reaches,
    _refresh_start,
    _replace_ticker_cache,
    _ticker_is_current,
)

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

    _replace_ticker_cache(tmp_path, "AAPL", "2025-08-01", _frame("2026-09-01"))

    files = sorted(tmp_path.glob("AAPL_*.pkl"))
    assert len(files) == 1, "refresh must replace, not accumulate"
    assert _covers(files[0], _AS_OF) is True


def test_serialization_failure_preserves_existing_cache(tmp_path, monkeypatch):
    old = _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))
    original = old.read_bytes()

    def fail_dump(frame, file):
        file.write(b"partial")
        raise RuntimeError("serialization failed")

    monkeypatch.setattr("scripts.build_universe_list.pickle.dump", fail_dump)

    with pytest.raises(RuntimeError, match="serialization failed"):
        _replace_ticker_cache(tmp_path, "AAPL", "2025-08-01", _frame("2026-09-01"))

    assert old.read_bytes() == original
    assert sorted(tmp_path.glob("AAPL_*.pkl")) == [old]
    assert not list(tmp_path.glob("*.tmp"))


def test_temp_file_open_failure_preserves_existing_cache(tmp_path, monkeypatch):
    old = _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))
    original = old.read_bytes()

    def fail_open(**kwargs):
        raise OSError("cannot open temporary file")

    monkeypatch.setattr("scripts.build_universe_list.tempfile.NamedTemporaryFile", fail_open)

    with pytest.raises(OSError, match="cannot open temporary file"):
        _replace_ticker_cache(tmp_path, "AAPL", "2025-08-01", _frame("2026-09-01"))

    assert old.read_bytes() == original
    assert sorted(tmp_path.glob("AAPL_*.pkl")) == [old]


# ── _ticker_is_current: judged on the file _raw_close actually reads ──────────

def test_ticker_with_no_files_is_not_current(tmp_path):
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is False


def test_ticker_with_single_fresh_file_is_current(tmp_path):
    _pickle(tmp_path, "AAPL_2025-09-01.pkl", _frame("2026-09-01"))
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is True


def test_ticker_with_single_stale_file_is_not_current(tmp_path):
    _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is False


def test_ticker_with_stale_AND_fresh_file_is_not_current(tmp_path):
    """The regression an `any(covers)` test would miss: a fresh file exists, but
    _raw_close reads the earliest-start one, which is stale. Must refresh."""
    _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))   # earliest -> chosen
    _pickle(tmp_path, "AAPL_2025-09-01.pkl", _frame("2026-09-01"))   # fresh but ignored
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is False


def test_other_tickers_do_not_leak_into_the_decision(tmp_path):
    _pickle(tmp_path, "MSFT_2025-09-01.pkl", _frame("2026-09-01"))
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is False


def test_accumulating_would_have_regressed(tmp_path):
    """Pins WHY replacement matters: with both files present, the earliest-start
    file _raw_close would choose is the stale one."""
    _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))
    _pickle(tmp_path, "AAPL_2025-09-01.pkl", _frame("2026-09-01"))

    chosen = sorted(tmp_path.glob("AAPL_*.pkl"))[0]   # _raw_close's rule
    assert chosen.name == "AAPL_2025-08-01.pkl"
    assert _covers(chosen, _AS_OF) is False           # ...and it is stale


# ── refresh must never truncate existing history ──────────────────────────────

def test_refresh_start_keeps_deep_history(tmp_path):
    """build_full_cache.py writes deep files (2009-) that the PIT grid rebuild
    needs. A monthly refresh replaces the ticker's file, so it must refetch from
    at least as far back — otherwise history is silently truncated and
    build_full_cache's 'already have a file?' check skips restoring it."""
    (tmp_path / "AAPL_2009-01-01.pkl").write_bytes(b"x")
    assert _refresh_start(tmp_path, "AAPL", "2025-09-01") == "2009-01-01"


def test_refresh_start_uses_default_when_no_deeper_history(tmp_path):
    (tmp_path / "AAPL_2025-09-01.pkl").write_bytes(b"x")
    assert _refresh_start(tmp_path, "AAPL", "2025-08-01") == "2025-08-01"


def test_refresh_start_with_no_existing_files(tmp_path):
    assert _refresh_start(tmp_path, "AAPL", "2025-09-01") == "2025-09-01"


def test_refresh_start_ignores_unparsable_filenames(tmp_path):
    (tmp_path / "AAPL_notadate.pkl").write_bytes(b"x")
    assert _refresh_start(tmp_path, "AAPL", "2025-09-01") == "2025-09-01"


# ── fetched frames must be validated before they are trusted or stored ────────

def test_frame_reaching_as_of_is_accepted():
    assert _frame_reaches(_frame("2026-09-01"), _AS_OF) is True


def test_nonempty_frame_ending_before_as_of_is_rejected():
    """fetch_eod can return a populated frame whose last bar predates as_of
    (thin coverage, a halt, a partial response). Storing it would rank the very
    same build on a stale close, and the file would look perfectly healthy."""
    assert _frame_reaches(_frame("2026-08-03"), _AS_OF) is False


def test_empty_frame_is_rejected():
    empty = pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))
    assert _frame_reaches(empty, _AS_OF) is False


def test_none_frame_is_rejected():
    assert _frame_reaches(None, _AS_OF) is False
