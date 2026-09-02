#!/usr/bin/env python3
"""Parameter sweep on the CLEAN, survivorship-free framework (2004-2024).

Which configuration is best? On the same bias-free universe and simulation
core as run_clean_pit_backtest (scripts/clean_sim.py) it sweeps:

  * holding period (max hold days)      x
  * stop-loss (daily-close hard stop)   x
  * take-profit (daily-close target)

with V1 sizing (10%/signal, max 10), then a small sizing sub-sweep on the best
risk-adjusted cell. Answers explicitly: the most profitable hold with NO TP and
NO SL, and the best cell by CAGR / Sharpe / drawdown across all combinations.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import clean_sim as cs  # noqa: E402

HOLDS = [126, 252, 378, 504, 756, 1008]   # 0.5, 1, 1.5, 2, 3, 4 years
STOPS = [0.0, 0.20, 0.30, 0.40]            # 0 = no stop-loss
TPS = [0.0, 0.50, 1.00, 2.00]              # 0 = no take-profit
SIZINGS = [(0.05, 20), (0.10, 10), (0.20, 5)]   # sub-sweep only
_OUT = ROOT / "validation" / "clean_pit_sweep.md"


def _cell(w: cs.Window, hold, stop, tp, spy_cagr, pct=0.10, max_pos=10) -> dict:
    res = cs.run(w.mkt, w.events, cs.SimConfig(hold=hold, stop=stop, tp=tp, pct=pct,
                                               max_pos=max_pos))
    m = cs.metrics(res.daily, w.cal)
    rets = np.array([t["ret"] for t in res.trades], float)
    return {"hold": hold, "stop": stop, "tp": tp, "pct": pct, "max_pos": max_pos,
            "cagr": m["cagr"], "sharpe": m["sharpe"], "max_dd": m["max_dd"],
            "final": m["final"], "win": float((rets > 0).mean()) if len(rets) else 0.0,
            "n": len(rets), "kinds": dict(res.exits), "beat": m["cagr"] > spy_cagr}


def _sl(stop) -> str:
    return "none" if stop == 0 else f"−{int(stop * 100)}%"


def _tp(tp) -> str:
    return "none" if tp == 0 else f"+{int(tp * 100)}%"


def _fmt(r) -> str:
    return (f"hold {r['hold']} / SL {_sl(r['stop'])} / TP {_tp(r['tp'])} / size "
            f"{int(r['pct'] * 100)}%×{r['max_pos']}: CAGR {r['cagr']:+.1%}, Sharpe "
            f"{r['sharpe']:.2f}, MaxDD {r['max_dd']:.0%}, win {r['win']:.0%}, {r['n']} trades")


def _sweep(w: cs.Window) -> list[dict]:
    rows = []
    for hold in HOLDS:
        for stop in STOPS:
            for tp in TPS:
                rows.append(_cell(w, hold, stop, tp, w.spy["cagr"]))
        print(f"  hold={hold} done ({len(STOPS) * len(TPS)} cells)", flush=True)
    return rows


def _grid_section(rows: list[dict]) -> list[str]:
    L = ["## 2. CAGR across all stop × take-profit, per hold\n"]
    for h in HOLDS:
        L.append(f"\n### hold = {h}\n")
        L.append("| SL \\ TP | none | +50% | +100% | +200% |\n|---|--:|--:|--:|--:|")
        for stop in STOPS:
            cells = [f"{r['cagr']:+.1%}" for tp in TPS for r in rows
                     if r["hold"] == h and r["stop"] == stop and r["tp"] == tp]
            L.append(f"| **{_sl(stop)}** | " + " | ".join(cells) + " |")
    return L


def _report(rows: list[dict], sub: list[dict], spy_m: dict, best_sharpe: dict) -> str:
    pure = sorted([r for r in rows if r["stop"] == 0 and r["tp"] == 0], key=lambda r: r["hold"])
    best_pure = max(pure, key=lambda r: r["cagr"])
    best_cagr = max(rows, key=lambda r: r["cagr"])
    best_dd = max(rows, key=lambda r: r["max_dd"])
    beats = [r for r in rows if r["beat"]]

    L = ["# Clean framework sweep — hold × stop-loss × take-profit (2004-2024)\n",
         "Bias-free universe (PIT S&P 500 top-100 by dollar-volume), pure price signal, "
         "next-session fills, V1 sizing (10%/max10 unless noted). **SPY: CAGR "
         f"{spy_m['cagr']:+.1%}, Sharpe {spy_m['sharpe']:.2f}, MaxDD {spy_m['max_dd']:.1%}.**\n",
         "Holds in trading days: 126≈½y, 252≈1y, 378≈1.5y, 504≈2y, 756≈3y, 1008≈4y.\n",
         "## 1. Most profitable hold with NO stop-loss and NO take-profit\n",
         "| hold | CAGR | Sharpe | MaxDD | win% | trades | vs SPY |",
         "|---|--:|--:|--:|--:|--:|:--|"]
    for r in pure:
        L.append(f"| {r['hold']} | {r['cagr']:+.1%} | {r['sharpe']:.2f} | {r['max_dd']:.0%} "
                 f"| {r['win']:.0%} | {r['n']} | {'BEATS' if r['beat'] else 'loses'} |")
    L.append(f"\n**Most profitable pure hold → {best_pure['hold']} days "
             f"({best_pure['cagr']:+.1%} CAGR).**\n")
    L += _grid_section(rows)
    L.append(f"\n## 3. Overall winners (all {len(rows)} combinations)\n")
    L.append(f"- **Best CAGR** — {_fmt(best_cagr)}")
    L.append(f"- **Best Sharpe (risk-adjusted)** — {_fmt(best_sharpe)}")
    L.append(f"- **Shallowest drawdown** — {_fmt(best_dd)}")
    L.append(f"- **Beat SPY on CAGR**: {len(beats)}/{len(rows)} combinations\n")
    L.append("## 4. Sizing sub-sweep on the best-Sharpe cell\n")
    L.append(f"(hold {best_sharpe['hold']} / SL {_sl(best_sharpe['stop'])} / "
             f"TP {_tp(best_sharpe['tp'])})\n")
    L.append("| sizing | CAGR | Sharpe | MaxDD | win% | trades |\n|---|--:|--:|--:|--:|--:|")
    for r in sub:
        L.append(f"| {int(r['pct'] * 100)}% × {r['max_pos']} | {r['cagr']:+.1%} | "
                 f"{r['sharpe']:.2f} | {r['max_dd']:.0%} | {r['win']:.0%} | {r['n']} |")
    return "\n".join(L) + "\n"


def main():
    print("Loading clean inputs...", flush=True)
    inp = cs.load_inputs()
    w = cs.full_window(inp)
    print(f"  SPY: CAGR {w.spy['cagr']:+.1%}  Sharpe {w.spy['sharpe']:.2f}  "
          f"MaxDD {w.spy['max_dd']:.1%}\n", flush=True)

    rows = _sweep(w)
    best_sharpe = max(rows, key=lambda r: r["sharpe"])
    sub = [_cell(w, best_sharpe["hold"], best_sharpe["stop"], best_sharpe["tp"],
                 w.spy["cagr"], pct, mp) for pct, mp in SIZINGS]

    rep = _report(rows, sub, w.spy, best_sharpe)
    _OUT.write_text(rep)
    print("\n" + "\n".join(rep.splitlines()[-24:]), flush=True)
    print(f"\nsaved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
