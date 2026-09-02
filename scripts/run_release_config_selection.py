#!/usr/bin/env python3
"""Pick the best config to RELEASE by robust risk-adjusted return.

The full-window backtest is misleading (see run_clean_pit_rolling), so candidate
configurations are judged on their DISTRIBUTION of Sharpe across rolling 5-year
windows (2004-2024): median Sharpe (higher = better), dispersion (lower = more
stable), worst-window Sharpe, median excess CAGR vs SPY, and median drawdown.
Ranked by median rolling Sharpe. Pure price signal, clean survivorship-free
universe. Take-profit is excluded (the sweep showed it only hurts).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(ROOT / ".env"))

import run_clean_pit_backtest as base  # noqa: E402
import run_clean_pit_sweep as sweep  # noqa: E402
from core.data.eodhd import fetch_eod  # noqa: E402

WIN_LEN = 5
_OUT = ROOT / "validation" / "release_config_selection.md"

# (hold, stop, take_profit, pct, max_pos, label)
CANDIDATES = [
    (252, 0.00, 0.0, 0.10, 10, "default 1y / 10%×10"),
    (378, 0.00, 0.0, 0.10, 10, "1.5y / 10%×10"),
    (504, 0.00, 0.0, 0.10, 10, "2y / 10%×10"),
    (756, 0.00, 0.0, 0.10, 10, "3y / 10%×10"),
    (252, 0.40, 0.0, 0.10, 10, "1y / SL−40% / 10%×10"),
    (504, 0.40, 0.0, 0.10, 10, "2y / SL−40% / 10%×10"),
    (504, 0.30, 0.0, 0.10, 10, "2y / SL−30% / 10%×10"),
    (504, 0.00, 0.0, 0.05, 20, "2y / 5%×20 (diversified)"),
    (504, 0.00, 0.0, 0.20, 5,  "2y / 20%×5 (concentrated)"),
    (504, 0.40, 0.0, 0.20, 5,  "2y / SL−40% / 20%×5"),
    (756, 0.00, 0.0, 0.20, 5,  "3y / 20%×5 (concentrated)"),
    (504, 0.00, 0.0, 0.05, 20, "2y / 5%×20 dup"),  # placeholder removed below
]
CANDIDATES = [c for c in CANDIDATES if c[5] != "2y / 5%×20 dup"]


def main():
    print("Loading intermediate + panel...", flush=True)
    data = base.build_intermediate()
    topn = base.top_n_by_rebal(data)
    elig = base.eligible_crossings(data, topn)
    spy = fetch_eod("SPY.US", base._FETCH_START, base._FETCH_END, adjust=True)
    full_cal = spy["Close"].index
    full_cal = full_cal[(full_cal >= pd.Timestamp("2004-01-02")) & (full_cal <= base._SIM_END)]
    elig = base.snap_events_to_calendar(elig, full_cal)
    panel = base._load_close_panel(sorted(elig), full_cal)

    # Windows + SPY per-window metrics (computed once).
    windows = []
    for sy in range(2004, 2025 - WIN_LEN + 1):
        w0 = pd.Timestamp(f"{sy}-01-01")
        w1 = pd.Timestamp(f"{sy + WIN_LEN - 1}-12-31")
        cal_w = full_cal[(full_cal >= w0) & (full_cal <= w1)]
        if len(cal_w) >= 252:
            windows.append((f"{sy}-{sy+WIN_LEN-1}", cal_w))
    spy_by_win = {}
    for name, cal_w in windows:
        spy_by_win[name] = base.spy_metrics(cal_w)
    print(f"  {len(windows)} rolling {WIN_LEN}y windows\n", flush=True)

    rows = []
    for hold, stop, tp, pct, mp, label in CANDIDATES:
        sharpes, excesses, dds, ntr, beats = [], [], [], [], 0
        for name, cal_w in windows:
            base._SIM_START = cal_w[0]
            sweep._SIM_START = cal_w[0]
            res = sweep.simulate_cfg(elig, panel, cal_w, hold, stop, tp, pct, mp)
            m = base.metrics(res["daily"], cal_w)
            sm = spy_by_win[name]
            sharpes.append(m["sharpe"])
            excesses.append(m["cagr"] - sm["cagr"])
            dds.append(m["max_dd"])
            ntr.append(len(res["trades"]))
            if m["sharpe"] > sm["sharpe"]:
                beats += 1
        rows.append({
            "label": label, "hold": hold, "stop": stop, "pct": pct, "mp": mp,
            "med_sharpe": float(np.median(sharpes)),
            "std_sharpe": float(np.std(sharpes)),
            "min_sharpe": float(np.min(sharpes)),
            "med_excess": float(np.median(excesses)),
            "med_dd": float(np.median(dds)),
            "beat_sharpe": beats, "n_win": len(windows),
            "avg_trades": float(np.mean(ntr)),
        })
        print(f"  {label:<28} med Sharpe {rows[-1]['med_sharpe']:.2f} "
              f"(±{rows[-1]['std_sharpe']:.2f}, worst {rows[-1]['min_sharpe']:.2f})  "
              f"med excess {rows[-1]['med_excess']:+.1%}  med DD {rows[-1]['med_dd']:.0%}",
              flush=True)

    base._SIM_START = pd.Timestamp("2004-01-02")
    sweep._SIM_START = pd.Timestamp("2004-01-02")

    rows.sort(key=lambda r: -r["med_sharpe"])
    spy_med_sharpe = float(np.median([spy_by_win[n]["sharpe"] for n, _ in windows]))

    L = ["# Best config to release — ranked by robust risk-adjusted return\n",
         f"Candidate configs judged on their distribution of Sharpe across "
         f"**{len(windows)} rolling {WIN_LEN}-year windows** (2004-2024), clean "
         f"survivorship-free universe, pure price signal. Ranked by **median rolling "
         f"Sharpe**. SPY's median rolling Sharpe over the same windows: "
         f"**{spy_med_sharpe:.2f}**.\n",
         "| rank | config | med Sharpe | ±std | worst | med excess vs SPY | med DD | "
         "beat-SPY-Sharpe | avg trades |",
         "|---|---|--:|--:|--:|--:|--:|--:|--:|"]
    for i, r in enumerate(rows, 1):
        L.append(f"| {i} | {r['label']} | **{r['med_sharpe']:.2f}** | {r['std_sharpe']:.2f} "
                 f"| {r['min_sharpe']:.2f} | {r['med_excess']:+.1%} | {r['med_dd']:.0%} "
                 f"| {r['beat_sharpe']}/{r['n_win']} | {r['avg_trades']:.0f} |")
    win = rows[0]
    L.append(f"\n## Winner: **{win['label']}**\n")
    L.append(f"Median rolling Sharpe {win['med_sharpe']:.2f} vs SPY {spy_med_sharpe:.2f}, "
             f"std {win['std_sharpe']:.2f} (stability), worst-window Sharpe "
             f"{win['min_sharpe']:.2f}, median drawdown {win['med_dd']:.0%}, "
             f"beats SPY's Sharpe in {win['beat_sharpe']}/{win['n_win']} windows.\n")
    L.append("Reminder: even the winner does not reliably beat SPY (the edge is "
             "regime-dependent, per run_clean_pit_rolling). This is the most stable "
             "risk-adjusted config to ship IF building on the signal regardless — not "
             "a claim that it beats the index.")
    _OUT.write_text("\n".join(L) + "\n")
    print(f"\nWINNER: {win['label']}  (med Sharpe {win['med_sharpe']:.2f})", flush=True)
    print(f"saved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
