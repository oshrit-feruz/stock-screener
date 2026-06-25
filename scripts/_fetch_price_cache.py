#!/usr/bin/env python3
"""Populate the PriceData pkl cache via Yahoo's chart API using plain `requests`.

The vendored yfinance (1.4.1) uses curl_cffi/BoringSSL, which cannot complete a
TLS handshake through the agent proxy. This helper fetches the same data with
`requests` (which honours REQUESTS_CA_BUNDLE) and writes pickles in exactly the
layout PriceData.get_prices() expects, so the rest of the pipeline runs unchanged.

Output matches yfinance(auto_adjust=True): OHLC adjusted by the split/dividend
factor, Close == adjusted close.
"""
from __future__ import annotations

import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.tickers import VALIDATION_UNIVERSE  # noqa: E402

_CACHE_DIR = Path(__file__).parent.parent / "data" / "cache" / "prices"
_START = "2016-01-01"
_END = "2024-12-31"
_HEADERS = {"User-Agent": "Mozilla/5.0 (research price fetch)"}


def _unix(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())


def fetch_chart(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Fetch daily OHLCV from Yahoo, auto-adjusted to match yfinance."""
    # end is exclusive in yfinance download; pad +2 days to be safe, we trim later.
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?period1={_unix(start)}&period2={_unix(end) + 86400}"
        f"&interval=1d&events=div%2Csplit&includeAdjustedClose=true"
    )
    r = requests.get(url, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    adj = res["indicators"]["adjclose"][0]["adjclose"]

    idx = pd.to_datetime(ts, unit="s", utc=True).tz_convert(None).normalize()
    df = pd.DataFrame(
        {
            "Open": q["open"],
            "High": q["high"],
            "Low": q["low"],
            "Close": q["close"],
            "Volume": q["volume"],
            "AdjClose": adj,
        },
        index=idx,
    )
    df = df.dropna(subset=["Close", "AdjClose"])
    # Apply auto_adjust: scale OHLC by adjclose/close, then Close := adjclose.
    factor = df["AdjClose"] / df["Close"]
    for col in ("Open", "High", "Low"):
        df[col] = df[col] * factor
    df["Close"] = df["AdjClose"]
    df = df.drop(columns=["AdjClose"])
    df.index.name = "Date"
    # Trim to [start, end) to mirror yfinance's end-exclusive behaviour.
    df = df[(df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end) + pd.Timedelta(days=1))]
    return df


def main() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tickers = list(VALIDATION_UNIVERSE) + ["SPY"]
    ok, fail = 0, 0
    for t in tickers:
        path = _CACHE_DIR / f"{t}_{_START}_{_END}.pkl"
        if path.exists():
            ok += 1
            continue
        try:
            df = fetch_chart(t, _START, _END)
            if df.empty or len(df) < 252:
                print(f"  SKIP {t}: only {len(df)} rows")
                fail += 1
                continue
            with open(path, "wb") as f:
                pickle.dump(df, f)
            ok += 1
            print(f"  OK   {t}: {len(df)} rows  {df.index[0].date()}..{df.index[-1].date()}")
        except Exception as e:
            fail += 1
            print(f"  FAIL {t}: {repr(e)[:120]}")
        time.sleep(0.3)
    print(f"\nDone. cached={ok} failed={fail} -> {_CACHE_DIR}")


if __name__ == "__main__":
    main()
