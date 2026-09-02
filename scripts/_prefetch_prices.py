#!/usr/bin/env python3
"""Prefetch price history into PriceData's cache using a requests-session
backend (the default curl_cffi backend cannot negotiate TLS through the
agent proxy). Cache files are written in the exact format PriceData expects
so the rest of the pipeline runs offline."""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

import requests
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.tickers import VALIDATION_UNIVERSE
from core.data.prices import _DEFAULT_CACHE, _safe_ticker

START = "2016-01-01"
END = "2024-12-31"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    return s


def fetch_one(session: requests.Session, ticker: str) -> bool:
    path = _DEFAULT_CACHE / f"{_safe_ticker(ticker)}_{START}_{END}.pkl"
    if path.exists():
        return True
    for attempt in range(6):
        try:
            t = yf.Ticker(ticker, session=session)
            df = t.history(start=START, end=END, auto_adjust=True)
            if df is not None and not df.empty:
                # Normalise to PriceData/yf.download shape: tz-naive index, OHLCV.
                df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                df.index = df.index.tz_localize(None)
                _DEFAULT_CACHE.mkdir(parents=True, exist_ok=True)
                with open(path, "wb") as f:
                    pickle.dump(df, f)
                print(f"  {ticker:<6} OK  {len(df)} rows  "
                      f"{df.index[0].date()}..{df.index[-1].date()}")
                return True
            print(f"  {ticker:<6} empty (attempt {attempt})")
        except Exception as e:  # noqa: BLE001
            print(f"  {ticker:<6} {type(e).__name__}: {str(e)[:70]} (attempt {attempt})")
        time.sleep(5 * (attempt + 1))
    return False


def main() -> None:
    session = make_session()
    tickers = list(VALIDATION_UNIVERSE) + ["SPY"]
    print(f"Prefetching {len(tickers)} tickers {START}..{END}")
    failed = []
    for tk in tickers:
        ok = fetch_one(session, tk)
        if not ok:
            failed.append(tk)
        time.sleep(1.0)
    print(f"\nDone. {len(tickers) - len(failed)}/{len(tickers)} cached.")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
