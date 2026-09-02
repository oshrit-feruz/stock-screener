from __future__ import annotations

import logging
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from core.data.eodhd import fetch_eod

log = logging.getLogger(__name__)

_DEFAULT_CACHE = Path(__file__).parent.parent.parent / "data" / "cache" / "prices"

# What a cached pickle is allowed to raise on load: anything. The list used to
# name five error types, and a frame pickled by a newer pandas fails to
# reconstruct on an older one with none of them — TypeError on pandas 2.1/2.2
# ("StringDtype.__init__() takes from 1 to 2 positional arguments but 3 were
# given"), NotImplementedError on 2.3. Uncaught, that escaped get_prices and
# took the whole backtest down. The honest outcome of an unreadable cache
# file is a warning naming it and a live fetch, whatever the exception.
_CACHE_LOAD_ERRORS = (Exception,)


def _safe_ticker(ticker: str) -> str:
    """Sanitize a ticker symbol for use in cache paths.
    
    Returns:
        str: The ticker containing only alphanumeric characters, hyphens, and underscores.
    """
    return "".join(c for c in ticker if c.isalnum() or c in "-_")


def _key_start_ts(path: Path) -> pd.Timestamp | None:
    """The `start` date encoded in a cache filename ({ticker}_{start}.pkl), or
    None if it doesn't parse. rsplit tolerates underscores in the ticker part."""
    try:
        return pd.Timestamp(path.stem.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return None


class PriceData:
    def __init__(self, cache_dir: Path = _DEFAULT_CACHE):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, ticker: str, start: str) -> Path:
        # Cache key excludes `end`: historical prices for a given start are
        # identical regardless of the requested end date. Keying by end too
        # would force a full re-download every day as `end` advances.
        safe_start = start.replace("/", "-").replace("\\", "-")
        return self.cache_dir / f"{_safe_ticker(ticker)}_{safe_start}.pkl"

    def _find_covering_cache(self, ticker: str, start_ts: pd.Timestamp,
                              end_ts: pd.Timestamp) -> pd.DataFrame | None:
        """
                              Find cached price data for a ticker that covers the requested interval.
                              
                              Parameters:
                                  ticker (str): Ticker symbol whose cached data is searched.
                                  start_ts (pd.Timestamp): Inclusive start of the requested interval.
                                  end_ts (pd.Timestamp): Exclusive end of the requested interval.
                              
                              Returns:
                                  pd.DataFrame | None: The earliest suitable cached dataset, or `None` if no covering cache is available.
                              """
        best = None
        for p in sorted(self.cache_dir.glob(f"{_safe_ticker(ticker)}_*.pkl")):
            try:
                with open(p, "rb") as f:
                    df = pickle.load(f)
            except _CACHE_LOAD_ERRORS as exc:
                log.warning("Failed to load cache file %s: %s", p, exc)
                continue
            if df is None or df.empty:
                continue
            # Start-side coverage is satisfied by EITHER of:
            #   1. the data itself reaching back to start_ts, or
            #   2. the file's KEY start (the `start` of the request that produced
            #      it, encoded in the filename) being <= start_ts. EODHD returns
            #      everything it has from the requested start, so a file fetched
            #      from an earlier start is complete even when its first data bar
            #      is later — the ticker simply has no earlier data (IPO/spinoff:
            #      APP 2021, GEV 2024, ...). Without (2), every late-IPO ticker
            #      failed the min<=start check and live-refetched its full range
            #      on every cold boot, only to receive bytes identical to this
            #      cached file.
            key_start = _key_start_ts(p)
            starts_ok = (df.index.min() <= start_ts
                         or (key_start is not None and key_start <= start_ts))
            if starts_ok and df.index.max() >= end_ts - pd.Timedelta(days=1):
                if best is None or df.index.min() < best.index.min():
                    best = df
        return best

    # Freshness bound for "current price" contexts (dashboard marks, alert
    # checks — anywhere the last close is presented as the price NOW). 7
    # trading days: generous enough for a holiday-extended weekend plus a few
    # provider hiccups, far too tight for the multi-week drift that shipped a
    # 2026-06-30 close labeled "(now)" on 2026-08-22. Historical/backtest
    # windows must NOT pass this — an old window is stale by definition.
    CURRENT_MAX_STALE_TDAYS = 7

    def get_prices(self, ticker: str, start: str, end: str,
                   max_stale_tdays: int | None = None) -> pd.DataFrame:
        """
                   Load OHLCV price data for the half-open interval [start, end).
                   
                   Parameters:
                       ticker (str): Instrument identifier.
                       start (str): Inclusive start date.
                       end (str): Exclusive end date.
                       max_stale_tdays (int | None): Maximum permitted trading-day age of the
                           latest price bar. If exceeded, no data is returned.
                   
                   Returns:
                       pd.DataFrame: Price data for the requested interval, or an empty DataFrame
                           when data is unavailable or exceeds the permitted staleness.
                   """
        df = self._load_prices(ticker, start, end)
        if max_stale_tdays is None or df.empty:
            return df
        last_bar = df.index.max()
        age_tdays = int(np.busday_count(last_bar.date().isoformat(),
                                        pd.Timestamp(end).date().isoformat()))
        if age_tdays > max_stale_tdays:
            log.warning(
                "get_prices: %s last bar %s is %d trading days behind requested "
                "end %s (limit %d) — refusing stale data for a current-price "
                "context; returning empty.",
                ticker, last_bar.date(), age_tdays,
                pd.Timestamp(end).date(), max_stale_tdays,
            )
            return pd.DataFrame()
        return df

    def _load_prices(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        """
        Load price data for a ticker over the requested date range.
        
        Cached data is reused when available; otherwise, covering cached data or a
        live fetch is used. If fetching fails, available cached data is returned.
        
        Parameters:
            ticker (str): Ticker symbol.
            start (str): Inclusive start date.
            end (str): Exclusive end date.
        
        Returns:
            pd.DataFrame: Price data before the exclusive end date, or an empty
                DataFrame when no data is available.
        """
        path     = self._cache_path(ticker, start)
        start_ts = pd.Timestamp(start)
        end_ts   = pd.Timestamp(end)

        cached: pd.DataFrame | None = None
        if path.exists():
            try:
                with open(path, "rb") as f:
                    cached = pickle.load(f)
            except _CACHE_LOAD_ERRORS as exc:
                log.warning("Failed to load exact-key cache file %s: %s", path, exc)
                cached = None

        # Reuse the cache when it already extends to (or past) the requested
        # end. A few days of slack absorbs weekends/holidays so a daily run
        # does not re-download every time `end` advances. yfinance treats
        # `end` as exclusive — mirror that by slicing on `< end`.
        if (cached is not None and not cached.empty
                and cached.index.max() >= end_ts - pd.Timedelta(days=1)):
            return cached[cached.index < end_ts]

        # Exact key missed (or was too short) — try any cached file for this
        # ticker with broad-enough coverage before treating it as a real miss.
        fallback = self._find_covering_cache(ticker, start_ts, end_ts)
        if fallback is not None:
            # Persist the fallback to the exact-key cache path so future identical
            # requests hit the fast path instead of re-scanning all candidate files.
            try:
                with open(path, "wb") as f:
                    pickle.dump(fallback, f)
            except OSError as exc:
                log.warning("Failed to persist fallback cache to %s: %s", path, exc)
            return fallback[fallback.index < end_ts]

        try:
            # EODHD (split+dividend adjusted, Close == adjusted_close) replaces
            # yfinance: it works through the proxy and serves delisted tickers.
            # fetch_eod already logs and returns an empty frame on any failure.
            df = fetch_eod(ticker, start, end, adjust=True)
            if df is None or df.empty:
                # Fall back to stale cache rather than losing data on a failed
                # fetch — but say so: this branch silently served 7-week-old
                # closes as current during the 2026 outage.
                if cached is not None and not cached.empty:
                    log.warning(
                        "get_prices: fetch for %s returned nothing — serving "
                        "cached data ending %s (requested end %s).",
                        ticker, cached.index.max().date(), end_ts.date(),
                    )
                    return cached[cached.index < end_ts]
                return pd.DataFrame()
            # Cache the full downloaded range; callers get the end-exclusive slice.
            with open(path, "wb") as f:
                pickle.dump(df, f)
            return df[df.index < end_ts]
        except Exception as exc:
            # Fall back to stale cache rather than losing data on a failed fetch
            # — but say so (see above).
            if cached is not None and not cached.empty:
                log.warning(
                    "get_prices: fetch for %s raised (%s) — serving cached data "
                    "ending %s (requested end %s).",
                    ticker, str(exc)[:120], cached.index.max().date(), end_ts.date(),
                )
                return cached[cached.index < end_ts]
            return pd.DataFrame()

    def get_return(self, ticker: str, start: str, end: str) -> float | None:
        df = self.get_prices(ticker, start, end)
        if df.empty or len(df) < 2:
            return None
        try:
            start_price = float(df["Close"].iloc[0])
            end_price = float(df["Close"].iloc[-1])
            if start_price == 0:
                return None
            return (end_price - start_price) / start_price
        except Exception:
            return None
