#!/usr/bin/env python3
"""Build the COMPLETE prebuilt Simulator cache for a given window (default 2018-2024).

The first cut (PR #29) shipped a pit_market_cap grid that was only partly
populated (2022-2024 nearly empty) because prefetch_pit_market_caps skips
entries that already exist — including stale nulls from older partial runs —
so it never completed. This script:

  1. fills missing RAW prices (delisted tickers) via EODHD (adjust=False),
  2. fills missing EDGAR company-facts via SEC (get_shares_outstanding),
  3. FORCE-recomputes the whole market-cap grid (all members x every month),
  4. derives the true Top-100 union and rebuilds data/seed_cache/ (grid +
     adjusted prices + slim EDGAR) for that union,
  5. reports the resulting shipped-cache size.

Build-time only; needs EODHD_API_KEY. Run:
    EODHD_API_KEY=... python scripts/build_full_cache.py
    EODHD_API_KEY=... python scripts/build_full_cache.py --start 2010-01-01 --end 2026-06-30

Window note: EDGAR has no reliable shares-outstanding data before ~2009 (the
XBRL mandate rolled out through 2009-2011), so months before ~2010 may rank
fewer than TOP_N companies — this is a genuine data-availability limit, not a
bug; the manifest's months_full_rank reports the actual coverage.
"""
from __future__ import annotations

import argparse
import json
import pickle
import shutil
import sys
import time
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from core.data.eodhd import fetch_eod  # noqa: E402
from core.data.prices import PriceData  # noqa: E402
import data.sp500_universe as u  # noqa: E402
from scripts.build_prebuilt_cache import _slim_edgar, _dir_mb  # noqa: E402

_DEFAULT_SIM_START, _DEFAULT_SIM_END = "2018-01-01", "2024-12-31"
TOP_N = 100
_CACHE = REPO / "data" / "cache"
_SEED = REPO / "data" / "seed_cache"
_RAW = _CACHE / "prices_raw"

# Set by main() from CLI args; module-level so the phase functions (which
# mirror the original single-window script) don't need a config object threaded
# through every call.
SIM_START = _DEFAULT_SIM_START
SIM_END = _DEFAULT_SIM_END
WARMUP_START = _DEFAULT_SIM_START  # recomputed in main() as SIM_START - 400 days


def _fdates() -> list[str]:
    spy = PriceData().get_prices("SPY", WARMUP_START, SIM_END)
    fmonths: dict[tuple, pd.Timestamp] = {}
    for ts in spy.index:
        if pd.Timestamp(SIM_START) <= ts <= pd.Timestamp(SIM_END):
            fmonths.setdefault((ts.year, ts.month), ts)
    return [ts.date().isoformat() for ts in fmonths.values()]


def _fill_raw_prices(pool: list[str]) -> None:
    """Phase 1: Fill missing RAW prices via EODHD (covers delisted)."""
    _RAW.mkdir(parents=True, exist_ok=True)
    missing_raw = [t for t in pool if not list(_RAW.glob(f"{t}_*.pkl"))]
    print(f"Fetching {len(missing_raw)} missing raw-price series via EODHD…")
    got = 0
    for t in missing_raw:
        try:
            df = fetch_eod(t, WARMUP_START, SIM_END, adjust=False)
            if df is not None and not df.empty:
                with open(_RAW / f"{t}_{WARMUP_START}.pkl", "wb") as f:
                    pickle.dump(df, f)
                got += 1
        except Exception as e:
            print(f"  raw {t}: {e!r}"[:100])
        time.sleep(0.1)
    print(f"  raw filled: {got}/{len(missing_raw)}")


def _fill_edgar_facts(pool: list[str]) -> None:
    """Phase 2: Fill missing EDGAR facts (get_shares_outstanding fetches + caches)."""
    edgar_dir = _CACHE / "edgar"
    missing_edgar = [t for t in pool if not (edgar_dir / f"{t}.json").exists()]
    print(f"Fetching {len(missing_edgar)} missing EDGAR company-facts via SEC…")
    ge = 0
    for t in missing_edgar:
        try:
            u._get_edgar().get_shares_outstanding(t, "2020-06-30")
            if (edgar_dir / f"{t}.json").exists():
                ge += 1
        except Exception:
            pass
        time.sleep(0.05)
    print(f"  edgar filled: {ge}/{len(missing_edgar)}")


def _force_recompute_grid(pool: list[str], fdates: list[str]) -> tuple[int, int]:
    """Phase 3: FORCE-recompute the whole grid (delete first so nothing is skipped).

    Loops TICKER-outer (not u.prefetch_pit_market_caps's date-outer order) and
    evicts each ticker's cached EDGAR company-facts JSON from memory once done
    with it. EdgarFundamentals._facts_mem is a plain in-process dict that never
    evicts, so date-outer order (which touches every ticker within the first
    date) accumulates ALL ~840 companies' full facts JSON — several MB each —
    simultaneously, several GB total. Ticker-outer + per-ticker eviction bounds
    memory to ~1 company's facts at a time; the on-disk facts cache (unaffected)
    still makes every call after the first EODHD/SEC fetch instant. Grid is
    checkpointed to disk every 50 tickers so a crash doesn't lose all progress.
    """
    grid_file = _CACHE / "pit_market_cap" / "pit_market_caps.json"
    if grid_file.exists():
        grid_file.unlink()
    u._pit_cache = None
    u._raw_frames.clear()
    u._shares_memo.clear()
    print("Force-recomputing full market-cap grid (ticker-outer, memory-bounded)…")
    cache = u._load_pit_cache()
    now = time.time()
    edgar = u._get_edgar()
    for i, t in enumerate(pool):
        for d in fdates:
            cache[f"{t}|{d}"] = {"mcap": u._compute_pit_mcap(t, d), "ts": now}
        edgar._facts_mem.pop(t, None)  # bound memory: drop this ticker's full facts JSON
        if (i + 1) % 50 == 0:
            u._save_pit_cache()
            print(f"  grid checkpoint: {i + 1}/{len(pool)} tickers")
    u._save_pit_cache()
    grid = json.loads(grid_file.read_text())
    nonnull = sum(1 for v in grid.values() if v.get("mcap"))
    from collections import defaultdict
    per_date = defaultdict(int)
    for k, v in grid.items():
        if v.get("mcap"):
            per_date[k.rsplit("|", 1)[1]] += 1
    rich = sum(1 for dt in fdates if per_date.get(dt, 0) >= TOP_N)
    print(f"  grid: {nonnull}/{len(grid)} non-null; months with >={TOP_N} rankable: {rich}/{len(fdates)}")
    return nonnull, rich


def _rebuild_seed_cache(fdates: list[str], nonnull: int, rich: int, pool: list[str]) -> None:
    """Phase 4: True Top-100 union + rebuild seed_cache."""
    top_union: set[str] = set()
    for d in fdates:
        top_union |= set(u.get_universe_top_n(d, TOP_N))
    top_union = sorted(top_union)
    print(f"True Top-{TOP_N} union: {len(top_union)} tickers")

    grid_file = _CACHE / "pit_market_cap" / "pit_market_caps.json"
    if _SEED.exists():
        shutil.rmtree(_SEED)
    for sub in ("pit_market_cap", "prices", "edgar", "sp500_universe", "fred"):
        (_SEED / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy2(grid_file, _SEED / "pit_market_cap" / "pit_market_caps.json")

    prices = PriceData()
    npx = 0
    for t in sorted(set(top_union) | {"SPY"}):
        prices.get_prices(t, WARMUP_START, SIM_END)
        src = _CACHE / "prices" / f"{t}_{WARMUP_START}.pkl"
        if src.exists():
            shutil.copy2(src, _SEED / "prices" / src.name); npx += 1

    edgar_dir = _CACHE / "edgar"
    ned = 0
    for t in top_union:
        src = edgar_dir / f"{t}.json"
        if src.exists():
            slim = _slim_edgar(src)
            if slim and slim["facts"]:
                (_SEED / "edgar" / f"{t}.json").write_text(json.dumps(slim)); ned += 1
    csv_src = _CACHE / "sp500_universe" / "sp500_historical_components.csv"
    if csv_src.exists():
        shutil.copy2(csv_src, _SEED / "sp500_universe" / csv_src.name)
    for f in (_CACHE / "fred").glob("*"):
        shutil.copy2(f, _SEED / "fred" / f.name)

    _report_sizes_and_manifest(fdates, pool, top_union, nonnull, rich, npx, ned)


def _report_sizes_and_manifest(fdates: list[str], pool: list[str], top_union: list[str],
                                 nonnull: int, rich: int, npx: int, ned: int) -> None:
    """Phase 5: Report the resulting shipped-cache size and write manifest."""
    sizes = {s.name: round(_dir_mb(s), 2) for s in sorted(_SEED.iterdir()) if s.is_dir()}
    manifest = {"window": {"warmup_start": WARMUP_START, "sim_start": SIM_START, "sim_end": SIM_END},
                "top_n": TOP_N, "months": len(fdates), "pool": len(pool),
                "top_union": len(top_union), "grid_nonnull": nonnull,
                "months_full_rank": rich, "prices": npx, "edgar": ned, "sizes_mb": sizes}
    (_SEED / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("\nSeed cache sizes (MB):")
    for k, v in sizes.items():
        print(f"  {k:16} {v:8.2f}")
    print(f"  {'TOTAL':16} {round(_dir_mb(_SEED), 2):8.2f}")
    print("FULL_BUILD_DONE")


def main() -> None:
    global SIM_START, SIM_END, WARMUP_START

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default=_DEFAULT_SIM_START, help="Simulator window start (YYYY-MM-DD)")
    ap.add_argument("--end", default=_DEFAULT_SIM_END, help="Simulator window end (YYYY-MM-DD)")
    args = ap.parse_args()
    SIM_START, SIM_END = args.start, args.end
    # PriceData._cache_path keys the price pkl by an EXACT string match on the
    # requested `start` date — no glob, no nearest-date fallback (unlike the raw
    # price / EDGAR lookups elsewhere in this pipeline, which do glob). So this
    # MUST compute byte-for-byte the same date product/backtest/engine.py's
    # _load_backtest_data uses at request time, or every shipped price file is
    # invisible to the runtime and silently live-refetched on every request —
    # exactly the bug this comment is here to prevent regressing (discovered when
    # the 2010-2026 cache's 400-day placeholder didn't match the runtime's
    # 365-day-capped-at-2016-01-01 formula: 2008-11-27 vs 2009-01-01, a 100%
    # cache-miss for the price cache specifically — the grid/ranking still worked
    # because that lookup path glob-matches, not exact-matches).
    _ENGINE_WARMUP_FLOOR = date(2016, 1, 1)   # product/backtest/engine.py _WARMUP_START
    _ENGINE_WARMUP_DAYS = 365                  # product/backtest/engine.py's start_date offset
    WARMUP_START = min(_ENGINE_WARMUP_FLOOR,
                       date.fromisoformat(SIM_START) - timedelta(days=_ENGINE_WARMUP_DAYS)).isoformat()

    print(f"Window: {SIM_START} .. {SIM_END}  (warmup from {WARMUP_START})")
    fdates = _fdates()
    pool = sorted({t for d in fdates for t in u.get_universe(d)})
    print(f"Ranking pool: {len(pool)} tickers x {len(fdates)} months")

    _fill_raw_prices(pool)
    _fill_edgar_facts(pool)
    nonnull, rich = _force_recompute_grid(pool, fdates)
    _rebuild_seed_cache(fdates, nonnull, rich, pool)


if __name__ == "__main__":
    main()
