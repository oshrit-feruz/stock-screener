from __future__ import annotations

import logging
import pickle
from pathlib import Path

import pandas as pd

from core.data.eodhd import fetch_eod

log = logging.getLogger(__name__)

_DEFAULT_CACHE = Path(__file__).parent.parent.parent / "data" / "cache" / "prices"


def _safe_ticker(ticker: str) -> str:
    """Strip path separators and dots to prevent cache path traversal."""
    return "".join(c for c in ticker if c.isalnum() or c in "-_")


def _key_start_ts(path: Path) -> pd.Timestamp | None:
    """The `start` date encoded in a cache filename ({ticker}_{start}.pkl), or
    None if it doesn't parse. rsplit tolerates underscores in the ticker part."""
    try:
        return pd.Timestamp(path.stem.rsplit("_", 1)[1])
    except Exception:
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
        """Fall back to ANY cached file for this ticker that covers
        [start_ts, end_ts), not just the one keyed by this exact call's
        `start` string.

        Prices for a ticker+date don't depend on which `start` a previous
        call used when it fetched and cached them — but `_cache_path`'s exact
        string match treats them as unrelated files. A prebuilt cache seeded
        at deploy time is written with one `start` (e.g. a build script's
        warmup floor for its whole window); a live request computes its own
        `start` per its own start_date (e.g. product/backtest/engine.py's
        per-request warmup formula). Those two strings only coincide for the
        exact date that produced the prebuilt cache's warmup — any other
        request silently misses the entire prebuilt cache and re-fetches the
        whole universe live. This mirrors the glob-based lookup already used
        for raw prices/EDGAR facts elsewhere in this codebase.
        """
        best = None
        for p in sorted(self.cache_dir.glob(f"{_safe_ticker(ticker)}_*.pkl")):
            try:
                with open(p, "rb") as f:
                    df = pickle.load(f)
            except (pickle.UnpicklingError, EOFError, OSError, ValueError, AttributeError) as exc:
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

    def get_prices(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        path     = self._cache_path(ticker, start)
        start_ts = pd.Timestamp(start)
        end_ts   = pd.Timestamp(end)

        cached: pd.DataFrame | None = None
        if path.exists():
            try:
                with open(path, "rb") as f:
                    cached = pickle.load(f)
            except (pickle.UnpicklingError, EOFError, OSError, ValueError, AttributeError) as exc:
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
                # Fall back to stale cache rather than losing data on a failed fetch.
                if cached is not None and not cached.empty:
                    return cached[cached.index < end_ts]
                return pd.DataFrame()
            # Cache the full downloaded range; callers get the end-exclusive slice.
            with open(path, "wb") as f:
                pickle.dump(df, f)
            return df[df.index < end_ts]
        except Exception:
            # Fall back to stale cache rather than losing data on a failed fetch.
            if cached is not None and not cached.empty:
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
