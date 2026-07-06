"""EODHD end-of-day price adapter.

Replaces yfinance as the network source behind ``PriceData``. yfinance (via
curl_cffi/BoringSSL) cannot complete a TLS handshake through the agent proxy on
Render, and Yahoo drops many delisted tickers. EODHD is reached with plain
``requests`` (honours REQUESTS_CA_BUNDLE), serves delisted tickers, and returns
both raw OHLCV and ``adjusted_close`` in a single call.

Contract (verified against the paid plan):
    GET https://eodhd.com/api/eod/{TICKER}.US?api_token=KEY&fmt=json&from=&to=
    -> JSON array, ascending by date. Each bar:
       date "YYYY-MM-DD" | open/high/low/close/adjusted_close (float) | volume (int)

The API token is read from the ``EODHD_API_KEY`` environment variable and is
never hardcoded. On any error (missing key, HTTP error, empty payload) the fetch
logs a clear message and returns an empty DataFrame so the caller can fall back
to a stale cache and the run does not crash.
"""
from __future__ import annotations

import logging
import os

import pandas as pd
import requests

log = logging.getLogger(__name__)

_BASE_URL = "https://eodhd.com/api/eod"
_TIMEOUT = 30
_ENV_KEY = "EODHD_API_KEY"


def normalize_ticker(ticker: str) -> str:
    """Map an internal ticker to EODHD's ``SYMBOL.US`` form.

    Class-suffix tickers use a dash on EODHD, not a dot: ``BRK.B`` -> ``BRK-B.US``
    (the dot form 404s). Any ``.US`` already present is preserved.
    """
    t = ticker.strip().upper()
    if t.endswith(".US"):
        base = t[:-3]
    else:
        base = t
    # Class suffixes (BRK.B, BF.B) -> dash. Whole-symbol dots become dashes.
    base = base.replace(".", "-")
    return f"{base}.US"


def _api_key() -> str | None:
    key = os.environ.get(_ENV_KEY, "").strip()
    return key or None


def fetch_eod(ticker: str, start: str, end: str, adjust: bool = True) -> pd.DataFrame:
    """Fetch daily bars for ``ticker`` in ``[start, end]`` (inclusive) from EODHD.

    adjust=True  -> split+dividend adjusted, ``Close == adjusted_close`` and OHL
                    scaled by ``adjusted_close/close`` (matches yfinance
                    ``auto_adjust=True`` — the frame the backtest consumes).
    adjust=False -> RAW (unadjusted) OHLCV, needed for point-in-time market cap
                    (raw price x raw EDGAR shares).

    Returns a DataFrame indexed by a naive DatetimeIndex named ``Date`` with
    columns Open/High/Low/Close/Volume, or an EMPTY DataFrame on any failure
    (missing key, HTTP/JSON error, no rows). Never raises.
    """
    key = _api_key()
    if key is None:
        log.warning("EODHD: %s is not set; cannot fetch %s", _ENV_KEY, ticker)
        return pd.DataFrame()

    symbol = normalize_ticker(ticker)
    url = f"{_BASE_URL}/{symbol}"
    params = {
        "api_token": key,
        "fmt": "json",
        "from": start,
        "to": end,
        "period": "d",
    }
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
    except Exception as exc:  # network / TLS / timeout
        log.warning("EODHD: request failed for %s (%s): %r", ticker, symbol, exc)
        return pd.DataFrame()

    if resp.status_code != 200:
        # 404 => unknown symbol (e.g. wrong suffix); others => transient/server.
        log.warning(
            "EODHD: HTTP %s for %s (%s): %s",
            resp.status_code, ticker, symbol, resp.text[:120],
        )
        return pd.DataFrame()

    try:
        rows = resp.json()
    except Exception as exc:
        log.warning("EODHD: bad JSON for %s (%s): %r", ticker, symbol, exc)
        return pd.DataFrame()

    if not isinstance(rows, list) or not rows:
        log.warning("EODHD: empty payload for %s (%s) in [%s, %s]", ticker, symbol, start, end)
        return pd.DataFrame()

    return _to_frame(rows, adjust=adjust, ticker=ticker)


def _to_frame(rows: list[dict], adjust: bool, ticker: str) -> pd.DataFrame:
    """Convert the EODHD JSON array into the OHLCV frame ``PriceData`` expects."""
    df = pd.DataFrame(rows)
    required = {"date", "open", "high", "low", "close", "adjusted_close", "volume"}
    missing = required - set(df.columns)
    if missing:
        log.warning("EODHD: %s missing fields %s; skipping", ticker, sorted(missing))
        return pd.DataFrame()

    # Build from numpy arrays (not Series) so the constructor does not try to
    # align each column's RangeIndex against the DatetimeIndex (which would NaN
    # everything out).
    idx = pd.DatetimeIndex(pd.to_datetime(df["date"]).dt.normalize().to_numpy())
    out = pd.DataFrame(
        {
            "Open": pd.to_numeric(df["open"], errors="coerce").to_numpy(),
            "High": pd.to_numeric(df["high"], errors="coerce").to_numpy(),
            "Low": pd.to_numeric(df["low"], errors="coerce").to_numpy(),
            "Close": pd.to_numeric(df["close"], errors="coerce").to_numpy(),
            "Volume": pd.to_numeric(df["volume"], errors="coerce").to_numpy(),
        },
        index=idx,
    )
    out["_adj"] = pd.to_numeric(df["adjusted_close"], errors="coerce").to_numpy()
    out = out.dropna(subset=["Close"])
    if out.empty:
        return out.drop(columns=["_adj"])

    if adjust:
        # Match yfinance auto_adjust: scale OHL by adjusted_close/close, Close := adj.
        factor = out["_adj"] / out["Close"]
        for col in ("Open", "High", "Low"):
            out[col] = out[col] * factor
        out["Close"] = out["_adj"]
        out = out.dropna(subset=["Close"])

    out = out.drop(columns=["_adj"]).sort_index()
    out.index.name = "Date"
    return out
