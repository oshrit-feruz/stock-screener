#!/usr/bin/env python3
"""One-off: prefetch OHLCV into PriceData's cache via a TLS-trusting requests session.

yfinance 1.4.1 defaults to curl_cffi (BoringSSL) which ignores the agent proxy
CA bundle; we force a plain requests.Session pointed at the bundle and write to
the exact cache paths PriceData.get_prices() reads.
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

import requests
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.tickers import VALIDATION_UNIVERSE  # noqa: E402

CA = "/root/.ccr/ca-bundle.crt"
START, END = "2016-01-01", "2024-12-31"
CACHE = Path(__file__).parent.parent / "data" / "cache" / "prices"
CACHE.mkdir(parents=True, exist_ok=True)


def session() -> requests.Session:
    s = requests.Session()
    s.verify = CA
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def fetch(ticker: str, s: requests.Session, retries: int = 5):
    path = CACHE / f"{ticker}_{START}_{END}.pkl"
    if path.exists():
        return "cached"
    for attempt in range(retries):
        try:
            df = yf.download(ticker, start=START, end=END, auto_adjust=True,
                             progress=False, session=s)
            if df is not None and not df.empty:
                import pandas as pd
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                with open(path, "wb") as f:
                    pickle.dump(df, f)
                return f"ok ({len(df)})"
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
            continue
        time.sleep(5 * (attempt + 1))
    return f"FAIL ({last if 'last' in dir() else 'empty'})"


def main():
    s = session()
    tickers = ["SPY"] + list(VALIDATION_UNIVERSE)
    for i, t in enumerate(tickers):
        res = fetch(t, s)
        print(f"[{i+1}/{len(tickers)}] {t}: {res}", flush=True)
        if not res.startswith("cached"):
            time.sleep(2)


if __name__ == "__main__":
    main()
