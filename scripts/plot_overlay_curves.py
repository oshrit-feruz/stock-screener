#!/usr/bin/env python3
"""Full-period equity + drawdown curves for the deployment variants vs SPY.

Four lines, same window, same clean signal universe, 2-year sleeves, 10%×10,
next-session fills (scripts/clean_sim.py):
  standalone     — original: signal only, idle money in CASH (the cash-drag case)
  SPY-when-idle  — fix A: idle money parked in SPY, signal fires in all regimes
  + gate 10%     — fix A+B: signal fires only when market DD >= 10% (validated)
  SPY            — pure buy-and-hold benchmark

    python scripts/plot_overlay_curves.py            # 2004-2024 (~21y)
    python scripts/plot_overlay_curves.py 2008       # 2008-2024 (17y)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import clean_sim as cs  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mt  # noqa: E402

START_YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2004
HOLD, PCT, MAXP, GATE = 504, 0.10, 10, 0.10


def main():
    inp = cs.load_inputs(f"{START_YEAR}-01-01")
    w = cs.full_window(inp)
    curves = [
        ("standalone (idle in cash)",
         cs.run(w.mkt, w.events, cs.SimConfig(hold=HOLD, pct=PCT, max_pos=MAXP)).daily,
         "#9467bd", "-"),
        ("SPY when idle, no gate", cs.overlay_series(w, None, HOLD, PCT, MAXP), "#1f77b4", "-"),
        (f"SPY core + gate {GATE:.0%} (validated)", cs.overlay_series(w, GATE, HOLD, PCT, MAXP),
         "#2ca02c", "-"),
        ("SPY buy & hold", w.spy["daily"], "#7f7f7f", "--"),
    ]
    years = (w.cal[-1] - w.cal[0]).days / 365.25

    fig, (ax, axd) = plt.subplots(2, 1, figsize=(13, 9), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1.3]})
    for label, ser, color, ls in curves:
        m = cs.metrics(ser, w.cal)
        ax.plot(ser.index, ser.values, ls, color=color, lw=1.6,
                label=f"{label} — final ${m['final']:,.0f}, CAGR {m['cagr']:+.1%}, "
                      f"Sharpe {m['sharpe']:.2f}, MaxDD {m['max_dd']:.0%}")
        ddc = (ser - ser.cummax()) / ser.cummax()
        axd.plot(ddc.index, ddc.values * 100, ls, color=color, lw=1.1)
        print(f"  {label:<34} final ${m['final']:>12,.0f}  CAGR {m['cagr']:+.1%}  "
              f"Sharpe {m['sharpe']:.2f}  MaxDD {m['max_dd']:.0%}")
    ax.axhline(cs.INITIAL_CAP, color="black", lw=0.6, alpha=0.4)
    ax.set_yscale("log")
    ax.set_title(f"Deployment variants vs SPY — {w.cal[0].year}–{w.cal[-1].year} "
                 f"({years:.0f}y), clean universe, 2y sleeves, 10%×10, next-session fills")
    ax.set_ylabel("Portfolio value ($, log)")
    ax.yaxis.set_major_formatter(mt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax.legend(loc="upper left", fontsize=8.5)
    ax.grid(True, alpha=0.25, which="both")
    axd.set_ylabel("Drawdown (%)")
    axd.set_xlabel("Year")
    axd.grid(True, alpha=0.25)
    axd.axhline(0, color="black", lw=0.6)
    fig.tight_layout()
    out = ROOT / "validation" / f"overlay_curves_{w.cal[0].year}.png"
    fig.savefig(out, dpi=130)
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
