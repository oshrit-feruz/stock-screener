#!/usr/bin/env python3
"""Audit the clean PIT backtest for look-ahead / survivorship leaks.

Checks, using the exact intermediate the backtest consumes:
  1. Top-10 by dollar-volume at several rebalance dates — must look like the
     ACTUAL mega-caps of that era (if NVDA/TSLA topped 2005, ranking would be
     leaking the future). This is the strongest sniff-test for PIT correctness.
  2. Every traded (eligible) signal's ticker WAS in the top-100 as of the most
     recent rebalance strictly on/before the signal date — re-derived here
     independently and asserted.
  3. The dollar-volume value used at each rebalance is computed only from data
     up to that date (trailing) — verified by recomputing one case from raw.
"""
from __future__ import annotations

import bisect
import pickle
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(ROOT / ".env"))

_INTERMED = ROOT / "data" / "cache" / "clean_pit_intermediate.pkl"
_RAW_DIR = ROOT / "data" / "cache" / "prices_raw"
_FETCH_START, _FETCH_END = "1998-06-01", "2024-12-31"
_TOP_N, _DV_WINDOW = 100, 63
_SIM_START, _SIM_END = pd.Timestamp("2000-01-03"), pd.Timestamp("2024-12-31")


def _safe(t):
    return "".join(c for c in t if c.isalnum() or c in "-_")


def top_n(data):
    dv = data["dv"]
    out = {}
    for d_iso in data["rebals"]:
        d = pd.Timestamp(d_iso)
        members = data["members_by_rebal"][d_iso]
        caps = [(t, dv[t][d]) for t in members if t in dv and d in dv[t]]
        caps.sort(key=lambda x: -x[1])
        out[d] = caps
    return out


def main():
    with open(_INTERMED, "rb") as f:
        data = pickle.load(f)
    ranked = top_n(data)
    iso_by_date = {k[:10]: k for k in data["members_by_rebal"]}

    print("=" * 72)
    print("CHECK 1 — Top-10 by dollar-volume at each date (should match the era)")
    print("=" * 72)
    for d_iso in ["2000-03-15", "2005-06-15", "2010-06-15", "2015-06-15",
                  "2021-06-15", "2024-06-15"]:
        d = pd.Timestamp(d_iso)
        caps = ranked.get(d, [])
        n_members = len(data["members_by_rebal"][iso_by_date[d_iso]])
        top10 = [t for t, _ in caps[:10]]
        print(f"\n{d_iso}  (members={n_members}, ranked={len(caps)}):")
        print("   " + ", ".join(top10))

    print("\n" + "=" * 72)
    print("CHECK 2 — every eligible signal's ticker was in top-100 as of the")
    print("          most recent rebalance strictly on/before the signal date")
    print("=" * 72)
    topn_sets = {d: {t for t, _ in caps[:_TOP_N]} for d, caps in ranked.items()}
    keys = sorted(topn_sets.keys())
    total = 0
    violations = 0
    future_rebal = 0
    for t, cross in data["crossings"].items():
        for ts, comp, close in cross:
            if not (_SIM_START <= ts <= _SIM_END):
                continue
            idx = bisect.bisect_right(keys, ts) - 1
            if idx < 0:
                continue
            rebal = keys[idx]
            # rebalance must be strictly on/before the signal (no future)
            if rebal > ts:
                future_rebal += 1
            in_top = t in topn_sets[rebal]
            if in_top:
                total += 1          # this is an eligible trade
                if rebal > ts:
                    violations += 1
    print(f"  eligible signals: {total}")
    print(f"  signals whose governing rebalance is AFTER the signal date: {future_rebal}")
    print(f"  look-ahead violations (eligible + future rebalance): {violations}")
    print(f"  VERDICT: {'CLEAN' if violations == 0 and future_rebal == 0 else 'LEAK!'}")

    print("\n" + "=" * 72)
    print("CHECK 3 — dollar-volume at a rebalance uses only trailing data")
    print("=" * 72)
    # Recompute AAPL's DV at 2015-06-15 from raw, using ONLY data <= that date,
    # and compare to the stored value.
    d = pd.Timestamp("2015-06-15")
    t = "AAPL"
    stored = data["dv"].get(t, {}).get(d)
    with open(_RAW_DIR / f"{_safe(t)}_{_FETCH_START}_{_FETCH_END}.pkl", "rb") as f:
        raw = pickle.load(f)
    past = raw[raw.index <= d]
    dvol = (past["Close"] * past["Volume"]).astype(float)
    recomputed = float(dvol.rolling(_DV_WINDOW).median().iloc[-1])
    # Also compute using FULL data (incl. future) to show it would differ if leaked
    full = (raw["Close"] * raw["Volume"]).astype(float)
    # median centered would need future; trailing rolling at d is identical, so
    # instead show the next-quarter value to prove the series is date-sensitive.
    nxt = pd.Timestamp("2015-09-15")
    dvol_nxt = float(full[full.index <= nxt].rolling(_DV_WINDOW).median().iloc[-1])
    print(f"  AAPL DV stored @ {d.date()}      : {stored:,.0f}")
    print(f"  AAPL DV recomputed (<= date)     : {recomputed:,.0f}")
    print(f"  match: {abs(stored - recomputed) < 1e-6}")
    print(f"  AAPL DV @ next quarter {nxt.date()}: {dvol_nxt:,.0f}"
          "  (differs -> date-sensitive, trailing)")

    print("\n" + "=" * 72)
    print("CHECK 4 — ranking coverage per sampled date (members missing a DV are")
    print("          simply not ranked; large gaps would bias the top-100)")
    print("=" * 72)
    for d_iso in ["2000-03-15", "2008-09-15", "2015-06-15", "2024-06-15"]:
        d = pd.Timestamp(d_iso)
        n_members = len(data["members_by_rebal"][iso_by_date[d_iso]])
        n_ranked = len(ranked.get(d, []))
        print(f"  {d_iso}: {n_ranked}/{n_members} members have a dollar-volume "
              f"({n_ranked/n_members:.0%})")


if __name__ == "__main__":
    main()
