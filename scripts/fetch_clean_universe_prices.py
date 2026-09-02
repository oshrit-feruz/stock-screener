#!/usr/bin/env python3
"""Stage A of the clean point-in-time backtest: fetch prices for every ticker
that was ever an S&P 500 member across the window, from EODHD (delisted names
included). Adjusted prices drive the signal and trade returns; RAW prices drive
the dollar-volume size ranking (split-adjusted volume would distort splitters).

Resumable: a ticker already present in both caches is skipped, so a container
recycle mid-run just continues. Failures (unknown EODHD symbol) are logged and
written to a skip-list; they are simply absent from the universe.
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(str(ROOT / ".env"))

from core.data.eodhd import fetch_eod, normalize_ticker  # noqa: E402
from data.sp500_universe import get_universe  # noqa: E402

START = "1998-06-01"     # warmup so 252d rolling windows are valid from 2000
END = "2024-12-31"
_ADJ_DIR = ROOT / "data" / "cache" / "prices"
_RAW_DIR = ROOT / "data" / "cache" / "prices_raw"
_FAIL_FILE = ROOT / "data" / "cache" / "clean_universe_fetch_failures.txt"


def _safe(t: str) -> str:
    return "".join(c for c in t if c.isalnum() or c in "-_")


def union_members() -> list[str]:
    dates = [f"{y}-{m:02d}-15" for y in range(1999, 2025) for m in (3, 6, 9, 12)]
    u: set[str] = set()
    for d in dates:
        try:
            u |= set(get_universe(d))
        except Exception:
            pass
    return sorted(u)


def _cache_ok(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
        return df is not None and not df.empty
    except Exception:
        return False


def main() -> None:
    _ADJ_DIR.mkdir(parents=True, exist_ok=True)
    _RAW_DIR.mkdir(parents=True, exist_ok=True)
    tickers = union_members()
    print(f"Universe: {len(tickers)} ever-members {START}..{END}", flush=True)

    failures: list[str] = []
    done = 0
    for i, t in enumerate(tickers, 1):
        adj_path = _ADJ_DIR / f"{_safe(t)}_{START}_{END}.pkl"
        raw_path = _RAW_DIR / f"{_safe(t)}_{START}_{END}.pkl"
        need_adj = not _cache_ok(adj_path)
        need_raw = not _cache_ok(raw_path)
        if not need_adj and not need_raw:
            done += 1
            continue

        ok_any = False
        if need_adj:
            df = fetch_eod(t, START, END, adjust=True)
            if df is not None and not df.empty:
                with open(adj_path, "wb") as f:
                    pickle.dump(df, f)
                ok_any = True
        if need_raw:
            dr = fetch_eod(t, START, END, adjust=False)
            if dr is not None and not dr.empty:
                with open(raw_path, "wb") as f:
                    pickle.dump(dr, f)
                ok_any = True

        if ok_any:
            done += 1
        else:
            failures.append(t)
        if i % 50 == 0:
            print(f"  {i}/{len(tickers)}  ok={done}  fail={len(failures)}  "
                  f"(last {normalize_ticker(t)})", flush=True)
        time.sleep(0.05)

    _FAIL_FILE.write_text("\n".join(failures) + ("\n" if failures else ""))
    print(f"\nDONE. cached={done}/{len(tickers)}  failed={len(failures)}", flush=True)
    print(f"failures -> {_FAIL_FILE}", flush=True)
    if failures:
        print("sample failures:", failures[:25], flush=True)


if __name__ == "__main__":
    main()
