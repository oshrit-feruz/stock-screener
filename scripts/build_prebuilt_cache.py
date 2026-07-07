#!/usr/bin/env python3
"""Build the committed prebuilt cache that lets the live Simulator run fast on a
cold deploy (Render) over the validated 2018-2024 window.

The Simulator's expensive cold-start work is (a) ranking the point-in-time
Top-100 universe — which needs a raw-price cache + EDGAR shares — and (b) the
per-ticker EDGAR quality gate, whose full company-facts JSON is ~4-5 MB each.
Shipping the whole thing is impractical (raw prices 144 MB, EDGAR 2.3 GB).

This script distils exactly what the Simulator touches into a small, committed
`data/seed_cache/` tree (~tens of MB):

  seed_cache/pit_market_cap/  full precomputed market-cap grid for the S&P
                              membership union x monthly rebuild dates. Because
                              historical caps are immutable (_pit_entry_valid),
                              the runtime reuses these directly and NEVER needs
                              the raw-price / EDGAR-shares caches to RANK — so the
                              true Top-100 universe builds with no fallback.
  seed_cache/prices/          adjusted OHLCV pkls for the Top-100 union + SPY.
  seed_cache/edgar/           SLIM company-facts (only the ~6 concepts the quality
                              gate reads) for the Top-100 union — ~180 KB/ticker
                              vs ~4.8 MB full, and a strict structural subset so
                              the existing reader is unchanged.
  seed_cache/sp500_universe/  the historical-components CSV (membership).
  seed_cache/fred/            the Fed Funds series (idle-cash yield).

`scripts/seed_cache.py` copies this into `data/cache/` on a cold boot.

Run (build-time only; needs the warm local raw-price + EDGAR caches):
    EODHD_API_KEY=... python scripts/build_prebuilt_cache.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

import data.sp500_universe as u  # noqa: E402
from core.data.edgar import (  # noqa: E402
    _EQUITY_CONCEPTS,
    _LT_DEBT_CONCEPTS,
    _NET_INCOME_CONCEPTS,
    _REVENUE_CONCEPTS,
    _SHARES_OUTSTANDING_CONCEPTS,
)
from core.data.prices import PriceData  # noqa: E402

# ── Window: the validated Simulator range (2016 warmup, months 2018-2024). ──
WARMUP_START = "2016-01-01"
SIM_START    = "2018-01-01"
SIM_END      = "2024-12-31"
TOP_N        = 100

_CACHE   = REPO / "data" / "cache"
_SEED    = REPO / "data" / "seed_cache"

# EDGAR concepts the quality gate + market-cap shares actually read.
_GAAP_KEEP = (set(_REVENUE_CONCEPTS) | set(_NET_INCOME_CONCEPTS)
              | set(_EQUITY_CONCEPTS) | set(_LT_DEBT_CONCEPTS)
              | {c for tax, c in _SHARES_OUTSTANDING_CONCEPTS if tax == "us-gaap"})
_DEI_KEEP = {c for tax, c in _SHARES_OUTSTANDING_CONCEPTS if tax == "dei"}


def _monthly_rebuild_dates() -> list[str]:
    """First trading day of each month in [SIM_START, SIM_END], via SPY."""
    prices = PriceData()
    spy = prices.get_prices("SPY", WARMUP_START, SIM_END)
    if spy is None or spy.empty:
        raise SystemExit("SPY prices unavailable — cannot build the calendar.")
    start_ts, end_ts = pd.Timestamp(SIM_START), pd.Timestamp(SIM_END)
    fmonths: dict[tuple, pd.Timestamp] = {}
    for ts in spy.index:
        if start_ts <= ts <= end_ts:
            fmonths.setdefault((ts.year, ts.month), ts)
    return [ts.date().isoformat() for ts in fmonths.values()]


def _slim_edgar(src: Path) -> dict | None:
    try:
        d = json.loads(src.read_text())
    except Exception:
        return None
    slim = {"cik": d.get("cik"), "entityName": d.get("entityName"), "facts": {}}
    for tax, concepts in d.get("facts", {}).items():
        keep = _DEI_KEEP if tax == "dei" else _GAAP_KEEP
        kept = {c: v for c, v in concepts.items() if c in keep}
        if kept:
            slim["facts"][tax] = kept
    return slim


def _dir_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


def main() -> None:
    if _SEED.exists():
        shutil.rmtree(_SEED)
    for sub in ("pit_market_cap", "prices", "edgar", "sp500_universe", "fred"):
        (_SEED / sub).mkdir(parents=True, exist_ok=True)

    fdates = _monthly_rebuild_dates()
    if not fdates:
        raise SystemExit(f"No trading days found in SPY between {SIM_START} and {SIM_END}; "
                         "cannot build monthly rebuild calendar.")
    print(f"Rebuild dates: {len(fdates)} months ({fdates[0]} .. {fdates[-1]})")

    # 1) Full market-cap grid for the ranking pool, then the Top-100 union.
    full_union = sorted({t for d in fdates for t in u.get_universe(d)})
    print(f"S&P membership union: {len(full_union)} tickers — computing PIT market caps…")
    u.prefetch_pit_market_caps(full_union, fdates)  # warms data/cache/pit_market_cap

    top_union: set[str] = set()
    for d in fdates:
        top_union |= set(u.get_universe_top_n(d, TOP_N))
    top_union = sorted(top_union)
    print(f"Top-{TOP_N} PIT union: {len(top_union)} tickers")
    if len(top_union) < 50:
        raise SystemExit(f"Top-N union suspiciously small ({len(top_union)}); "
                         "is the local raw-price / EDGAR cache warm?")

    # Copy the market-cap grid verbatim (immutable historical entries).
    shutil.copy2(_CACHE / "pit_market_cap" / "pit_market_caps.json",
                 _SEED / "pit_market_cap" / "pit_market_caps.json")

    # 2) Adjusted prices for the Top-100 union + SPY (ensure the 2016-keyed pkl).
    prices = PriceData()
    price_tickers = sorted(set(top_union) | {"SPY"})
    n_px = 0
    for t in price_tickers:
        prices.get_prices(t, WARMUP_START, SIM_END)  # ensures {t}_2016-01-01.pkl
        src = _CACHE / "prices" / f"{t}_{WARMUP_START}.pkl"
        if src.exists():
            shutil.copy2(src, _SEED / "prices" / src.name)
            n_px += 1
        else:
            print(f"  WARN no adjusted-price pkl for {t}")
    print(f"Copied {n_px}/{len(price_tickers)} price pkls")

    # 3) Slim EDGAR for the Top-100 union (quality gate).
    n_ed = 0
    for t in top_union:
        src = _CACHE / "edgar" / f"{t}.json"
        if not src.exists():
            print(f"  WARN no EDGAR facts for {t} (quality gate will fetch live)")
            continue
        slim = _slim_edgar(src)
        if slim and slim["facts"]:
            (_SEED / "edgar" / f"{t}.json").write_text(json.dumps(slim))
            n_ed += 1
    print(f"Wrote {n_ed}/{len(top_union)} slim EDGAR files")

    # 4) Membership CSV + Fed Funds (small, avoids a cold-start download).
    csv_src = _CACHE / "sp500_universe" / "sp500_historical_components.csv"
    if csv_src.exists():
        shutil.copy2(csv_src, _SEED / "sp500_universe" / csv_src.name)
    fred_dir = _CACHE / "fred"
    if fred_dir.exists():
        for f in fred_dir.glob("*"):
            shutil.copy2(f, _SEED / "fred" / f.name)

    # Manifest + size report.
    sizes = {sub.name: round(_dir_mb(sub), 2) for sub in sorted(_SEED.iterdir()) if sub.is_dir()}
    manifest = {
        "window": {"warmup_start": WARMUP_START, "sim_start": SIM_START, "sim_end": SIM_END},
        "top_n": TOP_N, "months": len(fdates),
        "membership_union": len(full_union), "top_union": len(top_union),
        "tickers": top_union, "sizes_mb": sizes,
    }
    (_SEED / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("\nSeed cache sizes (MB):")
    for k, v in sizes.items():
        print(f"  {k:16} {v:8.2f}")
    print(f"  {'TOTAL':16} {round(_dir_mb(_SEED), 2):8.2f}")
    print(f"\nWrote {_SEED}")


if __name__ == "__main__":
    main()
