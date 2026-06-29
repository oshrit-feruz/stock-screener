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
import os
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


# ── Market-cap size filter ────────────────────────────────────────────────────
#
# CAVEAT: this uses *current* market cap as a static proxy and applies it to all
# historical dates (cached 30 days). That is what the spec asks for, but two
# biases follow and must be read alongside any top-N result:
#   1. Ranking by today's size favours the names that *grew* — a form of
#      look-ahead/survivorship bias.
#   2. Delisted/renamed names usually have no current cap, so they are silently
#      excluded — exactly the failures the point-in-time universe was meant to
#      include. An unbiased version would use price × historical shares
#      outstanding (e.g. from EDGAR) per date.
#
# Transport note: yfinance's `info` (curl_cffi backend) cannot complete a TLS
# handshake through the agent proxy. yfinance reads marketCap from Yahoo's
# `v7/finance/quote` endpoint, so we read that same endpoint directly with plain
# `requests` (it needs a crumb + cookie). FMP's `quote` endpoint is a last
# resort. All three yield the same `marketCap` field.

_MCAP_DIR = Path(__file__).parent / "cache" / "market_cap"
_MCAP_FILE = _MCAP_DIR / "market_caps.json"
_MCAP_TTL_SECONDS = 30 * 86400
_mcap_cache: dict[str, dict] | None = None  # ticker -> {"marketCap": float|None, "ts": epoch}
_yahoo_session = None
_yahoo_crumb: str | None = None


def _yahoo_quote(symbols: list[str]) -> dict[str, float | None]:
    """Batch marketCap from Yahoo's quote endpoint (the source yfinance uses).

    Establishes a cookie + crumb once and reuses it; refreshes on a 401.
    Returns {symbol: marketCap|None}; symbols absent from the response map to None.
    """
    global _yahoo_session, _yahoo_crumb
    out: dict[str, float | None] = {s: None for s in symbols}
    try:
        if _yahoo_session is None:
            _yahoo_session = requests.Session()
            _yahoo_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            _yahoo_session.get("https://fc.yahoo.com", timeout=15)
            _yahoo_crumb = _yahoo_session.get(
                "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15
            ).text.strip()

        for i in range(0, len(symbols), 50):
            batch = symbols[i:i + 50]
            r = _yahoo_session.get(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                params={"symbols": ",".join(batch), "crumb": _yahoo_crumb},
                timeout=20,
            )
            if r.status_code == 401:  # crumb expired — refresh once and retry
                _yahoo_crumb = _yahoo_session.get(
                    "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15
                ).text.strip()
                r = _yahoo_session.get(
                    "https://query1.finance.yahoo.com/v7/finance/quote",
                    params={"symbols": ",".join(batch), "crumb": _yahoo_crumb},
                    timeout=20,
                )
            if r.status_code != 200:
                continue
            for q in r.json().get("quoteResponse", {}).get("result", []):
                mc = q.get("marketCap")
                if mc:
                    out[q.get("symbol")] = float(mc)
    except Exception:
        pass
    return out


def _load_mcap_cache() -> dict[str, dict]:
    global _mcap_cache
    if _mcap_cache is None:
        _MCAP_DIR.mkdir(parents=True, exist_ok=True)
        if _MCAP_FILE.exists():
            try:
                _mcap_cache = json.loads(_MCAP_FILE.read_text())
            except Exception:
                _mcap_cache = {}
        else:
            _mcap_cache = {}
    return _mcap_cache


def _save_mcap_cache() -> None:
    if _mcap_cache is not None:
        _MCAP_DIR.mkdir(parents=True, exist_ok=True)
        _MCAP_FILE.write_text(json.dumps(_mcap_cache))


def _fetch_market_cap(ticker: str) -> float | None:
    """Current market cap for `ticker`, or None if unavailable.

    Tries yfinance first (the spec's source); falls back to FMP's `quote`
    endpoint via `requests` because the vendored yfinance/curl_cffi cannot reach
    Yahoo through the agent proxy.
    """
    try:
        import yfinance as yf

        mc = yf.Ticker(ticker).info.get("marketCap")
        if mc:
            return float(mc)
    except Exception:
        pass

    mc = _yahoo_quote([ticker]).get(ticker)
    if mc:
        return mc

    key = os.environ.get("FMP_API_KEY")
    if key:
        try:
            r = requests.get(
                "https://financialmodelingprep.com/stable/quote",
                params={"symbol": ticker, "apikey": key},
                timeout=20,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    mc = data[0].get("marketCap")
                    if mc:
                        return float(mc)
        except Exception:
            pass
    return None


def _market_cap(ticker: str) -> float | None:
    cache = _load_mcap_cache()
    entry = cache.get(ticker)
    if entry is not None and (time.time() - entry.get("ts", 0)) < _MCAP_TTL_SECONDS:
        mc = entry.get("marketCap")
        return float(mc) if mc is not None else None
    mc = _fetch_market_cap(ticker)
    cache[ticker] = {"marketCap": mc, "ts": time.time()}
    _save_mcap_cache()
    return mc


def prefetch_market_caps(tickers: list[str]) -> None:
    """Warm the market-cap cache for many tickers up front via Yahoo's batched
    quote endpoint (one save at the end). Only fetches tickers whose cached value
    is missing or older than the TTL."""
    cache = _load_mcap_cache()
    stale = [t for t in tickers
             if cache.get(t) is None
             or (time.time() - cache[t].get("ts", 0)) >= _MCAP_TTL_SECONDS]
    if not stale:
        return
    caps = _yahoo_quote(stale)
    now = time.time()
    for t in stale:
        cache[t] = {"marketCap": caps.get(t), "ts": now}
    _save_mcap_cache()


def get_universe_top_n(date: str, n: int) -> list[str]:
    """The `n` largest S&P 500 members on `date` by (current) market cap.

    Members with no available market cap are excluded silently. Returns fewer
    than `n` tickers only if too few members have a usable cap.
    """
    members = get_universe(date)
    capped = [(t, _market_cap(t)) for t in members]
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
