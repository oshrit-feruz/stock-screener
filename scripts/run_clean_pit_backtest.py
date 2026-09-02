#!/usr/bin/env python3
"""Clean point-in-time backtest — survivorship-bias-free, long window.

Universe / signal: see scripts/clean_intermediate.py (PIT S&P 500 top-100 by
trailing dollar-volume, delisted names included, pure price signal, no gate).
Simulation: see scripts/clean_sim.py (next-session fills, forced exit at a
name's final print, completion rule, one SPY master calendar).

Sizing / exits: V1 (10% of portfolio value per signal, max 10 concurrent, no
stop-loss, idle money in cash). Three holding periods compared — 252 / 378 /
504 trading days.

    python scripts/run_clean_pit_backtest.py              # 2000-01-03 .. 2024-12-31
    python scripts/run_clean_pit_backtest.py 2004-01-02   # hermetic window (>=85% coverage)

Outputs: comparison table, per-trade return distribution by holding bucket, and
a single portfolio-value chart of the three holds + SPY.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import clean_intermediate as ci  # noqa: E402
import clean_sim as cs  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402

# 2004+ is the "hermetic" window where ranking coverage is >=85%; 2000 includes
# the dot-com crash but with only ~77% early coverage.
_START = sys.argv[1] if len(sys.argv) > 1 else "2000-01-03"
_HOLDS = [252, 378, 504]
_OUT_DIR = ROOT / "validation"


def _out_paths(cal) -> tuple[Path, Path]:
    return (_OUT_DIR / f"clean_pit_portfolio_{cal[0].year}.png",
            _OUT_DIR / f"clean_pit_backtest_{cal[0].year}.md")


# ── Report ────────────────────────────────────────────────────────────────────

def _summary_table(results, mets, stats, spy_m) -> list[str]:
    L = ["## Summary\n", "| Metric | H252 | H378 | H504 | SPY |", "|---|--:|--:|--:|--:|"]

    def row(label, cells, spy_cell="—"):
        L.append("| " + label + " | " + " | ".join(cells) + " | " + spy_cell + " |")

    row("Final value", [f"${m['final']:,.0f}" for m in mets], f"${spy_m['final']:,.0f}")
    row("Total return", [f"{m['total']:+.1%}" for m in mets], f"{spy_m['total']:+.1%}")
    row("CAGR", [f"{m['cagr']:+.1%}" for m in mets], f"{spy_m['cagr']:+.1%}")
    row("Sharpe", [f"{m['sharpe']:.2f}" for m in mets], f"{spy_m['sharpe']:.2f}")
    row("Max drawdown", [f"{m['max_dd']:.1%}" for m in mets], f"{spy_m['max_dd']:.1%}")
    row("Avg trade", [f"{s['avg']:+.1%}" for s in stats])
    row("Median trade", [f"{s['median']:+.1%}" for s in stats])
    row("Win rate", [f"{s['win']:.0%}" for s in stats])
    row("Trades", [f"{s['n']}" for s in stats])
    row("…exited at final print (delisted)", [f"{r.exits.get('delist', 0)}" for r in results])
    row("Excluded (late)", [f"{r.excluded}" for r in results])
    return L


def _bucket_table(stats) -> list[str]:
    L = ["\n## Return distribution by holding bucket (share of trades)\n",
         "| Bucket | H252 | H378 | H504 |\n|---|--:|--:|--:|"]
    for name, _ in cs.BUCKETS:
        cells = [f"{(s['buckets'][name] / s['n']):.0%}" if s["n"] else "—" for s in stats]
        L.append("| " + name + " | " + " | ".join(cells) + " |")
    return L


def _interpretation(mets, stats, spy_m, cal) -> list[str]:
    """Data-driven read of the table (no hardcoded numbers or conclusions)."""
    n_years = mets[0]["n_years"]
    y0 = cal[0].year
    cagrs = [m["cagr"] for m in mets]
    wins = [s["win"] for s in stats]
    tails = [s["buckets"][">100%"] / s["n"] if s["n"] else 0.0 for s in stats]
    monotone = all(a <= b for a, b in zip(cagrs, cagrs[1:])) and \
        all(a <= b for a, b in zip(wins, wins[1:]))
    beats = [f"H{h}" for h, m in zip(_HOLDS, mets) if m["cagr"] > spy_m["cagr"]]
    loses = [f"H{h}" for h, m in zip(_HOLDS, mets) if m["cagr"] <= spy_m["cagr"]]
    crash_txt = "2002 and 2008-09" if y0 <= 2002 else "2008-09"

    L = [f"## Interpretation (bias-free, ~{n_years:.0f} years)\n"]
    seq = " -> ".join(f"{c:+.1%}" for c in cagrs)
    wseq = " -> ".join(f"{w:.0%}" for w in wins)
    tseq = " -> ".join(f"{t:.0%}" for t in tails)
    if monotone:
        L.append("**The holding-period effect holds on the clean universe.** CAGR "
                 f"{seq}, win rate {wseq} and the fat right tail (>100% trades) {tseq} "
                 "all improve with hold length (H252 -> H378 -> H504).")
    else:
        L.append("**The holding-period effect is NOT monotonic on the clean universe.** "
                 f"CAGR {seq}, win rate {wseq}, >100% share {tseq} (H252 -> H378 -> H504).")
    L.append(f"Trades: {' / '.join(str(s['n']) for s in stats)} over ~{n_years:.0f} years.\n")
    beat_txt = (f"{', '.join(beats)} beat SPY ({spy_m['cagr']:+.1%} CAGR)"
                if beats else "no hold beats SPY")
    lose_txt = f"; {', '.join(loses)} lose to it" if loses and beats else ""
    L.append(f"**Versus SPY:** {beat_txt}{lose_txt}. The earlier 50-stock 2018-2024 "
             "test showed every variant crushing SPY — that was survivorship / "
             "selection bias.\n")
    L.append("**Tail risk is severe.** Max drawdowns of "
             f"{min(m['max_dd'] for m in mets):.0%}..{max(m['max_dd'] for m in mets):.0%} "
             f"(vs SPY {spy_m['max_dd']:.0%}) — a concentrated, no-stop, dip-buying book "
             f"got destroyed in {crash_txt}. The 2018-2024 window never contained a 2008.\n")
    L.append("### Method + data caveats\n"
             "- **Fills:** a signal computed on a bar's close fills at the NEXT session's "
             "close; the hold clock runs from the fill (no same-bar look-ahead).\n"
             "- **Delistings:** a name that stops trading mid-hold is sold at its final "
             "print (counted above), never carried at a stale quote.\n"
             "- **Size proxy:** ranked by dollar-volume, not exact market cap (unavailable "
             "free pre-2015). Highly correlated for mega-caps, not identical.\n"
             "- **No fundamental gate:** pure price signal (the gate needs EDGAR data that "
             "would re-introduce survivorship bias over 25y).\n"
             "- **Fetch coverage:** 1054/1090 ever-members fetched (96.7%); FB recovered by "
             "aliasing FB->META. The ~36 missing are mostly bankruptcies/acquisitions with "
             "no free continuing series (LEHMQ, RSHCQ, AAMRQ, APC, CAM, PCL...). Some of "
             "those would have been big *losers* had the signal fired on them, so their "
             "absence may slightly *flatter* results.\n")
    return L


def report(results, mets, stats, spy_m, cal, png_name: str) -> str:
    n_years = mets[0]["n_years"]
    L = [f"# Clean point-in-time backtest — S&P 500, {cal[0].year}-{cal[-1].year}\n",
         f"Window **{cal[0].date()}..{cal[-1].date()}** "
         f"({len(cal)} trading days, ~{n_years:.1f}y). Start ${cs.INITIAL_CAP:,.0f}.\n",
         "**Universe (clean):** PIT S&P 500 membership (incl. delisted), ranked each "
         f"quarter by trailing {ci.DV_WINDOW}-day median dollar-volume, top {ci.TOP_N}. "
         "No survivorship bias, no lookahead.\n",
         "**Signal:** recovery composite >= 0.60, **no fundamental gate** (pure price "
         "signal). **Sizing:** V1 — 10%/signal, max 10, no stop-loss, next-session fills. "
         "Late signals that cannot complete the hold are excluded.\n"]
    L += _summary_table(results, mets, stats, spy_m)
    L += _bucket_table(stats)
    L.append(f"\n![portfolio]({png_name})\n")
    L += _interpretation(mets, stats, spy_m, cal)
    return "\n".join(L)


def make_plot(results, spy_m, cal, png: Path) -> None:
    fig, ax = plt.subplots(figsize=(12, 7))
    colors = {252: "#1f77b4", 378: "#ff7f0e", 504: "#2ca02c"}
    for h, res in zip(_HOLDS, results):
        ax.plot(res.daily.index, res.daily.values, color=colors[h], linewidth=1.4,
                label=f"H{h} (final ${res.final:,.0f})")
    ax.plot(spy_m["daily"].index, spy_m["daily"].values, "--", color="#7f7f7f",
            linewidth=1.3, label=f"SPY (final ${spy_m['final']:,.0f})")
    ax.axhline(cs.INITIAL_CAP, color="black", linewidth=0.6, alpha=0.4)
    ax.set_yscale("log")
    ax.set_title("Clean PIT top-100 (dollar-volume) — holding periods vs SPY, "
                 f"{cal[0].year}-{cal[-1].year}")
    ax.set_ylabel("Portfolio value ($, log)")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.25, which="both")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    fig.tight_layout()
    fig.savefig(png, dpi=130)
    print(f"  chart -> {png}", flush=True)


def main() -> None:
    print("Loading clean inputs...", flush=True)
    inp = cs.load_inputs(_START)
    w = cs.full_window(inp)
    png, md = _out_paths(w.cal)

    results, mets, stats = [], [], []
    for h in _HOLDS:
        res = cs.run(w.mkt, w.events, cs.SimConfig(hold=h))
        m = cs.metrics(res.daily, w.cal)
        s = cs.trade_stats(res.trades)
        results.append(res)
        mets.append(m)
        stats.append(s)
        print(f"  H{h}: trades={s['n']} delist={res.exits.get('delist', 0)} "
              f"excl={res.excluded} final=${m['final']:,.0f} CAGR={m['cagr']:+.1%} "
              f"sharpe={m['sharpe']:.2f} win={s['win']:.0%}", flush=True)

    make_plot(results, w.spy, w.cal, png)
    rep = report(results, mets, stats, w.spy, w.cal, png.name)
    md.write_text(rep)
    print(f"  report -> {md}\n", flush=True)
    print("=" * 70, flush=True)
    print(rep, flush=True)


if __name__ == "__main__":
    main()
