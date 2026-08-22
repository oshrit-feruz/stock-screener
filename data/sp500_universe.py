# Data source: github.com/fja05680/sp500 — temporary, replace with FMP paid API when upgrading to production
"""Point-in-time S&P 500 universe.

Reconstructs the exact S&P 500 membership on any date from a free historical
dataset: a CSV where each row is a snapshot (date, comma-separated tickers) of
the full index on that date, going back to 1996.

Public interface (kept deliberately small so the backing source can later be
swapped for the FMP paid API without touching any caller):
    get_universe(date: str) -> list[str]
    get_universe_top_n(date: str, n: int) -> list[str]
    validate_universe(date: str) -> None
"""
from __future__ import annotations

import atexit
import bisect
import csv
import io
import json
import time
from datetime import date as _date
from pathlib import Path

import requests

_CSV_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)
_CACHE_DIR = Path(__file__).parent / "cache" / "sp500_universe"
_CACHE_FILE = _CACHE_DIR / "sp500_historical_components.csv"
_CACHE_TTL_SECONDS = 7 * 86400
_TIMEOUT = 60

# Parsed snapshots, sorted ascending by date: (iso_date, [tickers]).
# Loaded lazily and memoised for the process lifetime.
_snapshots: list[tuple[str, list[str]]] | None = None
_snapshot_dates: list[str] | None = None


def _cache_fresh(path: Path) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < _CACHE_TTL_SECONDS


def _download_csv() -> str | None:
    try:
        resp = requests.get(_CSV_URL, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return None


def _load_csv_text() -> str:
    """Return the CSV text, (re)downloading when the cache is missing or stale.

    Falls back to a stale cached copy if a refresh fails, so a transient network
    error never takes the whole backtest down.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if _cache_fresh(_CACHE_FILE):
        return _CACHE_FILE.read_text()

    text = _download_csv()
    if text is not None:
        _CACHE_FILE.write_text(text)
        return text

    if _CACHE_FILE.exists():
        return _CACHE_FILE.read_text()  # stale, but better than nothing
    raise RuntimeError(
        f"Could not download the S&P 500 history from {_CSV_URL} and no cache exists."
    )


def _parse(text: str) -> list[tuple[str, list[str]]]:
    """Parse the CSV into sorted (iso_date, tickers) snapshots.

    The tickers column is itself comma-separated and therefore quoted in the
    file (`YYYY-MM-DD,"TICK1,TICK2,..."`), so it must be parsed with a real CSV
    reader — a naive split on commas would leave stray quotes on the first and
    last ticker of every row.
    """
    snapshots: list[tuple[str, list[str]]] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        date_str = row[0].strip()
        if date_str.lower() == "date":          # header
            continue
        try:
            _date.fromisoformat(date_str)
        except ValueError:
            continue                             # skip any malformed row
        # Remaining fields are the tickers (csv.reader already stripped quotes;
        # join handles the rare case of an unquoted multi-field row).
        ticker_blob = ",".join(row[1:])
        tickers = sorted({t.strip() for t in ticker_blob.split(",") if t.strip()})
        if tickers:
            snapshots.append((date_str, tickers))
    snapshots.sort(key=lambda r: r[0])
    return snapshots


def _ensure_loaded() -> None:
    global _snapshots, _snapshot_dates
    if _snapshots is None:
        _snapshots = _parse(_load_csv_text())
        _snapshot_dates = [d for d, _ in _snapshots]


def get_universe(date: str) -> list[str]:
    """Tickers that were S&P 500 members on `date` (YYYY-MM-DD).

    Uses the most recent snapshot whose date is on or before `date`.
    Raises ValueError if `date` precedes the first available snapshot.
    """
    _date.fromisoformat(date)  # validate format early
    _ensure_loaded()
    assert _snapshots is not None and _snapshot_dates is not None

    # Rightmost snapshot with snapshot_date <= date.
    idx = bisect.bisect_right(_snapshot_dates, date) - 1
    if idx < 0:
        raise ValueError(
            f"{date} is before the first available S&P 500 snapshot "
            f"({_snapshot_dates[0]})."
        )
    return list(_snapshots[idx][1])


# ── Point-in-time market-cap size filter ─────────────────────────────────
#
# Market cap is computed point-in-time, NOT from a current snapshot:
#     pit_market_cap = raw_close_on_date × shares_outstanding_from_EDGAR
#
# - raw_close: UNADJUSTED close from data/cache/prices_raw (split-adjusted prices
#   would deflate any future-splitter — NVDA 40:1, AMZN/GOOGL 20:1, AAPL 4:1 —
#   and corrupt the cross-sectional ranking).
# - shares: EdgarFundamentals.get_shares_outstanding with the same 90-day
#   publication lag as the quality gate (most recent 10-K/10-Q filed on or before
#   date − 90d).
# If either is missing the ticker is excluded — there is NO fallback to current
# market cap. Results cached under data/cache/pit_market_cap/ (30-day TTL), keyed
# by ticker+date (monthly granularity).

_RAW_PRICE_DIR = Path(__file__).parent / "cache" / "prices_raw"
_RAW_PRICE_START = "2016-01-01"
_PIT_MCAP_DIR = Path(__file__).parent / "cache" / "pit_market_cap"
_PIT_MCAP_FILE = _PIT_MCAP_DIR / "pit_market_caps.json"
_PIT_MCAP_TTL_SECONDS = 30 * 86400

_pit_cache: dict[str, dict] | None = None
_pit_dirty = False                       # unsaved in-memory grid entries pending flush
_raw_frames: dict[str, object] = {}      # ticker -> DataFrame | None (memoised)
_edgar = None                            # lazy EdgarFundamentals
_shares_memo: dict[tuple[str, str], float | None] = {}


def _get_edgar():
    global _edgar
    if _edgar is None:
        from core.data.edgar import EdgarFundamentals
        from core.data.eodhd_fundamentals import EODHDFundamentals
        _edgar = EdgarFundamentals(fallback=EODHDFundamentals())
    return _edgar


def _raw_close(ticker: str, date: str) -> float | None:
    """Unadjusted closing price on or before `date` from the raw price cache."""
    import pickle

    if ticker not in _raw_frames:
        # Prefer the EARLIEST-start raw file so it covers the most history (e.g. a
        # 2008-start file is needed for 2010-2017 dates; a 2016-start one is not).
        matches = sorted(_RAW_PRICE_DIR.glob(f"{ticker}_*.pkl"))
        path = matches[0] if matches else None
        frame = None
        if path is not None and path.exists():
            try:
                with open(path, "rb") as f:
                    frame = pickle.load(f)
            except Exception:
                frame = None
        _raw_frames[ticker] = frame

    frame = _raw_frames[ticker]
    if frame is None or getattr(frame, "empty", True):
        return None
    import pandas as pd

    sub = frame[frame.index <= pd.Timestamp(date)]
    if sub.empty:
        return None
    try:
        return float(sub["Close"].iloc[-1])
    except Exception:
        return None


def _shares(ticker: str, date: str) -> float | None:
    key = (ticker, date)
    if key not in _shares_memo:
        try:
            _shares_memo[key] = _get_edgar().get_shares_outstanding(ticker, date)
        except Exception:
            _shares_memo[key] = None
    return _shares_memo[key]


def _load_pit_cache() -> dict[str, dict]:
    global _pit_cache
    if _pit_cache is None:
        _PIT_MCAP_DIR.mkdir(parents=True, exist_ok=True)
        if _PIT_MCAP_FILE.exists():
            try:
                _pit_cache = json.loads(_PIT_MCAP_FILE.read_text())
            except Exception:
                _pit_cache = {}
        else:
            _pit_cache = {}
    return _pit_cache


def _save_pit_cache() -> None:
    global _pit_dirty
    if _pit_cache is not None:
        _PIT_MCAP_DIR.mkdir(parents=True, exist_ok=True)
        _PIT_MCAP_FILE.write_text(json.dumps(_pit_cache))
    _pit_dirty = False


def _flush_pit_cache() -> None:
    """Write the grid to disk only if new entries are pending.

    pit_market_cap() used to call _save_pit_cache() after EVERY computed
    entry — a json.dumps of the whole grid (166k entries, ~51MB as Python
    objects, ~11MB serialized) per member. A single screener universe build
    for an uncached date triggers ~503 computes, i.e. ~503 full-grid dumps:
    measured as the bulk of a +188MB RSS spike (transient dump strings) and
    a large slice of the recompute wall time. Computes now just mark the
    grid dirty; outer loops flush once when done (plus an atexit flush as a
    backstop for direct pit_market_cap callers, e.g. research scripts).
    """
    if _pit_dirty:
        _save_pit_cache()


atexit.register(_flush_pit_cache)


# A point-in-time market cap for a date more than this many days in the past is
# immutable: the raw close is history, and the shares-outstanding figure that was
# public as of that date can no longer change (a later filing does not alter what
# was known then). Cached entries for such dates never expire — this lets a
# prebuilt cache ship historical caps that stay valid indefinitely, so a cold
# deploy never has to recompute them from the raw-price / EDGAR caches.
_PIT_MCAP_IMMUTABLE_AGE_DAYS = 120


def _pit_entry_valid(date: str, entry: dict | None, now: float) -> bool:
    """True if a cached pit-market-cap entry may be reused as-is.

    Fresh within the TTL, OR the as-of date is old enough that the value is
    immutable (see _PIT_MCAP_IMMUTABLE_AGE_DAYS) — in which case it never expires.
    """
    if entry is None:
        return False
    if (now - entry.get("ts", 0)) < _PIT_MCAP_TTL_SECONDS:
        return True
    try:
        age_days = (_date.today() - _date.fromisoformat(date)).days
    except Exception:
        return False
    return age_days > _PIT_MCAP_IMMUTABLE_AGE_DAYS


def pit_market_cap(ticker: str, date: str) -> float | None:
    """Point-in-time market cap = raw close × EDGAR shares outstanding.

    Returns None (and caches None) if either input is missing. Cached 30 days;
    entries for immutable historical dates never expire (see _pit_entry_valid).
    """
    cache = _load_pit_cache()
    ck = f"{ticker}|{date}"
    entry = cache.get(ck)
    if _pit_entry_valid(date, entry, time.time()):
        mc = entry.get("mcap")
        return float(mc) if mc is not None else None

    mc = _compute_pit_mcap(ticker, date)
    cache[ck] = {"mcap": mc, "ts": time.time()}
    global _pit_dirty
    _pit_dirty = True   # batched: flushed by the outer loop / atexit, not per entry
    return mc


def _compute_pit_mcap(ticker: str, date: str) -> float | None:
    px = _raw_close(ticker, date)
    if not px or px <= 0:          # no price → skip the EDGAR lookup entirely
        return None
    sh = _shares(ticker, date)
    return float(px * sh) if (sh and sh > 0) else None


def prefetch_pit_market_caps(tickers: list[str], dates: list[str]) -> None:
    """Warm the point-in-time market-cap cache for the (ticker, date) grid, with a
    single save at the end."""
    cache = _load_pit_cache()
    now = time.time()
    changed = False
    for date in dates:
        for t in tickers:
            ck = f"{t}|{date}"
            if _pit_entry_valid(date, cache.get(ck), now):
                continue
            cache[ck] = {"mcap": _compute_pit_mcap(t, date), "ts": now}
            changed = True
    if changed:
        _save_pit_cache()


def get_universe_top_n(date: str, n: int) -> list[str]:
    """The `n` largest S&P 500 members on `date` by POINT-IN-TIME market cap
    (raw close × EDGAR shares outstanding, 90-day filing lag).

    Members whose point-in-time market cap cannot be computed (no price or no
    EDGAR filing in range) are excluded silently. No current-market-cap fallback.
    """
    members = get_universe(date)
    capped = [(t, pit_market_cap(t, date)) for t in members]
    _flush_pit_cache()   # one write for the whole ranking, not one per member
    capped = [(t, mc) for t, mc in capped if mc is not None and mc > 0]
    capped.sort(key=lambda x: -x[1])
    return [t for t, _ in capped[:n]]



def validate_universe(date: str) -> None:
    """Print the member count and a 10-ticker sample for `date` (sanity check)."""
    members = get_universe(date)
    sample = members[:10]
    print(f"{date}: {len(members)} S&P 500 members")
    print(f"  sample (first 10): {', '.join(sample)}")


if __name__ == "__main__":
    import sys

    for d in sys.argv[1:] or ["2010-01-04", "2018-01-02", "2020-12-21"]:
        validate_universe(d)
