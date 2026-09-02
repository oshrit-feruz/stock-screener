#!/usr/bin/env python3
"""Out-of-sample validation of the dislocation-gate threshold D.

We must not trust D=10% just because it topped the 2004-2024 in-sample run.
Two checks:

  A. Sensitivity / plateau — sweep a fine D grid on rolling 5y windows. If 8-12%
     all behave similarly, the threshold is a robust plateau, not a lucky spike.
  B. Train/test split — pick the D with the best EXCESS CAGR over SPY on a train
     half, then measure that same D on the untouched test half (both directions).
     A threshold chosen out-of-sample that still beats SPY in-test is real.

SPY-core + conditional overlay, 2-year sleeves, 10%×10, clean universe.
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
from core.data.eodhd import fetch_eod  # noqa: E402
from run_spy_overlay import simulate_overlay  # noqa: E402

D_GRID = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20]
HOLD, PCT, MAXP = 504, 0.10, 10
WIN_LEN = 5
_OUT = ROOT / "validation" / "overlay_oos.md"


def _load():
    data = base.build_intermediate()
    topn = base.top_n_by_rebal(data)
    elig = base.eligible_crossings(data, topn)
    spy = fetch_eod("SPY.US", base._FETCH_START, base._FETCH_END, adjust=True)
    full_cal = spy["Close"].index
    full_cal = full_cal[(full_cal >= pd.Timestamp("2004-01-02")) & (full_cal <= base._SIM_END)]
    elig = base.snap_events_to_calendar(elig, full_cal)
    panel = base._load_close_panel(sorted(elig), full_cal)
    spy_px = spy["Close"]
    s = spy_px.reindex(full_cal, method="ffill")
    dd = (s.rolling(252, min_periods=1).max() - s) / s.rolling(252, min_periods=1).max()
    return elig, panel, full_cal, spy_px, dd


def _perf(elig, panel, cal, spy_px, dd, D):
    """CAGR, Sharpe, excess vs SPY over a calendar."""
    ser = simulate_overlay(elig, panel, cal, spy_px, dd, HOLD, PCT, MAXP, D)
    m = base.metrics(ser, cal)
    sm = base.spy_metrics(cal)
    return {"cagr": m["cagr"], "sharpe": m["sharpe"], "dd": m["max_dd"],
            "excess": m["cagr"] - sm["cagr"], "spy_cagr": sm["cagr"],
            "spy_sharpe": sm["sharpe"], "beat": m["cagr"] > sm["cagr"]}


def main():
    print("Loading...", flush=True)
    elig, panel, full_cal, spy_px, dd = _load()

    # windows for sensitivity
    windows = []
    for sy in range(2004, 2025 - WIN_LEN + 1):
        w0 = pd.Timestamp(f"{sy}-01-01"); w1 = pd.Timestamp(f"{sy+WIN_LEN-1}-12-31")
        cw = full_cal[(full_cal >= w0) & (full_cal <= w1)]
        if len(cw) >= 252:
            windows.append((f"{sy}-{sy+WIN_LEN-1}", cw))
    spy_by_win = {n: base.spy_metrics(cw) for n, cw in windows}

    # ── A. sensitivity ──
    print("\nA. threshold sensitivity (rolling 5y):", flush=True)
    sens = []
    for D in D_GRID:
        exc, shp, beats = [], [], 0
        for n, cw in windows:
            base._SIM_START = cw[0]
            ser = simulate_overlay(elig, panel, cw, spy_px, dd, HOLD, PCT, MAXP, D)
            m = base.metrics(ser, cw); sm = spy_by_win[n]
            exc.append(m["cagr"] - sm["cagr"]); shp.append(m["sharpe"])
            if m["cagr"] > sm["cagr"]:
                beats += 1
        sens.append({"D": D, "beat": beats, "n": len(windows),
                     "med_excess": float(np.median(exc)), "med_sharpe": float(np.median(shp))})
        print(f"   D={D:.0%}: beat {beats}/{len(windows)}, med excess "
              f"{np.median(exc):+.1%}, med Sharpe {np.median(shp):.2f}", flush=True)
    base._SIM_START = pd.Timestamp("2004-01-02")

    # ── B. train/test ──
    def half(a, b):
        return full_cal[(full_cal >= pd.Timestamp(a)) & (full_cal <= pd.Timestamp(b))]
    splits = [
        ("2004-2013", "2004-01-02", "2013-12-31", "2014-2024", "2014-01-01", "2024-12-31"),
        ("2014-2024", "2014-01-01", "2024-12-31", "2004-2013", "2004-01-02", "2013-12-31"),
    ]
    print("\nB. train/test split:", flush=True)
    tt = []
    for tr_name, tr0, tr1, te_name, te0, te1 in splits:
        cal_tr, cal_te = half(tr0, tr1), half(te0, te1)
        train = {D: _perf(elig, panel, cal_tr, spy_px, dd, D) for D in D_GRID}
        best_D = max(D_GRID, key=lambda D: train[D]["excess"])
        te = _perf(elig, panel, cal_te, spy_px, dd, best_D)
        tt.append({"train": tr_name, "test": te_name, "best_D": best_D,
                   "train_excess": train[best_D]["excess"], "te": te})
        print(f"   train {tr_name} → best D={best_D:.0%} (train excess "
              f"{train[best_D]['excess']:+.1%}); TEST {te_name}: excess "
              f"{te['excess']:+.1%}, Sharpe {te['sharpe']:.2f} vs SPY {te['spy_sharpe']:.2f}, "
              f"{'BEATS' if te['beat'] else 'loses'}", flush=True)

    # ── report ──
    L = ["# Out-of-sample validation of the dislocation gate D\n",
         "SPY-core + conditional overlay, 2y sleeves, 10%×10, clean universe.\n",
         "## A. Threshold sensitivity — rolling 5y windows (is 10% a plateau?)\n",
         "| D (market DD gate) | beat SPY | median excess | median Sharpe |",
         "|---|--:|--:|--:|"]
    for r in sens:
        L.append(f"| {r['D']:.0%} | {r['beat']}/{r['n']} | {r['med_excess']:+.1%} "
                 f"| {r['med_sharpe']:.2f} |")
    L.append("\n## B. Train / test split (D chosen on train, measured on test)\n")
    L.append("| train (pick D) | best D | train excess | test | test excess | "
             "test Sharpe | vs SPY | verdict |")
    L.append("|---|--:|--:|---|--:|--:|--:|:--|")
    for r in tt:
        te = r["te"]
        L.append(f"| {r['train']} | {r['best_D']:.0%} | {r['train_excess']:+.1%} | "
                 f"{r['test']} | {te['excess']:+.1%} | {te['sharpe']:.2f} | "
                 f"{te['spy_sharpe']:.2f} | {'✅ BEATS' if te['beat'] else '❌ loses'} |")
    plateau = [r for r in sens if r["med_excess"] > 0 and r["beat"] >= len(windows) * 0.6]
    plateau_str = ", ".join(f"{r['D']:.0%}" for r in plateau) if plateau else "none"
    L.append("\n## Read\n")
    L.append("- Positive, majority-beating D values: " + plateau_str + " — a plateau "
             "here (not a lone 10% spike) means the gate is robust, not fit.")
    L.append("- If the train-chosen D still beats SPY on the untouched test half in "
             "BOTH directions, the threshold generalizes out-of-sample.")
    _OUT.write_text("\n".join(L) + "\n")
    print(f"\nsaved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
