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

# Override the default [start, end] range from the command line:
#   python scripts/_fetch_price_cache.py 2008-01-01 2024-12-31
if len(sys.argv) >= 3:
    _START, _END = sys.argv[1], sys.argv[2]


def _unix(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())


def fetch_chart(ticker: str, start: str, end: str, adjust: bool = True) -> pd.DataFrame:
    """Fetch daily OHLCV from Yahoo.

    adjust=True  → split+dividend adjusted (matches yfinance auto_adjust); this is
                   what the backtest uses.
    adjust=False → RAW (unadjusted) OHLC. Required for point-in-time market cap:
                   raw price × raw EDGAR shares. Adjusted prices would deflate any
                   future-splitter (e.g. NVDA 40:1, AMZN 20:1) and corrupt the
                   cross-sectional size ranking.
    """
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
    if adjust:
        # Apply auto_adjust: scale OHLC by adjclose/close, then Close := adjclose.
        factor = df["AdjClose"] / df["Close"]
        for col in ("Open", "High", "Low"):
            df[col] = df[col] * factor
        df["Close"] = df["AdjClose"]
    else:
        # Yahoo's quote.close is already split-adjusted (not dividend-adjusted).
        # Undo the split adjustment to recover the TRUE unadjusted price: each row
        # is multiplied by the product of split ratios that occurred AFTER it.
        splits = (res.get("events", {}) or {}).get("splits", {}) or {}
        split_factor = pd.Series(1.0, index=df.index)
        for ev in splits.values():
            num, den = ev.get("numerator"), ev.get("denominator")
            ev_ts = ev.get("date")
            if not (num and den and ev_ts):
                continue
            split_day = pd.Timestamp(int(ev_ts), unit="s").normalize()
            split_factor[df.index < split_day] *= float(num) / float(den)
        for col in ("Open", "High", "Low", "Close"):
            df[col] = df[col] * split_factor
    df = df.drop(columns=["AdjClose"])
    df.index.name = "Date"
    # Trim to [start, end) to mirror yfinance's end-exclusive behaviour.
    df = df[(df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end) + pd.Timedelta(days=1))]
    return df


def _load_ticker_list() -> list[str]:
    """Default universe + SPY, or a custom list via `--tickers <file>`.

    The file is one ticker per line (blank lines ignored). SPY is always
    appended so the trading calendar / benchmark is available.
    """
    if "--tickers" in sys.argv:
        path = Path(sys.argv[sys.argv.index("--tickers") + 1])
        names = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
        if "SPY" not in names:
            names.append("SPY")
        return names
    return list(VALIDATION_UNIVERSE) + ["SPY"]


def main() -> None:
    # --raw: store UNADJUSTED prices under data/cache/prices_raw (for point-in-time
    # market cap). Default: split/div-adjusted prices for the backtest.
    raw = "--raw" in sys.argv
    cache_dir = (_CACHE_DIR.parent / "prices_raw") if raw else _CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    tickers = _load_ticker_list()
    ok, fail = 0, 0
    for t in tickers:
        # Cache filename must match PriceData._cache_path: keyed by ticker+start
        # only (end is excluded; the reader slices to the requested end).
        path = cache_dir / f"{t}_{_START}.pkl"
        if path.exists():
            ok += 1
            continue
        try:
            df = fetch_chart(t, _START, _END, adjust=not raw)
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
    print(f"\nDone. cached={ok} failed={fail} -> {cache_dir}")


if __name__ == "__main__":
    main()
