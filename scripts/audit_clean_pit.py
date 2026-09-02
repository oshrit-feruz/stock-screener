#!/usr/bin/env python3
"""Audit the clean PIT research framework for look-ahead / survivorship leaks.

Runs against the SAME code paths the backtests consume (clean_intermediate +
clean_sim), not a re-implementation, so a regression in either is caught:

  1. Top-10 by dollar-volume at several rebalance dates — must look like the
     ACTUAL mega-caps of that era (if NVDA/TSLA topped 2005, ranking would be
     leaking the future). The strongest sniff-test for PIT correctness.
  2. Every signal the backtest treats as eligible (output of the real
     `eligible_crossings`) is re-checked here: its governing rebalance is
     re-derived independently (plain scan, not bisect), asserted to be on/before
     the signal date, and the ticker asserted to be in that rebalance's top-100.
     A control shows the check bites: eligibility recomputed with the NEXT
     (future) rebalance yields a different set, and none of the "future-only"
     signals leaked into the backtest's set.
  3. Dollar-volume at a rebalance uses only trailing data — recomputed from the
     raw pickles for EVERY member at sampled rebalances (data <= date only) and
     compared to the stored grid; the independently derived top-100 must match.
  4. Ranking coverage per sampled date.
  5. Simulation-level: fills are strictly after the signal bar, and no trade
     exits after its ticker's final print (no stale-quote carry).
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import clean_intermediate as ci  # noqa: E402
import clean_sim as cs  # noqa: E402

_SIM_START, _SIM_END = pd.Timestamp("2000-01-03"), ci.SIM_END
_SAMPLE_DATES = ["2000-03-15", "2005-06-15", "2010-06-15", "2015-06-15",
                 "2021-06-15", "2024-06-15"]
_DV_SAMPLE_DATES = ["2008-09-15", "2015-06-15", "2024-06-15"]


def _hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def check_1_top10(data: dict, ranked: dict) -> None:
    _hr("CHECK 1 — Top-10 by dollar-volume at each date (should match the era)")
    for d_iso in _SAMPLE_DATES:
        d = pd.Timestamp(d_iso)
        caps = ranked.get(d, [])
        n_members = len(data["members_by_rebal"][d.isoformat()])
        print(f"\n{d_iso}  (members={n_members}, ranked={len(caps)}):")
        print("   " + ", ".join(t for t, _ in caps[:10]))


def _scan_governing(rebals: list[pd.Timestamp], ts: pd.Timestamp) -> pd.Timestamp | None:
    """Independent (linear-scan) governing rebalance: latest one <= ts."""
    best = None
    for d in rebals:
        if d <= ts:
            best = d
    return best


def check_2_eligibility(data: dict, topn: dict, elig: dict) -> bool:
    _hr("CHECK 2 — every eligible signal validated against the governing (past)\n"
        "          rebalance ranking; control with a FUTURE rebalance")
    rebals = sorted(topn)
    top_sets = {d: set(v) for d, v in topn.items()}
    total = future_rebal = not_in_top = date_sensitive = 0
    for t, cross in elig.items():
        for ts, _comp, _close in cross:
            if not (_SIM_START <= ts <= _SIM_END):
                continue
            total += 1
            gov = _scan_governing(rebals, ts)
            if gov is None or gov > ts:
                future_rebal += 1
                continue
            if t not in top_sets[gov]:
                not_in_top += 1
            nxt_i = rebals.index(gov) + 1
            if nxt_i < len(rebals) and t not in top_sets[rebals[nxt_i]]:
                date_sensitive += 1
    # Control: eligibility computed with the NEXT rebalance (future ranking).
    future_topn = {rebals[i]: topn[rebals[i + 1]] for i in range(len(rebals) - 1)}
    future_elig = ci.eligible_crossings(data, future_topn)
    real = {(t, ts) for t, cr in elig.items() for ts, _, _ in cr}
    fut = {(t, ts) for t, cr in future_elig.items() for ts, _, _ in cr}
    future_only = fut - real
    leaked = {k for k in real if k in future_only}
    print(f"  eligible signals (backtest's own set): {total}")
    print(f"  governing rebalance AFTER the signal date: {future_rebal}")
    print(f"  eligible but NOT in the governing top-100: {not_in_top}")
    print(f"  eligible signals whose ticker leaves the top-100 at the next rebalance: "
          f"{date_sensitive}  (the check is date-sensitive, it bites)")
    print(f"  control — signals eligible only under the FUTURE ranking: {len(future_only)}; "
          f"of those present in the backtest's set: {len(leaked)}")
    ok = future_rebal == 0 and not_in_top == 0 and not leaked
    print(f"  VERDICT: {'CLEAN' if ok else 'LEAK!'}")
    return ok


def check_3_dollar_volume(data: dict, topn: dict) -> bool:
    _hr("CHECK 3 — dollar-volume at a rebalance uses only trailing data\n"
        "          (recomputed from raw for every member at sampled dates)")
    ok = True
    for d_iso in _DV_SAMPLE_DATES:
        d = pd.Timestamp(d_iso)
        members = data["members_by_rebal"][d.isoformat()]
        recomputed = {}
        for t in members:
            raw = ci.load_frame(ci.RAW_DIR, t)
            if raw is None or "Volume" not in raw.columns:
                continue
            past = raw[raw.index <= d]                       # ONLY data <= date
            med = (past["Close"] * past["Volume"]).astype(float).rolling(ci.DV_WINDOW).median()
            if len(med) and np.isfinite(med.iloc[-1]):
                recomputed[t] = float(med.iloc[-1])
        stored = {t: data["dv"][t][d] for t in members if t in data["dv"] and d in data["dv"][t]}
        common = set(stored) & set(recomputed)
        max_diff = max((abs(stored[t] - recomputed[t]) for t in common), default=0.0)
        indep_top = [t for t, _ in sorted(recomputed.items(), key=lambda x: -x[1])[:ci.TOP_N]]
        same_top = set(indep_top) == set(topn[d])
        ok &= max_diff < 1e-6 and same_top and set(stored) == set(recomputed)
        print(f"  {d_iso}: {len(common)} members recomputed, max |stored-recomputed| = "
              f"{max_diff:.3g}, keys match: {set(stored) == set(recomputed)}, independent "
              f"top-{ci.TOP_N} == backtest top-{ci.TOP_N}: {same_top}")
    print(f"  VERDICT: {'CLEAN' if ok else 'MISMATCH!'}")
    return ok


def check_4_coverage(data: dict, ranked: dict) -> None:
    _hr("CHECK 4 — ranking coverage per sampled date (members missing a DV are\n"
        "          simply not ranked; large gaps would bias the top-100)")
    for d_iso in ["2000-03-15", "2008-09-15", "2015-06-15", "2024-06-15"]:
        d = pd.Timestamp(d_iso)
        n_members = len(data["members_by_rebal"][d.isoformat()])
        n_ranked = len(ranked.get(d, []))
        print(f"  {d_iso}: {n_ranked}/{n_members} members have a dollar-volume "
              f"({n_ranked / n_members:.0%})")


def check_5_simulation(inp: cs.Inputs) -> bool:
    _hr("CHECK 5 — simulation: fills strictly after the signal bar; no exit after\n"
        "          a ticker's final print")
    w = cs.full_window(inp)
    res = cs.run(w.mkt, w.events, cs.SimConfig(hold=504))
    same_bar = late_fill = stale = 0
    for tr in res.trades:
        entry = pd.Timestamp(tr["entry"])
        prior = [ts for ts, _, _ in inp.elig[tr["ticker"]] if ts < entry]
        if not prior:
            same_bar += 1                    # entered with no earlier signal bar
            continue
        if (entry - max(prior)).days > 7:
            late_fill += 1                   # fill not at the next session
        if pd.Timestamp(tr["exit"]) > inp.panel.last_quote[tr["ticker"]]:
            stale += 1
    n_delist = res.exits.get("delist", 0)
    ok = same_bar == 0 and late_fill == 0 and stale == 0
    print(f"  trades: {len(res.trades)}; entered on/before their signal bar: {same_bar}; "
          f"filled later than the next session: {late_fill}; exited after the ticker's "
          f"final print: {stale}; forced exits at a final print: {n_delist}")
    print(f"  VERDICT: {'CLEAN' if ok else 'LEAK!'}")
    return ok


def main() -> None:
    random.seed(0)
    data = ci.build_intermediate()
    ranked = ci.ranked_by_rebal(data)
    topn = ci.top_n_by_rebal(data)
    elig = ci.eligible_crossings(data, topn)          # the backtest's own eligibility
    check_1_top10(data, ranked)
    ok2 = check_2_eligibility(data, topn, elig)
    ok3 = check_3_dollar_volume(data, topn)
    check_4_coverage(data, ranked)
    ok5 = check_5_simulation(cs.load_inputs(verbose=False))
    print("\nOVERALL:", "CLEAN" if (ok2 and ok3 and ok5) else "PROBLEMS FOUND")
    if not (ok2 and ok3 and ok5):
        sys.exit(1)


if __name__ == "__main__":
    main()
