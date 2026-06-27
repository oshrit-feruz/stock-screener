# Data source: github.com/fja05680/sp500 — temporary, replace with FMP paid API when upgrading to production
"""Point-in-time S&P 500 universe.

Reconstructs the exact S&P 500 membership on any date from a free historical
dataset: a CSV where each row is a snapshot (date, comma-separated tickers) of
the full index on that date, going back to 1996.

Public interface (kept deliberately small so the backing source can later be
swapped for the FMP paid API without touching any caller):
    get_universe(date: str) -> list[str]
    validate_universe(date: str) -> None
"""
from __future__ import annotations

import bisect
import csv
import io
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
