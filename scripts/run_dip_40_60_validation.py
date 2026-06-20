#!/usr/bin/env python3
"""
Dip range follow-up: full Stage 5b validation with 40-60% centered tier structure.

New tier structure:
  30-40% dip -> dip_score = 0.70  (approach zone)
  40-60% dip -> dip_score = 1.00  (sweet spot)
  60-70% dip -> dip_score = 0.50  (deep but possible)
  else       -> dip_score = 0.00

All other components (weights, gate, threshold, exit rule) are frozen.
Saves to results/recovery_40_60_validation.txt.
"""
from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── patch _dip_score_series BEFORE any other recovery_score import ─────────
import core.signals.recovery_score as _rsr


def _dip_40_60(close: pd.Series) -> pd.Series:
    high_52w     = close.rolling(252).max()
    drawdown_abs = ((high_52w - close) / high_52w).clip(lower=0)
    score        = pd.Series(np.nan, index=close.index, dtype=float)
    v            = drawdown_abs.notna()
    score[v & (drawdown_abs <  0.30)]                              = 0.0
    score[v & (drawdown_abs >= 0.30) & (drawdown_abs <  0.40)]    = 0.7
    score[v & (drawdown_abs >= 0.40) & (drawdown_abs <= 0.60)]    = 1.0
    score[v & (drawdown_abs >  0.60) & (drawdown_abs <= 0.70)]    = 0.5
    score[v & (drawdown_abs >  0.70)]                              = 0.0
    return score


_rsr._dip_score_series = _dip_40_60

# ── now safe to import everything that calls compute_recovery_signals ────────
from config.tickers import VALIDATION_UNIVERSE
from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from core.signals.recovery_score import BUY_THRESHOLD, WEIGHTS
from validation.recovery_backtest import RecoveryBacktester, RecoveryBacktestStats

# ── constants ─────────────────────────────────────────────────────────────────
_RANDOM_MEAN      = 0.223    # Stage 5b constant
_BASELINE_SPREAD  = 0.122    # Stage 5b HIGH spread — normalisation denominator
_BEAR_THRESHOLD   = 0.30     # decision rule: bear edge must exceed this
_SPREAD_DELTA     = 0.02     # decision rule: new spread must beat old by > 2pp

_RESULTS_DIR = Path(__file__).parent.parent / "results"

_LABEL = "40-60% tier test"


def _pct(v: float | None, w: int = 7) -> str:
    if v is None:
        return " " * (w - 4) + " N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1%}"


def _write_buckets(buf: StringIO, buckets: dict) -> tuple[bool, bool, float, float]:
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

    buf.write(f"\nSPREAD (HIGH - RANDOM 252d): {_pct(spread)}\n")

    c1 = (spread is not None and spread > 0.03)
    c2 = (pos_gap is not None and pos_gap > 0.05)
    buf.write(f"SUCCESS CRITERION 1 (HIGH-RANDOM > +3%):              {'PASS' if c1 else 'FAIL'}\n")
    buf.write(f"SUCCESS CRITERION 2 (HIGH %pos - RANDOM %pos > 5pp):  {'PASS' if c2 else 'FAIL'}\n")

    spread_val  = spread  if spread  is not None else 0.0
    pos_gap_val = pos_gap if pos_gap is not None else 0.0
    return c1, c2, spread_val, pos_gap_val


def _write_regime(buf: StringIO, regime: dict) -> tuple[float | None, float | None]:
    buf.write(f"\n{'Regime':<10} {'N':>6}  {'Mean12m':>8}  {'%Pos':>6}\n")
    buf.write("-" * 34 + "\n")
    bull_mean = bear_mean = None
    for r in ("bull", "bear"):
        d    = regime.get(r, {})
        n    = d.get("n", 0)
        mean = d.get("mean_12m")
        ppos = d.get("pct_positive")
        buf.write(f"{r:<10} {n:>6}  {_pct(mean):>8}  {_pct(ppos):>6}\n")
        if r == "bull":
            bull_mean = mean
        else:
            bear_mean = mean
    buf.write("-" * 34 + "\n")
    buf.write("Bull = SPY SMA200 rising vs 20 days prior.\n")
    return bull_mean, bear_mean


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


def _write_case_studies(buf: StringIO, cases: list[dict]) -> tuple[int, bool]:
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
    buf.write("MaxWin = max composite score in +-2-week window around the bottom date.\n")
    buf.write(f"BUY within +-2 weeks: {buy_count}/5\n")
    c3 = buy_count >= 3
    buf.write(f"SUCCESS CRITERION 3 (>=3/5 fired BUY within +-2 weeks): {'PASS' if c3 else 'FAIL'}\n")
    return buy_count, c3


def _write_ux(buf: StringIO, spread: float, drawdown: dict) -> float:
    pct_hit20 = drawdown.get("groups", {}).get("20", {}).get("touched", {}).get("pct", None)
    n_high    = drawdown.get("n_entries", 0)
    # pct_exited_at_loss comes from drawdown groups as "fwd_12m < 0"
    # we approximate from high stats: 1 - pct_positive_12m stored in drawdown analysis
    # The drawdown dict doesn't have pct_el directly; use touched pct as proxy
    # We compute pct_el from touched[touched_12m_negative] — not available directly.
    # Use the mae_p10 as indirect signal; report what we have.
    buf.write(f"\n  HIGH entry count:   {n_high}\n")
    if pct_hit20 is not None:
        buf.write(f"  %Hit-20% (MAE):    {pct_hit20:.1%}\n")
    return pct_hit20


def _write_drawdown(buf: StringIO, dd: dict, gate_none_dropped: int) -> None:
    n = dd.get("n_entries", 0)
    buf.write(
        f"\nHIGH entries (gate is True):               {n}\n"
        f"Entries removed by gate fix (None->blocked):  {gate_none_dropped}\n"
    )
    buf.write(
        f"\nMax adverse excursion from ENTRY price (252-day window):\n"
        f"  P10  (worst 10%)   {_pct(dd.get('mae_p10')):>8}\n"
        f"  P50  (median)      {_pct(dd.get('mae_median')):>8}\n"
        f"  P75               {_pct(dd.get('mae_p75')):>8}\n"
        f"  P90               {_pct(dd.get('mae_p90')):>8}\n"
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


def main() -> None:
    print(f"Universe: {len(VALIDATION_UNIVERSE)} tickers")
    print("Loading prices and EDGAR fundamentals...\n")
    print("Dip tier structure: 30-40%=0.70, 40-60%=1.00, 60-70%=0.50, else=0.00\n")

    prices       = PriceData()
    fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())
    backtester   = RecoveryBacktester(
        VALIDATION_UNIVERSE, prices, fundamentals,
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
    buf.write(f"RECOVERY ENTRY SCORE VALIDATION ({_LABEL})\n")
    buf.write("=" * 70 + "\n")
    buf.write("DIP TIER STRUCTURE:\n")
    buf.write("  30-40% drawdown -> dip_score = 0.70  (approach zone)\n")
    buf.write("  40-60% drawdown -> dip_score = 1.00  (sweet spot)\n")
    buf.write("  60-70% drawdown -> dip_score = 0.50  (deep but possible)\n")
    buf.write("  else            -> dip_score = 0.00\n")
    w_str = "  ".join(f"{k}={int(v*100)}%" for k, v in WEIGHTS.items())
    buf.write(f"Weights: {w_str}  |  BUY>={BUY_THRESHOLD}\n")

    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== BUCKET PERFORMANCE ===\n")
    buf.write("=" * 70 + "\n")
    c1, c2, spread, pos_gap = _write_buckets(buf, buckets)

    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== KNOWN RECOVERY CASE STUDIES ===\n")
    buf.write("=" * 70 + "\n")
    buy_count, c3 = _write_case_studies(buf, case_studies)

    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== ABLATION (component disabled -> frozen at 0.5) ===\n")
    buf.write("=" * 70 + "\n")
    _write_ablation(buf, ablation)

    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== REGIME BREAKDOWN (HIGH entries) ===\n")
    buf.write("=" * 70 + "\n")
    bull_mean, bear_mean = _write_regime(buf, regime)

    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== DRAWDOWN / MAE ANALYSIS (HIGH entries) ===\n")
    buf.write("=" * 70 + "\n")
    _write_drawdown(buf, drawdown, gate_none_dropped)

    # UX score
    pct_hit20 = drawdown.get("groups", {}).get("20", {}).get("touched", {}).get("pct")
    hi_stats  = buckets["HIGH"]
    pct_el    = (1.0 - hi_stats.pct_positive_12m) if hi_stats.pct_positive_12m is not None else None
    if spread and pct_hit20 is not None and pct_el is not None:
        ux = (spread / _BASELINE_SPREAD) * (1.0 - pct_hit20) * (1.0 - pct_el)
    else:
        ux = None

    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== SUCCESS SUMMARY ===\n")
    buf.write("=" * 70 + "\n")
    buf.write(f"Criterion 1 (spread > +3%):              {'PASS' if c1 else 'FAIL'}  ({_pct(spread)})\n")
    buf.write(f"Criterion 2 (%pos gap > 5pp):            {'PASS' if c2 else 'FAIL'}  ({_pct(pos_gap)})\n")
    buf.write(f"Criterion 3 (>=3/5 case studies BUY):    {'PASS' if c3 else 'FAIL'}  ({buy_count}/5)\n")
    buf.write(f"UX score:                                {'%.3f' % ux if ux else 'N/A'}\n")
    buf.write(f"Bull mean 252d:                          {_pct(bull_mean)}\n")
    buf.write(f"Bear mean 252d:                          {_pct(bear_mean)}\n")

    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== DECISION RULE (vs Stage 5b baseline) ===\n")
    buf.write("=" * 70 + "\n")
    buf.write("Stage 5b baseline spread:  +12.2%   bear: +30%+ required\n")
    buf.write(f"This run spread:           {_pct(spread)}\n")
    buf.write(f"This run bear mean:        {_pct(bear_mean)}\n")
    delta = spread - _BASELINE_SPREAD
    buf.write(f"Delta vs baseline:         {_pct(delta)}\n")

    beat_spread = delta > _SPREAD_DELTA
    beat_bear   = bear_mean is not None and bear_mean > _BEAR_THRESHOLD

    if beat_spread and beat_bear:
        buf.write(
            f"\nDECISION: ADOPT 40-60% TIER\n"
            f"  Spread beats baseline by {_pct(delta)} (> 2pp threshold).\n"
            f"  Bear edge {_pct(bear_mean)} > 30%.\n"
            f"  -> Update dip_score_series, re-document Stage 5b, proceed to Stage 6.\n"
        )
    elif beat_spread and not beat_bear:
        buf.write(
            f"\nDECISION: AWAIT — spread beats baseline ({_pct(delta)}) but bear edge "
            f"({_pct(bear_mean)}) does not clear 30%.\n"
        )
    else:
        buf.write(
            f"\nDECISION: KEEP 30-50% RANGE\n"
            f"  New spread ({_pct(spread)}) does not beat baseline by > 2pp (delta = {_pct(delta)}).\n"
            f"  Current range confirmed. Proceed to Stage 6.\n"
        )

    output = buf.getvalue()
    print(output)

    _RESULTS_DIR.mkdir(exist_ok=True)
    out_path = _RESULTS_DIR / "recovery_40_60_validation.txt"
    out_path.write_text(output)
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
