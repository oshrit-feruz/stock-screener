#!/usr/bin/env python3
"""Parameter sweep on the CLEAN, survivorship-free framework (2004-2024).

Which configuration is best? On the same bias-free universe as
run_clean_pit_backtest (PIT S&P 500 top-100 by dollar-volume, pure price signal,
completion rule) it sweeps:

  * holding period (max hold days)      x
  * stop-loss (daily-close hard stop)   x
  * take-profit (daily-close target)

with V1 sizing (10%/signal, max 10), then a small sizing sub-sweep on the best
risk-adjusted cell. Reuses the cached crossings/dollar-volume intermediate, so
every cell is fast. Answers explicitly: the most profitable hold with NO TP and
NO SL, and the best cell by CAGR / Sharpe / drawdown across all combinations.
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

HOLDS = [126, 252, 378, 504, 756, 1008]   # 0.5, 1, 1.5, 2, 3, 4 years
STOPS = [0.0, 0.20, 0.30, 0.40]            # 0 = no stop-loss
TPS = [0.0, 0.50, 1.00, 2.00]              # 0 = no take-profit
SIZINGS = [(0.05, 20), (0.10, 10), (0.20, 5)]   # sub-sweep only
_INITIAL = base._INITIAL_CAP
_MIN_POS = base._MIN_POSITION
_SIM_START = pd.Timestamp("2004-01-02")
_SIM_END = base._SIM_END
_OUT = ROOT / "validation" / "clean_pit_sweep.md"


def simulate_cfg(elig, panel, cal, hold, stop, tp, pct=0.10, max_pos=10):
    events = defaultdict(list)
    for t, cross in elig.items():
        for ts, comp, close in cross:
            if _SIM_START <= ts <= cal[-1]:
                events[ts].append((t, comp, close))
    prices = panel.reindex(cal, method="ffill")
    arr = prices.values.astype(float)
    col = {c: i for i, c in enumerate(prices.columns)}
    last_idx = len(cal) - 1

    def px(di, t):
        ci = col.get(t)
        return float(arr[di, ci]) if ci is not None else float("nan")

    def pval(di, cash, pos):
        v = cash
        for p in pos.values():
            q = px(di, p["ticker"])
            v += p["shares"] * (q if not np.isnan(q) else p["entry_price"])
        return v

    cash = _INITIAL
    pos: dict[int, dict] = {}
    last_entry: dict[str, int] = {}
    trades: list[dict] = []
    dvals = np.zeros(len(cal))
    pid = 0
    for di, day in enumerate(cal):
        for k in list(pos.keys()):
            p = pos[k]
            price = px(di, p["ticker"])
            hit_stop = (stop > 0 and not np.isnan(price)
                        and price <= p["entry_price"] * (1 - stop))
            hit_tp = (tp > 0 and not np.isnan(price)
                      and price >= p["entry_price"] * (1 + tp))
            hit_hold = di >= p["exit_idx"]
            if hit_stop or hit_tp or hit_hold:
                pos.pop(k)
                ep = price if not np.isnan(price) else p["entry_price"]
                cash += p["shares"] * ep
                exit_kind = ("stop" if hit_stop else "tp" if hit_tp else "hold")
                trades.append({"ret": ep / p["entry_price"] - 1, "kind": exit_kind})
        for t, comp, cprice in sorted(events.get(day, []), key=lambda x: -x[1]):
            if di + hold > last_idx:
                continue
            if t in last_entry and (di - last_entry[t]) < hold:
                continue
            if len(pos) >= max_pos:
                continue
            pv = pval(di, cash, pos)
            alloc = min(pv * pct, cash)
            if alloc < _MIN_POS:
                continue
            ep = px(di, t)
            if np.isnan(ep) or ep <= 0:
                ep = cprice
            if ep <= 0:
                continue
            pid += 1
            pos[pid] = {"ticker": t, "entry_idx": di, "exit_idx": di + hold,
                        "entry_price": ep, "shares": alloc / ep}
            last_entry[t] = di
            cash -= alloc
        dvals[di] = pval(di, cash, pos)
    dv = pd.Series(dvals, index=cal)
    return {"trades": trades, "daily": dv, "final": float(dv.iloc[-1])}


def _metrics_row(res, cal, hold, stop, tp, spy_cagr, pct=0.10, max_pos=10):
    m = base.metrics(res["daily"], cal)
    rets = np.array([t["ret"] for t in res["trades"]], float)
    n = len(rets)
    kinds = defaultdict(int)
    for t in res["trades"]:
        kinds[t["kind"]] += 1
    return {"hold": hold, "stop": stop, "tp": tp, "pct": pct, "max_pos": max_pos,
            "cagr": m["cagr"], "sharpe": m["sharpe"], "max_dd": m["max_dd"],
            "final": m["final"], "win": float((rets > 0).mean()) if n else 0.0,
            "n": n, "kinds": dict(kinds), "beat": m["cagr"] > spy_cagr}


def _fmt(r):
    sl = "none" if r["stop"] == 0 else f"−{int(r['stop']*100)}%"
    tp = "none" if r["tp"] == 0 else f"+{int(r['tp']*100)}%"
    return (f"hold {r['hold']} / SL {sl} / TP {tp} / size {int(r['pct']*100)}%×{r['max_pos']}: "
            f"CAGR {r['cagr']:+.1%}, Sharpe {r['sharpe']:.2f}, MaxDD {r['max_dd']:.0%}, "
            f"win {r['win']:.0%}, {r['n']} trades")


def main():
    print("Loading intermediate + panel...", flush=True)
    data = base.build_intermediate()
    topn = base.top_n_by_rebal(data)
    elig = base.eligible_crossings(data, topn)
    spy = fetch_eod("SPY.US", base._FETCH_START, base._FETCH_END, adjust=True)
    cal = spy["Close"].index
    cal = cal[(cal >= _SIM_START) & (cal <= _SIM_END)]
    elig = base.snap_events_to_calendar(elig, cal)
    panel = base._load_close_panel(sorted(elig), cal)
    spy_m = base.spy_metrics(cal)
    print(f"  SPY: CAGR {spy_m['cagr']:+.1%}  Sharpe {spy_m['sharpe']:.2f}  "
          f"MaxDD {spy_m['max_dd']:.1%}\n", flush=True)

    rows = []
    for hold in HOLDS:
        for stop in STOPS:
            for tp in TPS:
                res = simulate_cfg(elig, panel, cal, hold, stop, tp)
                rows.append(_metrics_row(res, cal, hold, stop, tp, spy_m["cagr"]))
        print(f"  hold={hold} done ({len(STOPS)*len(TPS)} cells)", flush=True)

    # Pure hold (no SL, no TP) — the "most profitable hold" question.
    pure = sorted([r for r in rows if r["stop"] == 0 and r["tp"] == 0],
                  key=lambda r: r["hold"])
    best_pure = max(pure, key=lambda r: r["cagr"])

    best_cagr = max(rows, key=lambda r: r["cagr"])
    best_sharpe = max(rows, key=lambda r: r["sharpe"])
    best_dd = max(rows, key=lambda r: r["max_dd"])
    beats = [r for r in rows if r["beat"]]

    # Sizing sub-sweep on the best-Sharpe cell.
    sub = []
    bh, bs, bt = best_sharpe["hold"], best_sharpe["stop"], best_sharpe["tp"]
    for pct, mp in SIZINGS:
        res = simulate_cfg(elig, panel, cal, bh, bs, bt, pct, mp)
        sub.append(_metrics_row(res, cal, bh, bs, bt, spy_m["cagr"], pct, mp))

    L = ["# Clean framework sweep — hold × stop-loss × take-profit (2004-2024)\n",
         f"Bias-free universe (PIT S&P 500 top-100 by dollar-volume), pure price "
         f"signal, V1 sizing (10%/max10 unless noted). **SPY: CAGR "
         f"{spy_m['cagr']:+.1%}, Sharpe {spy_m['sharpe']:.2f}, MaxDD "
         f"{spy_m['max_dd']:.1%}.**\n",
         "Holds in trading days: 126≈½y, 252≈1y, 378≈1.5y, 504≈2y, 756≈3y, 1008≈4y.\n",
         "## 1. Most profitable hold with NO stop-loss and NO take-profit\n",
         "| hold | CAGR | Sharpe | MaxDD | win% | trades | vs SPY |",
         "|---|--:|--:|--:|--:|--:|:--|"]
    for r in pure:
        L.append(f"| {r['hold']} | {r['cagr']:+.1%} | {r['sharpe']:.2f} | {r['max_dd']:.0%} "
                 f"| {r['win']:.0%} | {r['n']} | {'BEATS' if r['beat'] else 'loses'} |")
    L.append(f"\n**Most profitable pure hold → {best_pure['hold']} days "
             f"({best_pure['cagr']:+.1%} CAGR).**\n")

    L.append("## 2. CAGR across all stop × take-profit, per hold\n")
    for h in HOLDS:
        L.append(f"\n### hold = {h}\n")
        L.append("| SL \\ TP | none | +50% | +100% | +200% |\n|---|--:|--:|--:|--:|")
        for stop in STOPS:
            cells = []
            for tp in TPS:
                r = next(x for x in rows if x["hold"] == h and x["stop"] == stop and x["tp"] == tp)
                cells.append(f"{r['cagr']:+.1%}")
            sl = "none" if stop == 0 else f"−{int(stop*100)}%"
            L.append(f"| **{sl}** | " + " | ".join(cells) + " |")

    L.append("\n## 3. Overall winners (all 96 combinations)\n")
    L.append(f"- **Best CAGR** — {_fmt(best_cagr)}")
    L.append(f"- **Best Sharpe (risk-adjusted)** — {_fmt(best_sharpe)}")
    L.append(f"- **Shallowest drawdown** — {_fmt(best_dd)}")
    L.append(f"- **Beat SPY on CAGR**: {len(beats)}/{len(rows)} combinations\n")

    L.append("## 4. Sizing sub-sweep on the best-Sharpe cell\n")
    L.append(f"(hold {bh} / SL {'none' if bs==0 else f'−{int(bs*100)}%'} / "
             f"TP {'none' if bt==0 else f'+{int(bt*100)}%'})\n")
    L.append("| sizing | CAGR | Sharpe | MaxDD | win% | trades |\n|---|--:|--:|--:|--:|--:|")
    for r in sub:
        L.append(f"| {int(r['pct']*100)}% × {r['max_pos']} | {r['cagr']:+.1%} | "
                 f"{r['sharpe']:.2f} | {r['max_dd']:.0%} | {r['win']:.0%} | {r['n']} |")

    _OUT.write_text("\n".join(L) + "\n")
    print("\n" + "\n".join(L[-24:]), flush=True)
    print(f"\nsaved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
