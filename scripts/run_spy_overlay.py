#!/usr/bin/env python3
"""Test the core hypothesis: the signal's alpha is sparse (crisis-recovery only),
so deploy it as a conditional OVERLAY on an SPY core instead of a standalone
always-on strategy.

Model: the portfolio is always fully invested in SPY (no idle cash → no drag).
When the market is in a dislocation (SPY drawdown from its trailing 252-day high
>= D), eligible recovery signals may fire — funded by rotating OUT of SPY, up to
max_pos concurrent sleeves. When a sleeve exits (after `hold` days) its proceeds
rotate back into SPY. D = 0 means "always deploy" (isolates the cash-drag fix);
D > 0 adds regime gating (isolates the sparse-alpha fix).

Judged on rolling 5-year windows vs pure SPY: does gating the signal to
dislocations finally produce a STABLE edge, or is it still a coin flip?
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(ROOT / ".env"))

import run_clean_pit_backtest as base  # noqa: E402
from core.data.eodhd import fetch_eod  # noqa: E402

_INITIAL = base._INITIAL_CAP
_MIN_POS = base._MIN_POSITION
WIN_LEN = 5
_OUT = ROOT / "validation" / "spy_overlay.md"

# (D dislocation threshold, hold, pct, max_pos, label)
CONFIGS = [
    (0.00, 504, 0.10, 10, "always-deploy (idle→SPY, no gate)"),
    (0.10, 504, 0.10, 10, "gate: market DD ≥ 10%"),
    (0.15, 504, 0.10, 10, "gate: market DD ≥ 15%"),
    (0.20, 504, 0.10, 10, "gate: market DD ≥ 20%"),
    (0.15, 504, 0.20, 10, "gate ≥15% / 20% sleeves"),
]


def simulate_overlay(elig, panel, cal, spy_px, dd, hold, pct, max_pos, D):
    events = defaultdict(list)
    w0, w1 = cal[0], cal[-1]
    for t, cross in elig.items():
        for ts, comp, close in cross:
            if w0 <= ts <= w1:
                events[ts].append((t, comp, close))
    prices = panel.reindex(cal, method="ffill")
    arr = prices.values.astype(float)
    col = {c: i for i, c in enumerate(prices.columns)}
    spy = spy_px.reindex(cal, method="ffill").values.astype(float)
    ddv = dd.reindex(cal).values.astype(float)
    last_idx = len(cal) - 1

    def px(di, t):
        ci = col.get(t)
        return float(arr[di, ci]) if ci is not None else float("nan")

    shares_spy = _INITIAL / spy[0]
    pos: dict[int, dict] = {}
    last_entry: dict[str, int] = {}
    dvals = np.zeros(len(cal))
    pid = 0

    def total(di):
        v = shares_spy * spy[di]
        for p in pos.values():
            q = px(di, p["ticker"])
            v += p["shares"] * (q if not np.isnan(q) else p["entry_price"])
        return v

    for di in range(len(cal)):
        day = cal[di]
        # exits → proceeds back into SPY
        for k in [k for k, v in list(pos.items()) if di >= v["exit_idx"]]:
            p = pos.pop(k)
            ep = px(di, p["ticker"])
            if np.isnan(ep):
                ep = p["entry_price"]
            shares_spy += (p["shares"] * ep) / spy[di]
        # entries only during dislocation
        if ddv[di] >= D:
            for t, comp, cprice in sorted(events.get(day, []), key=lambda x: -x[1]):
                if di + hold > last_idx:
                    continue
                if t in last_entry and (di - last_entry[t]) < hold:
                    continue
                if len(pos) >= max_pos:
                    continue
                tv = total(di)
                alloc = min(tv * pct, shares_spy * spy[di])   # fund by selling SPY
                if alloc < _MIN_POS:
                    continue
                ep = px(di, t)
                if np.isnan(ep) or ep <= 0:
                    ep = cprice
                if ep <= 0:
                    continue
                pid += 1
                pos[pid] = {"ticker": t, "exit_idx": di + hold,
                            "entry_price": ep, "shares": alloc / ep}
                last_entry[t] = di
                shares_spy -= alloc / spy[di]
        dvals[di] = total(di)
    return pd.Series(dvals, index=cal)


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
    spy_px = spy["Close"]
    spy_on_cal = spy_px.reindex(full_cal, method="ffill")
    dd = (spy_on_cal.rolling(252, min_periods=1).max() - spy_on_cal) / \
         spy_on_cal.rolling(252, min_periods=1).max()

    windows = []
    for sy in range(2004, 2025 - WIN_LEN + 1):
        w0 = pd.Timestamp(f"{sy}-01-01")
        w1 = pd.Timestamp(f"{sy + WIN_LEN - 1}-12-31")
        cal_w = full_cal[(full_cal >= w0) & (full_cal <= w1)]
        if len(cal_w) >= 252:
            windows.append((f"{sy}-{sy+WIN_LEN-1}", cal_w))
    spy_by_win = {n: base.spy_metrics(cw) for n, cw in windows}
    spy_med_sharpe = float(np.median([spy_by_win[n]["sharpe"] for n, _ in windows]))
    print(f"  {len(windows)} rolling {WIN_LEN}y windows; SPY median Sharpe {spy_med_sharpe:.2f}\n",
          flush=True)

    rows = []
    for D, hold, pct, mp, label in CONFIGS:
        # full window
        full = simulate_overlay(elig, panel, full_cal, spy_px, dd, hold, pct, mp, D)
        fm = base.metrics(full, full_cal)
        # rolling
        exc, shp, beats = [], [], 0
        for n, cw in windows:
            s = simulate_overlay(elig, panel, cw, spy_px, dd, hold, pct, mp, D)
            m = base.metrics(s, cw)
            sm = spy_by_win[n]
            exc.append(m["cagr"] - sm["cagr"])
            shp.append(m["sharpe"])
            if m["cagr"] > sm["cagr"]:
                beats += 1
        rows.append({"label": label, "D": D,
                     "full_cagr": fm["cagr"], "full_sharpe": fm["sharpe"],
                     "full_dd": fm["max_dd"],
                     "med_excess": float(np.median(exc)), "med_sharpe": float(np.median(shp)),
                     "beats": beats, "n": len(windows)})
        print(f"  {label:<34} full CAGR {fm['cagr']:+.1%} (Sharpe {fm['sharpe']:.2f}, "
              f"DD {fm['max_dd']:.0%}) | rolling beat {beats}/{len(windows)}, "
              f"med excess {np.median(exc):+.1%}, med Sharpe {np.median(shp):.2f}", flush=True)

    L = ["# SPY-core + conditional signal overlay vs pure SPY\n",
         "Always fully invested in SPY; recovery signals fire only when the market "
         "is in a dislocation (SPY drawdown ≥ D from its trailing 252-day high), "
         "funded by rotating out of SPY and rotating back on exit. Clean "
         f"survivorship-free signal universe, 2-year sleeves. **SPY over 2004-2024: "
         f"CAGR {base.spy_metrics(full_cal)['cagr']:+.1%}, Sharpe "
         f"{base.spy_metrics(full_cal)['sharpe']:.2f}, MaxDD "
         f"{base.spy_metrics(full_cal)['max_dd']:.0%}. SPY median rolling {WIN_LEN}y "
         f"Sharpe {spy_med_sharpe:.2f}.**\n",
         "| config | full CAGR | full Sharpe | full MaxDD | rolling beat-SPY | "
         "med excess | med Sharpe |", "|---|--:|--:|--:|--:|--:|--:|"]
    for r in rows:
        L.append(f"| {r['label']} | {r['full_cagr']:+.1%} | {r['full_sharpe']:.2f} | "
                 f"{r['full_dd']:.0%} | {r['beats']}/{r['n']} | {r['med_excess']:+.1%} | "
                 f"{r['med_sharpe']:.2f} |")
    best = max(rows, key=lambda r: r["beats"])
    L.append(f"\n## Read\n")
    L.append(f"- SPY's own rolling {WIN_LEN}y Sharpe is {spy_med_sharpe:.2f}; a config only "
             "helps if it beats SPY in a clear majority of windows AND lifts the median "
             "Sharpe above that.")
    L.append(f"- Best gating config: **{best['label']}** — beats SPY in "
             f"{best['beats']}/{best['n']} windows, median excess {best['med_excess']:+.1%}, "
             f"median Sharpe {best['med_sharpe']:.2f} (vs SPY {spy_med_sharpe:.2f}).")
    L.append("- Compare to the STANDALONE signal (run_clean_pit_rolling): 8/17 windows, "
             "median excess ≈ 0. If the overlay lifts the beat-rate and median excess "
             "clearly above that, the sparse-alpha / wrong-deployment diagnosis holds.")
    _OUT.write_text("\n".join(L) + "\n")
    print(f"\nsaved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
