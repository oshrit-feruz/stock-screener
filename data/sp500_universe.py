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
    if _pit_cache is not None:
        _PIT_MCAP_DIR.mkdir(parents=True, exist_ok=True)
        _PIT_MCAP_FILE.write_text(json.dumps(_pit_cache))


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
    _save_pit_cache()
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


# ── Point-in-time dollar-volume size proxy (survivorship-free) ───────────
#
# The market-cap ranking above needs EDGAR shares outstanding, and SEC's
# company_tickers.json only lists CURRENTLY-active tickers: every delisted /
# acquired member fails the CIK lookup, gets no shares, and is silently dropped
# from the ranking. That is a survivorship bias in the ranking step even though
# the membership itself is point-in-time correct. Shares are also sparse before
# ~2010 (XBRL mandate), so early-year coverage is only ~60-77%.
#
# Dollar volume (raw close × volume, trailing median) needs ONLY prices, which
# exist for delisted names too and go back decades, so it ranks the full
# point-in-time membership with no survivorship bias and no EDGAR dependency.
# It is a LIQUIDITY proxy for size, not exact market cap — highly correlated for
# mega-caps, but tilted toward high-turnover names (tech/semis). Validated
# look-ahead-free in scripts/audit_clean_pit.py.
_DV_WINDOW = 63  # trailing trading days for the median dollar-volume
_PIT_DV_DIR = Path(__file__).parent / "cache" / "pit_dollar_volume"
_PIT_DV_FILE = _PIT_DV_DIR / "pit_dollar_volumes.json"
_pit_dv_cache: dict[str, dict] | None = None


def _load_pit_dv_cache() -> dict[str, dict]:
    global _pit_dv_cache
    if _pit_dv_cache is None:
        _PIT_DV_DIR.mkdir(parents=True, exist_ok=True)
        if _PIT_DV_FILE.exists():
            try:
                _pit_dv_cache = json.loads(_PIT_DV_FILE.read_text())
            except Exception:
                _pit_dv_cache = {}
        else:
            _pit_dv_cache = {}
    return _pit_dv_cache


def _save_pit_dv_cache() -> None:
    if _pit_dv_cache is not None:
        _PIT_DV_DIR.mkdir(parents=True, exist_ok=True)
        _PIT_DV_FILE.write_text(json.dumps(_pit_dv_cache))


def _raw_frame(ticker: str):
    """Memoised raw (unadjusted) OHLCV frame for a ticker, or None."""
    import pickle

    if ticker not in _raw_frames:
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
    return _raw_frames[ticker]


def _compute_pit_dollar_volume(ticker: str, date: str) -> float | None:
    """Trailing `_DV_WINDOW`-day median of raw close × volume, as of `date`.

    Uses only data on or before `date` (no look-ahead). Returns None if there is
    no raw frame, no Volume column, or fewer than `_DV_WINDOW` observations.
    """
    frame = _raw_frame(ticker)
    if frame is None or getattr(frame, "empty", True) or "Volume" not in frame.columns:
        return None
    import pandas as pd

    sub = frame[frame.index <= pd.Timestamp(date)]
    if len(sub) < _DV_WINDOW:
        return None
    dvol = (sub["Close"].astype(float) * sub["Volume"].astype(float))
    med = float(dvol.rolling(_DV_WINDOW).median().iloc[-1])
    return med if med > 0 and med == med else None  # med==med rejects NaN


def pit_dollar_volume(ticker: str, date: str) -> float | None:
    """Point-in-time median dollar-volume, cached (30-day TTL; immutable for old
    dates, see `_pit_entry_valid`)."""
    cache = _load_pit_dv_cache()
    ck = f"{ticker}|{date}"
    entry = cache.get(ck)
    if _pit_entry_valid(date, entry, time.time()):
        v = entry.get("dv")
        return float(v) if v is not None else None
    v = _compute_pit_dollar_volume(ticker, date)
    cache[ck] = {"dv": v, "ts": time.time()}
    _save_pit_dv_cache()
    return v


def prefetch_pit_dollar_volumes(tickers: list[str], dates: list[str]) -> None:
    """Warm the dollar-volume cache for the (ticker, date) grid, one save at end."""
    cache = _load_pit_dv_cache()
    now = time.time()
    changed = False
    for date in dates:
        for t in tickers:
            ck = f"{t}|{date}"
            if _pit_entry_valid(date, cache.get(ck), now):
                continue
            cache[ck] = {"dv": _compute_pit_dollar_volume(t, date), "ts": now}
            changed = True
    if changed:
        _save_pit_dv_cache()


def get_universe_top_n_by_market_cap(date: str, n: int) -> list[str]:
    """The `n` largest members on `date` by point-in-time market cap (raw close ×
    EDGAR shares). Retained for reference/comparison; NOT survivorship-free —
    delisted members have no CIK and are dropped. Prefer `get_universe_top_n`.
    """
    members = get_universe(date)
    capped = [(t, pit_market_cap(t, date)) for t in members]
    capped = [(t, mc) for t, mc in capped if mc is not None and mc > 0]
    capped.sort(key=lambda x: -x[1])
    return [t for t, _ in capped[:n]]


def get_universe_top_n(date: str, n: int) -> list[str]:
    """The `n` largest S&P 500 members on `date`, ranked by POINT-IN-TIME
    dollar-volume (survivorship-free; see the note above).

    Members whose dollar-volume cannot be computed (no raw price data or < 63
    observations as of the date) are excluded silently.
    """
    members = get_universe(date)
    ranked = [(t, pit_dollar_volume(t, date)) for t in members]
    ranked = [(t, dv) for t, dv in ranked if dv is not None and dv > 0]
    ranked.sort(key=lambda x: -x[1])
    return [t for t, _ in ranked[:n]]



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
