#!/usr/bin/env python3
"""Calendar-year scorecard of the validated deployment vs SPY.

The rolling-window medians (run_spy_overlay / run_overlay_oos) are the honest
expectation; this is the plain "in which years did we beat the index, and by
how much" view people actually ask for. Same clean framework as everything
else (scripts/clean_sim.py: survivorship-free universe, next-session fills,
delisted names sold at their final print), validated config: SPY core, gate at
10% market drawdown, fixed 504-session hold, 10% x 10 sleeves.

    python scripts/run_overlay_by_year.py           # 2004-2024
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import clean_sim as cs  # noqa: E402

GATE, HOLD, PCT, MAXP = 0.10, 504, 0.10, 10
WIN_LEN = 5
_OUT = ROOT / "validation" / "overlay_by_year.md"


def yearly_returns(series: pd.Series) -> pd.Series:
    """Calendar-year returns from a daily value series (first year from day 1)."""
    year_end = series.groupby(series.index.year).last()
    prev = year_end.shift(1)
    prev.iloc[0] = series.iloc[0]
    return year_end / prev - 1


def build_table(bot: pd.Series, spy: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"bot": yearly_returns(bot), "spy": yearly_returns(spy)})
    df["excess"] = df["bot"] - df["spy"]
    # A year with no sleeve open is SPY exactly: neither a win nor a loss.
    df["status"] = ["= SPY" if abs(e) < 1e-9 else ("beat" if e > 0 else "lost")
                    for e in df["excess"]]
    return df


def report(df: pd.DataFrame, full: cs.Window, mb: dict, ms: dict, ev: dict) -> str:
    active = df[df["status"] != "= SPY"]
    beats = int((df["status"] == "beat").sum())
    idle = int((df["status"] == "= SPY").sum())
    L = [f"# Calendar-year scorecard — validated overlay vs SPY, {full.name}\n",
         "SPY core + dislocation gate 10% + fixed 504-session hold + 10%×10 sleeves. Clean "
         "universe, next-session fills, delisted names sold at their final print. No costs, "
         "taxes or slippage.\n",
         "| year | bot | SPY | excess (pp) | result |", "|---|--:|--:|--:|:--|"]
    for y, r in df.iterrows():
        L.append(f"| {y} | {r['bot']:+.1%} | {r['spy']:+.1%} | {r['excess'] * 100:+.1f} | "
                 f"{r['status']} |")
    L += ["\n## Read\n",
          f"- **Beat SPY in {beats} of {len(df)} calendar years**; {idle} years were identical "
          "to SPY because the market never fell 10% from its trailing high and no sleeve was "
          f"open. Of the {len(active)} years in which the overlay actually traded: "
          f"{beats} beat, {len(active) - beats} lost.",
          f"- Full period: bot CAGR {mb['cagr']:+.1%} vs SPY {ms['cagr']:+.1%} "
          f"({(mb['cagr'] - ms['cagr']) * 100:+.1f} pp/yr); total {mb['total']:+.0%} vs "
          f"{ms['total']:+.0%}; max drawdown {mb['max_dd']:.0%} vs {ms['max_dd']:.0%}.",
          f"- Yearly excess: mean {df['excess'].mean() * 100:+.1f} pp, median "
          f"{df['excess'].median() * 100:+.1f} pp — the edge is concentrated in a few "
          "recovery years, not spread evenly.",
          f"- Rolling {WIN_LEN}-year windows (the number to quote): beat SPY in "
          f"{ev['beats']}/{ev['n']}, median excess {ev['med_excess'] * 100:+.1f} pp/yr, "
          f"median Sharpe {ev['med_sharpe']:.2f}.",
          "- The losing years are crisis years themselves (2008, 2022) and one correction "
          "(2011): sleeves bought into a recovery that kept falling. Regime-dependent by "
          "construction."]
    return "\n".join(L) + "\n"


def main():
    print("Loading clean inputs...", flush=True)
    inp = cs.load_inputs()
    full = cs.full_window(inp)
    bot = cs.overlay_series(full, GATE, HOLD, PCT, MAXP)
    spy = full.spy["daily"]
    df = build_table(bot, spy)
    mb, ms = cs.metrics(bot, full.cal), cs.metrics(spy, full.cal)
    ev = cs.evaluate_rolling(cs.rolling_windows(inp, WIN_LEN),
                             lambda w: cs.overlay_series(w, GATE, HOLD, PCT, MAXP))
    rep = report(df, full, mb, ms, ev)
    _OUT.write_text(rep)
    print(rep)
    print(f"saved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
