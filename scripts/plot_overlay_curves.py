#!/usr/bin/env python3
"""Full-period equity + drawdown curves for the deployment variants vs SPY.

Four lines, same window, same clean signal universe, 2-year sleeves, 10%×10:
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

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(ROOT / ".env"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mt  # noqa: E402

import run_clean_pit_backtest as base  # noqa: E402
from core.data.eodhd import fetch_eod  # noqa: E402
from run_spy_overlay import simulate_overlay  # noqa: E402

START_YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2004
HOLD, PCT, MAXP, GATE = 504, 0.10, 10, 0.10


def main():
    data = base.build_intermediate()
    topn = base.top_n_by_rebal(data)
    elig = base.eligible_crossings(data, topn)
    spy = fetch_eod("SPY.US", base._FETCH_START, base._FETCH_END, adjust=True)
    cal = spy["Close"].index
    cal = cal[(cal >= pd.Timestamp(f"{START_YEAR}-01-01")) & (cal <= base._SIM_END)]
    elig = base.snap_events_to_calendar(elig, cal)
    panel = base._load_close_panel(sorted(elig), cal)
    spy_px = spy["Close"]
    s_full = spy_px.reindex(spy["Close"].index, method="ffill")
    dd_full = (s_full.rolling(252, min_periods=1).max() - s_full) / \
              s_full.rolling(252, min_periods=1).max()

    base._SIM_START = cal[0]
    standalone = base.simulate(elig, panel, cal, HOLD)["daily"]
    idle_spy = simulate_overlay(elig, panel, cal, spy_px, dd_full, HOLD, PCT, MAXP, 0.0)
    gated = simulate_overlay(elig, panel, cal, spy_px, dd_full, HOLD, PCT, MAXP, GATE)
    spy_s = spy_px.reindex(cal, method="ffill")
    spy_curve = spy_s / float(spy_s.iloc[0]) * base._INITIAL_CAP

    curves = [
        ("standalone (idle in cash)", standalone, "#9467bd", "-"),
        ("SPY when idle, no gate", idle_spy, "#1f77b4", "-"),
        (f"SPY core + gate {GATE:.0%} (validated)", gated, "#2ca02c", "-"),
        ("SPY buy & hold", spy_curve, "#7f7f7f", "--"),
    ]
    years = (cal[-1] - cal[0]).days / 365.25

    fig, (ax, axd) = plt.subplots(2, 1, figsize=(13, 9), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1.3]})
    for label, ser, color, ls in curves:
        m = base.metrics(ser, cal)
        ax.plot(ser.index, ser.values, ls, color=color, lw=1.6,
                label=f"{label} — final ${m['final']:,.0f}, CAGR {m['cagr']:+.1%}, "
                      f"Sharpe {m['sharpe']:.2f}, MaxDD {m['max_dd']:.0%}")
        ddc = (ser - ser.cummax()) / ser.cummax()
        axd.plot(ddc.index, ddc.values * 100, ls, color=color, lw=1.1)
    ax.axhline(base._INITIAL_CAP, color="black", lw=0.6, alpha=0.4)
    ax.set_yscale("log")
    ax.set_title(f"Deployment variants vs SPY — {cal[0].year}–{cal[-1].year} ({years:.0f}y), "
                 "clean universe, 2y sleeves, 10%×10")
    ax.set_ylabel("Portfolio value ($, log)")
    ax.yaxis.set_major_formatter(mt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax.legend(loc="upper left", fontsize=8.5)
    ax.grid(True, alpha=0.25, which="both")
    axd.set_ylabel("Drawdown (%)")
    axd.set_xlabel("Year")
    axd.grid(True, alpha=0.25)
    axd.axhline(0, color="black", lw=0.6)
    fig.tight_layout()
    out = ROOT / "validation" / f"overlay_curves_{cal[0].year}.png"
    fig.savefig(out, dpi=130)
    print(f"saved -> {out}")
    for label, ser, _, _ in curves:
        m = base.metrics(ser, cal)
        print(f"  {label:<34} final ${m['final']:>12,.0f}  CAGR {m['cagr']:+.1%}  "
              f"Sharpe {m['sharpe']:.2f}  MaxDD {m['max_dd']:.0%}")


if __name__ == "__main__":
    main()
