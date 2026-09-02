#!/usr/bin/env python3
"""Build the point-in-time dollar-volume grid and ship it in data/seed_cache/.

get_universe_top_n now ranks by dollar-volume (survivorship-free). At runtime it
reads a precomputed grid (data/cache/pit_dollar_volume/pit_dollar_volumes.json),
exactly as the old market-cap ranking read its grid — because the deployed cache
ships NO raw prices, only the precomputed grid. This script builds that grid from
the local raw-price cache and copies it into the committed seed so a cold deploy
has the correct universe (it lands alongside the release asset, which lacks it).

Memory-frugal: one ticker's raw frame is resident at a time (ticker-outer), so it
does not OOM on the ~900-member pool. Historical entries omit the cache TTL
timestamp — _pit_entry_valid treats any date older than ~120 days as immutable,
so a missing `ts` still reads back as valid.

    python scripts/build_dollar_volume_seed.py            # 2010-01..2024-12
    python scripts/build_dollar_volume_seed.py 2015-01-01 2024-12-31
"""
from __future__ import annotations

import json
import math
import pickle
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(ROOT / ".env"))

from core.data.eodhd import fetch_eod  # noqa: E402
from data.sp500_universe import _DV_WINDOW, get_universe  # noqa: E402

_RAW_DIR = ROOT / "data" / "cache" / "prices_raw"
_OUT_CACHE = ROOT / "data" / "cache" / "pit_dollar_volume" / "pit_dollar_volumes.json"
_OUT_SEED = ROOT / "data" / "seed_cache" / "pit_dollar_volume" / "pit_dollar_volumes.json"

START = sys.argv[1] if len(sys.argv) > 1 else "2010-01-01"
END = sys.argv[2] if len(sys.argv) > 2 else "2024-12-31"


def _safe(t: str) -> str:
    return "".join(c for c in t if c.isalnum() or c in "-_")


def _spy_calendar() -> pd.DatetimeIndex:
    """SPY trading days in [START, END]. Fetched fresh from EODHD so the calendar
    always covers the requested window (a cached SPY pkl may be narrower)."""
    spy = fetch_eod("SPY.US", START, END, adjust=True)
    if spy is None or spy.empty:
        raise SystemExit("Could not fetch SPY calendar from EODHD (check EODHD_API_KEY).")
    idx = spy.index
    return idx[(idx >= pd.Timestamp(START)) & (idx <= pd.Timestamp(END))]


def _monthly_first_trading_days(cal: pd.DatetimeIndex) -> list[pd.Timestamp]:
    seen: dict[tuple, pd.Timestamp] = {}
    for ts in cal:
        seen.setdefault((ts.year, ts.month), ts)
    return sorted(seen.values())


def main() -> None:
    cal = _spy_calendar()
    fdates = _monthly_first_trading_days(cal)
    date_strs = [d.date().isoformat() for d in fdates]
    print(f"Rebuild dates: {len(fdates)} months ({date_strs[0]}..{date_strs[-1]})")

    pool = sorted({t for d in date_strs for t in get_universe(d)})
    print(f"Membership union: {len(pool)} tickers")

    grid: dict[str, dict] = {}
    date_ts = [(s, pd.Timestamp(s)) for s in date_strs]
    done = 0
    for i, t in enumerate(pool, 1):
        p = _RAW_DIR / f"{_safe(t)}_1998-06-01_2024-12-31.pkl"
        if not p.exists():
            matches = sorted(_RAW_DIR.glob(f"{_safe(t)}_*.pkl"))
            p = matches[0] if matches else None
        if p is None or not p.exists():
            continue
        try:
            with open(p, "rb") as f:
                raw = pickle.load(f)
        except Exception:
            continue
        if raw is None or raw.empty or "Volume" not in raw.columns:
            continue
        dvol = (raw["Close"].astype(float) * raw["Volume"].astype(float))
        med = dvol.rolling(_DV_WINDOW).median()
        for s, ts in date_ts:
            sub = med[med.index <= ts]
            if sub.empty:
                continue
            v = float(sub.iloc[-1])
            if math.isfinite(v) and v > 0:
                grid[f"{t}|{s}"] = {"dv": int(v)}
        done += 1
        if i % 100 == 0:
            print(f"  {i}/{len(pool)} tickers  ({len(grid)} grid entries)")

    _OUT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _OUT_CACHE.write_text(json.dumps(grid))
    _OUT_SEED.parent.mkdir(parents=True, exist_ok=True)
    _OUT_SEED.write_text(json.dumps(grid))
    mb = _OUT_SEED.stat().st_size / 1e6
    print(f"\nDONE. {len(grid)} entries from {done} tickers -> {mb:.1f} MB")
    print(f"  cache: {_OUT_CACHE}")
    print(f"  seed:  {_OUT_SEED}")


if __name__ == "__main__":
    main()
