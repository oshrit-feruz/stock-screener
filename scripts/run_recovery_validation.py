#!/usr/bin/env python3
"""
Recovery Entry Detector validation (Stage 5).

Backtests HIGH/LOW/RANDOM, ablation, known recovery case studies, regime breakdown.
Saves to results/recovery_entry_validation.txt.
"""
import sys
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.tickers import VALIDATION_UNIVERSE
from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from core.signals.recovery_score import BUY_THRESHOLD, LOW_THRESHOLD, WEIGHTS
from validation.recovery_backtest import CASE_STUDIES, RecoveryBacktestStats, RecoveryBacktester

_RESULTS_DIR = Path(__file__).parent.parent / "results"


def _pct(v: float | None) -> str:
    if v is None:
        return "   N/A "
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1%}"


def _write_buckets(buf: StringIO, buckets: dict) -> None:
    buf.write(
        f"\n{'Entry Type':<14} {'Fwd 21d':>8}  {'Fwd 63d':>8}  {'Fwd 252d':>9}  "
        f"{'%Pos12m':>9}  {'n entries':>10}\n"
    )
    buf.write("-" * 64 + "\n")
    for label in ("HIGH", "LOW", "RANDOM"):
        s: RecoveryBacktestStats = buckets[label]
        ppos = f"{s.pct_positive_12m:.1%}" if s.pct_positive_12m is not None else "   N/A "
        buf.write(
            f"{label:<14} {_pct(s.mean_return_21d):>8}  {_pct(s.mean_return_63d):>8}  "
            f"{_pct(s.mean_return_12m):>9}  {ppos:>9}  {s.n_entries:>10}\n"
        )
    buf.write("-" * 64 + "\n")

    hi = buckets["HIGH"]
    rn = buckets["RANDOM"]
    spread = (
        (hi.mean_return_12m - rn.mean_return_12m)
        if hi.mean_return_12m is not None and rn.mean_return_12m is not None else None
    )
    pos_gap = (
        (hi.pct_positive_12m - rn.pct_positive_12m)
        if hi.pct_positive_12m is not None and rn.pct_positive_12m is not None else None
    )

    buf.write(f"\nSPREAD (HIGH – RANDOM 252d): {_pct(spread)}\n")

    # Success criteria
    c1 = (spread is not None and spread > 0.03)
    c2 = (pos_gap is not None and pos_gap > 0.05)
    buf.write(f"SUCCESS CRITERION 1 (HIGH–RANDOM > +3%):              {'PASS' if c1 else 'FAIL'}\n")
    buf.write(f"SUCCESS CRITERION 2 (HIGH %pos – RANDOM %pos > 5pp):  {'PASS' if c2 else 'FAIL'}\n")
    buf.write(f"  [Replaced +10%/-10% capture metric: non-discriminative in both regimes\n")
    buf.write(f"   (bull gap -5.0pp, bear gap +0.3pp — see Stage 5b diagnostics)]\n")

    w_str = "  ".join(f"{k}={int(v*100)}%" for k, v in WEIGHTS.items())
    buf.write(
        f"\nWeights (theory-driven, NOT optimised): {w_str}\n"
        f"BUY threshold: {BUY_THRESHOLD}   LOW threshold: {LOW_THRESHOLD}\n"
    )
    return c1, c2


def _write_case_studies(buf: StringIO, cases: list[dict]) -> int:
    buf.write(
        f"\n{'Ticker':<7} {'Event':<30} {'Date':<12} {'Dip':>5}  {'Rec':>5}  "
        f"{'Comp':>5}  {'Gate':>5}  {'Signal':<7}  {'Fwd63d':>7}  {'MaxWin':>7}\n"
    )
    buf.write("-" * 98 + "\n")

    buy_count = 0
    for c in cases:
        dip  = f"{c['dip_score']:.2f}"   if c["dip_score"]   is not None else " N/A "
        rec  = f"{c['recovery_score']:.2f}" if c["recovery_score"] is not None else " N/A "
        comp = f"{c['composite']:.2f}"   if c["composite"]   is not None else " N/A "
        gate = ("yes" if c["gate"] else "no") if c["gate"] is not None else " ? "
        mw   = f"{c['max_window_score']:.2f}" if c["max_window_score"] is not None else " N/A "
        fwd  = _pct(c["fwd_63d"])
        buf.write(
            f"{c['ticker']:<7} {c['description']:<30} {str(c['date']):<12} {dip:>5}  {rec:>5}  "
            f"{comp:>5}  {gate:>5}  {c['signal']:<7}  {fwd:>7}  {mw:>7}\n"
        )
        if c.get("buy_in_window"):
            buy_count += 1

    buf.write("-" * 98 + "\n")
    buf.write("MaxWin = max composite score in ±2-week window around the bottom date.\n")
    buf.write(f"BUY within ±2 weeks: {buy_count}/5\n")

    c3 = buy_count >= 3
    buf.write(f"SUCCESS CRITERION 3 (≥3/5 fired BUY within ±2 weeks): {'PASS' if c3 else 'FAIL'}\n")
    return buy_count


def _write_ablation(buf: StringIO, ablation: dict) -> None:
    buf.write(
        f"\n{'Component disabled':<22} {'HIGH mean':>10}  {'LOW mean':>10}  "
        f"{'Spread':>8}  {'HIGH n':>7}  vs Baseline\n"
    )
    buf.write("-" * 72 + "\n")
    base   = ablation.get("none", {})
    base_h = base.get("high_mean")
    base_l = base.get("low_mean")
    base_s = (base_h - base_l) if (base_h is not None and base_l is not None) else None

    rows = [("none", "Baseline")] + [(k, f"{k.capitalize()} disabled") for k in WEIGHTS]
    for key, label in rows:
        row = ablation.get(key, {})
        hi  = row.get("high_mean")
        lo  = row.get("low_mean")
        n   = row.get("high_n", 0)
        sp  = (hi - lo) if (hi is not None and lo is not None) else None
        vs  = (_pct(sp - base_s) if (sp is not None and base_s is not None) else "   N/A ")
        buf.write(
            f"{label:<22} {_pct(hi):>10}  {_pct(lo):>10}  {_pct(sp):>8}  {n:>7}  {vs}\n"
        )
    buf.write("-" * 72 + "\n")


def _write_regime(buf: StringIO, regime: dict) -> None:
    buf.write(f"\n{'Regime':<10} {'N':>6}  {'Mean12m':>8}  {'%Pos':>6}\n")
    buf.write("-" * 34 + "\n")
    for r in ("bull", "bear"):
        d    = regime.get(r, {})
        n    = d.get("n", 0)
        mean = d.get("mean_12m")
        ppos = d.get("pct_positive")
        buf.write(f"{r:<10} {n:>6}  {_pct(mean):>8}  {_pct(ppos):>6}\n")
    buf.write("-" * 34 + "\n")
    buf.write("Bull = SPY SMA200 rising vs 20 days prior.\n")


def _write_drawdown(buf: StringIO, dd: dict, gate_none_dropped: int) -> None:
    n = dd.get("n_entries", 0)
    buf.write(
        f"\nHIGH entries (gate is True):               {n}\n"
        f"Entries removed by gate fix (None→blocked):  {gate_none_dropped}\n"
    )
    buf.write(
        f"\nMax adverse excursion from ENTRY price (252-day window):\n"
        f"  P10  (worst 10% of entries)  {_pct(dd.get('mae_p10')):>8}\n"
        f"  P50  (median)                {_pct(dd.get('mae_median')):>8}\n"
        f"  P75                          {_pct(dd.get('mae_p75')):>8}\n"
        f"  P90  (mildest decile)        {_pct(dd.get('mae_p90')):>8}\n"
        f"  (MAE < 0 means worst intraperiod price was below entry;\n"
        f"   MAE > 0 means entry price was never revisited downward)\n"
    )
    groups = dd.get("groups", {})
    buf.write(
        f"\n{'Threshold':<11} {'N touched':>10}  {'% HIGH':>7}  "
        f"{'252d (touched)':>15}  {'252d (not touched)':>19}\n"
    )
    buf.write("-" * 70 + "\n")
    for thresh, lbl in [("10", "  -10%"), ("15", "  -15%"), ("20", "  -20%")]:
        g  = groups.get(thresh, {})
        t  = g.get("touched",   {})
        u  = g.get("untouched", {})
        nt = t.get("n", 0)
        pc = t.get("pct", 0.0)
        buf.write(
            f"{lbl:<11} {nt:>10}  {pc:>7.1%}  "
            f"{_pct(t.get('fwd_12m')):>15}  {_pct(u.get('fwd_12m')):>19}\n"
        )
    buf.write("-" * 70 + "\n")
    buf.write(
        "'Touched' = entry price dropped to threshold vs entry at any point in\n"
        "the 252d window. Mean 252d return for touched vs never-touched shows\n"
        "the cost of applying a hard stop-loss at each level.\n"
        "If touched% = 0%: entries never revisit entry price minus that threshold\n"
        "within a year — hard stops at those levels would not fire.\n"
    )


def main(
    out_filename: str = "recovery_entry_validation.txt",
    label: str = "Stage 5",
    tickers: list[str] | None = None,
) -> None:
    universe = tickers if tickers is not None else VALIDATION_UNIVERSE
    print(f"Universe: {len(universe)} tickers")
    print("Loading price data and EDGAR fundamentals…\n")

    prices       = PriceData()
    fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())
    backtester   = RecoveryBacktester(
        universe, prices, fundamentals,
        start_date="2018-01-01", end_date="2024-12-31",
    )
    results = backtester.run()

    buckets           = results["buckets"]
    ablation          = results["ablation"]
    case_studies      = results["case_studies"]
    regime            = results["regime"]
    drawdown          = results["drawdown"]
    gate_none_dropped = results.get("gate_none_dropped", 0)

    buf = StringIO()

    buf.write("=" * 70 + "\n")
    buf.write(f"=== RECOVERY ENTRY SCORE VALIDATION ({label}) ===\n")
    buf.write("=" * 70 + "\n")
    w_str = "  ".join(f"{k}={int(v*100)}%" for k, v in WEIGHTS.items())
    buf.write(f"Weights: {w_str}  |  BUY≥{BUY_THRESHOLD}\n")

    # Section 1
    c1, c2 = _write_buckets(buf, buckets)

    # Section 2 — case studies
    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== KNOWN RECOVERY CASE STUDIES ===\n")
    buf.write("=" * 70 + "\n")
    buy_count = _write_case_studies(buf, case_studies)
    c3 = buy_count >= 3

    # Section 3 — ablation
    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== ABLATION (component disabled → frozen at 0.5) ===\n")
    buf.write("=" * 70 + "\n")
    _write_ablation(buf, ablation)

    # Section 4 — regime
    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== REGIME BREAKDOWN (HIGH entries) ===\n")
    buf.write("=" * 70 + "\n")
    _write_regime(buf, regime)

    # Section 5 — drawdown / stop-loss analysis
    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== DRAWDOWN DISTRIBUTION + STOP-LOSS ANALYSIS (HIGH entries) ===\n")
    buf.write("=" * 70 + "\n")
    _write_drawdown(buf, drawdown, gate_none_dropped)

    # Summary
    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== SUCCESS SUMMARY ===\n")
    buf.write("=" * 70 + "\n")
    buf.write(f"Criterion 1 (spread > +3%):              {'PASS' if c1 else 'FAIL'}\n")
    buf.write(f"Criterion 2 (%pos gap > 5pp):            {'PASS' if c2 else 'FAIL'}\n")
    buf.write(f"Criterion 3 (≥3/5 case studies BUY):     {'PASS' if c3 else 'FAIL'}\n")
    all_pass = c1 and c2 and c3
    buf.write(f"\nOverall: {'PROCEED TO STAGE 6' if all_pass else 'AWAIT DECISION'}\n")

    output = buf.getvalue()
    print(output)

    _RESULTS_DIR.mkdir(exist_ok=True)
    out_path = _RESULTS_DIR / out_filename
    out_path.write_text(output)
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
