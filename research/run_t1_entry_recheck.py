#!/usr/bin/env python3
"""Re-check the headline configs under the entry look-ahead fix (T+1 open).

Runs two configurations BOTH ways — legacy same-bar close vs. the corrected T+1
open fill — and prints a before/after table. The "before" columns must reproduce
the previously documented numbers (best ~0.82 Sharpe, V1 ~0.86) as a sanity anchor.

  A — best variant : Top-100 PIT universe + threshold 0.60 + money market
                     (Fed funds), flat 10%  → research/run_combined_clean_universe engine
  B — plain V1     : 50 survivors + flat 10% + no cash sleeve
                     → scripts/run_portfolio_sim engine (gated to VALIDATION_UNIVERSE)

Analysis only; writes a report under results/research/. No product code changed here.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.tickers import VALIDATION_UNIVERSE
from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from data.sp500_universe import get_universe, get_universe_top_n, prefetch_pit_market_caps
from research.run_combined_clean_universe import _TOP_N, simulate as sim_cash
from scripts.run_combined_validation import load_fedfunds
from scripts.run_portfolio_sim import compute_metrics, load_all_data
from scripts.run_portfolio_sim import simulate as sim_plain

_SIM_START = pd.Timestamp("2018-01-01")
_SIM_END = pd.Timestamp("2024-12-31")


def _row(m, n):
    return (m["final_value"], m["total_ret"], m["cagr"], m["sharpe"], m["max_dd"], n)


def main():
    prices_obj = PriceData()
    fund = EdgarFundamentals(fallback=PointInTimeFundamentals())
    spy_close = prices_obj.get_prices("SPY", "2016-01-01", "2024-12-31")["Close"]
    master_cal = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)].index

    # ── Config A: best variant (Top-100 PIT + thr0.60 + money market) ──────────
    print("Loading Top-100 PIT universe (best variant)...")
    fmonths = {}
    for ts in master_cal:
        fmonths.setdefault((ts.year, ts.month), ts)
    union_full = sorted({t for ts in fmonths.values() for t in get_universe(ts.date().isoformat())})
    prefetch_pit_market_caps(union_full, [ts.date().isoformat() for ts in fmonths.values()])
    month_members = {k: set(get_universe_top_n(ts.date().isoformat(), _TOP_N)) for k, ts in fmonths.items()}
    union = sorted(set().union(*month_members.values()))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cr, pw, ow, _ = load_all_data(prices_obj, fund, union, with_opens=True)
    rate = load_fedfunds().reindex(master_cal, method="ffill").values.astype(float)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        a_legacy = sim_cash(cr, pw, master_cal, "flat", "fed_funds", rate, month_members, entry_threshold=0.60)
        a_t1     = sim_cash(cr, pw, master_cal, "flat", "fed_funds", rate, month_members, entry_threshold=0.60,
                            opens_wide=ow)

    # ── Config B: plain V1 (50 survivors, flat 10%, no cash sleeve) ────────────
    print("Loading 50-survivor universe (V1 baseline)...")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cr50, pw50, ow50, _ = load_all_data(prices_obj, fund, list(VALIDATION_UNIVERSE), with_opens=True)
    mm50 = {(d.year, d.month): set(VALIDATION_UNIVERSE) for d in master_cal}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        b_legacy = sim_plain(cr50, pw50, master_cal, 0.10, 10, month_members=mm50)
        b_t1     = sim_plain(cr50, pw50, master_cal, 0.10, 10, month_members=mm50, opens_wide=ow50)

    runs = {
        ("A · Best (Top-100+thr0.60+MM)", "same-bar close"): (a_legacy, compute_metrics(a_legacy["daily_values"], 100_000)),
        ("A · Best (Top-100+thr0.60+MM)", "T+1 open"):       (a_t1,     compute_metrics(a_t1["daily_values"], 100_000)),
        ("B · Plain V1 (50 surv, flat10)", "same-bar close"):(b_legacy, compute_metrics(b_legacy["daily_values"], 100_000)),
        ("B · Plain V1 (50 surv, flat10)", "T+1 open"):      (b_t1,     compute_metrics(b_t1["daily_values"], 100_000)),
    }

    div = "=" * 100
    print("\n" + div)
    print("ENTRY LOOK-AHEAD FIX — same-bar close vs T+1 open, 2018-2024, $100k")
    print(div)
    print(f"\n  {'Config':<32}  {'Fill':<15}  {'Final $':>11}  {'TotRet':>8}  {'CAGR':>7}  {'Sharpe':>7}  {'MaxDD':>7}  {'Trades':>6}")
    print("  " + "-" * 96)
    for (cfg, fill), (res, m) in runs.items():
        n = len(res["trades"])
        fv, tr, cg, sh, dd, _ = _row(m, n)
        print(f"  {cfg:<32}  {fill:<15}  ${fv:>10,.0f}  {tr:>+7.1%}  {cg:>+6.1%}  {sh:>7.2f}  {dd:>6.1%}  {n:>6}")
    print()

    # Deltas (T+1 minus legacy)
    print("  DELTA (T+1 open − same-bar close)")
    print("  " + "-" * 96)
    for cfg in ["A · Best (Top-100+thr0.60+MM)", "B · Plain V1 (50 surv, flat10)"]:
        _, ml = runs[(cfg, "same-bar close")]
        _, mt = runs[(cfg, "T+1 open")]
        print(f"    {cfg:<32}  CAGR {mt['cagr']-ml['cagr']:>+6.1%}   Sharpe {mt['sharpe']-ml['sharpe']:>+5.2f}   "
              f"Final ${mt['final_value']-ml['final_value']:>+11,.0f}")
    print()
    print(div)

    # ── Markdown report ────────────────────────────────────────────────────────
    out = Path(__file__).parent.parent / "results" / "research" / "t1_entry_recheck.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Entry look-ahead fix — T+1 open re-check\n",
             "Same V1 signal, only entry execution changed from signal-day close to next-day",
             "(T+1) open. 2018-2024, $100k, 252d exit unchanged.\n",
             "| Config | Fill | Final $ | Total ret | CAGR | Sharpe | Max DD | Trades |",
             "|---|---|--:|--:|--:|--:|--:|--:|"]
    for (cfg, fill), (res, m) in runs.items():
        n = len(res["trades"])
        lines.append(f"| {cfg} | {fill} | ${m['final_value']:,.0f} | {m['total_ret']:+.1%} | "
                     f"{m['cagr']:+.1%} | {m['sharpe']:.2f} | {m['max_dd']:.1%} | {n} |")
    lines.append("")
    out.write_text("\n".join(lines))
    print(f"Report written: {out}")


if __name__ == "__main__":
    main()
