#!/usr/bin/env python3
"""
Entry Quality Score validation.

Backtests the BUY signal across 50 tickers × 2018-2024.
Sections: (1) HIGH vs LOW vs RANDOM, (2) per-component ablation,
          (3) exit strategies, (4) regime breakdown.
Saves to results/entry_validation.txt.
"""
import sys
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.tickers import VALIDATION_UNIVERSE
from core.data.prices import PriceData
from core.signals.entry_score import BUY_THRESHOLD, LOW_THRESHOLD, WEIGHTS
from validation.entry_backtest import BacktestStats, EntryBacktester

_RESULTS_DIR = Path(__file__).parent.parent / "results"
_START_DATE  = "2018-01-01"
_END_DATE    = "2024-12-31"


def _pct(v: float | None) -> str:
    if v is None:
        return "    N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1%}"


def _write_buckets(buf: StringIO, buckets: dict[str, BacktestStats]) -> None:
    w_str = "/".join(f"{int(w*100)}%" for w in WEIGHTS.values())
    buf.write(f"\n{'Bucket':<10} {'N':>6}  {'Mean12m':>8}  {'Med12m':>8}  "
              f"{'%Pos':>6}  {'AvgMaxDD':>9}\n")
    buf.write("-" * 56 + "\n")
    for label in ("HIGH", "LOW", "RANDOM"):
        s = buckets[label]
        buf.write(
            f"{label:<10} {s.n_entries:>6}  "
            f"{_pct(s.mean_return_12m):>8}  "
            f"{_pct(s.median_return_12m):>8}  "
            f"{_pct(s.pct_positive_12m):>6}  "
            f"{_pct(s.mean_max_drawdown):>9}\n"
        )
    buf.write("-" * 56 + "\n")
    hi = buckets["HIGH"]
    lo = buckets["LOW"]
    if hi.mean_return_12m is not None and lo.mean_return_12m is not None:
        spread = hi.mean_return_12m - lo.mean_return_12m
        buf.write(f"HIGH–LOW spread: {_pct(spread)}\n")
    buf.write(
        f"\nWeights (theory-driven, NOT optimised): "
        f"trend={int(WEIGHTS['trend']*100)}%  "
        f"momentum={int(WEIGHTS['momentum']*100)}%  "
        f"volume={int(WEIGHTS['volume']*100)}%  "
        f"rsi={int(WEIGHTS['rsi']*100)}%\n"
        f"BUY threshold: {BUY_THRESHOLD}   LOW threshold: {LOW_THRESHOLD}\n"
    )


def _write_ablation(buf: StringIO, ablation: dict) -> None:
    buf.write(f"\n{'Component disabled':<22} {'HIGH mean':>10}  {'LOW mean':>10}  "
              f"{'Spread':>8}  {'HIGH n':>7}  vs Baseline\n")
    buf.write("-" * 72 + "\n")
    baseline = ablation.get("none", {})
    base_hi  = baseline.get("high_mean")
    base_lo  = baseline.get("low_mean")
    base_spread = (base_hi - base_lo) if (base_hi is not None and base_lo is not None) else None

    for key, label in [
        ("none",     "Baseline"),
        ("trend",    "Trend disabled"),
        ("momentum", "Momentum disabled"),
        ("volume",   "Volume disabled"),
        ("rsi",      "RSI disabled"),
    ]:
        row = ablation.get(key, {})
        hi  = row.get("high_mean")
        lo  = row.get("low_mean")
        n   = row.get("high_n", 0)
        spread = (hi - lo) if (hi is not None and lo is not None) else None
        vs_base = (spread - base_spread) if (spread is not None and base_spread is not None) else None
        vs_str  = _pct(vs_base) if vs_base is not None else "    N/A"
        buf.write(
            f"{label:<22} {_pct(hi):>10}  {_pct(lo):>10}  "
            f"{_pct(spread):>8}  {n:>7}  {vs_str}\n"
        )
    buf.write("-" * 72 + "\n")


def _write_exits(buf: StringIO, buckets: dict[str, BacktestStats]) -> None:
    hi = buckets["HIGH"]
    buf.write(f"\n{'Strategy':<22} {'Mean return':>12}  {'N':>6}\n")
    buf.write("-" * 44 + "\n")
    buf.write(f"{'Fixed 12m':<22} {_pct(hi.mean_return_12m):>12}  {hi.n_entries:>6}\n")
    buf.write(f"{'Fixed 6m':<22} {_pct(hi.mean_return_6m):>12}  {hi.n_entries:>6}\n")
    buf.write(f"{'ATR 3× trailing':<22} {_pct(hi.mean_return_atr):>12}  {hi.n_entries:>6}\n")
    buf.write("-" * 44 + "\n")


def _write_regime(buf: StringIO, regime: dict) -> None:
    buf.write(f"\n{'Regime':<10} {'N':>6}  {'Mean12m':>8}  {'%Pos':>6}\n")
    buf.write("-" * 34 + "\n")
    for r_label in ("bull", "bear"):
        d = regime.get(r_label, {})
        n    = d.get("n", 0)
        mean = d.get("mean_12m")
        ppos = d.get("pct_positive")
        buf.write(f"{r_label:<10} {n:>6}  {_pct(mean):>8}  {_pct(ppos):>6}\n")
    buf.write("-" * 34 + "\n")
    buf.write("Bull = SPY SMA200 rising vs 20 days prior.\n")


def main() -> None:
    print(f"Universe: {len(VALIDATION_UNIVERSE)} tickers")
    print(f"Backtest: {_START_DATE} → {_END_DATE}")
    print("Loading price data and computing signals…\n")

    prices     = PriceData()
    backtester = EntryBacktester(VALIDATION_UNIVERSE, prices, _START_DATE, _END_DATE)
    results    = backtester.run()

    buckets  = results["buckets"]
    ablation = results["ablation"]
    regime   = results["regime"]

    buf = StringIO()

    # ── Section 1 ────────────────────────────────────────────────────────────
    buf.write("=" * 70 + "\n")
    buf.write("=== SECTION 1: HIGH vs LOW vs RANDOM ENTRY COMPARISON (12m) ===\n")
    buf.write("=" * 70 + "\n")
    _write_buckets(buf, buckets)

    # ── Section 2 ────────────────────────────────────────────────────────────
    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== SECTION 2: PER-COMPONENT ABLATION ===\n")
    buf.write("=" * 70 + "\n")
    buf.write("Each component disabled in turn (frozen at neutral 0.5).\n")
    _write_ablation(buf, ablation)

    # ── Section 3 ────────────────────────────────────────────────────────────
    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== SECTION 3: EXIT STRATEGY COMPARISON (HIGH entries only) ===\n")
    buf.write("=" * 70 + "\n")
    _write_exits(buf, buckets)

    # ── Section 4 ────────────────────────────────────────────────────────────
    buf.write("\n" + "=" * 70 + "\n")
    buf.write("=== SECTION 4: REGIME BREAKDOWN (HIGH entries) ===\n")
    buf.write("=" * 70 + "\n")
    _write_regime(buf, regime)

    output = buf.getvalue()
    print(output)

    _RESULTS_DIR.mkdir(exist_ok=True)
    out_path = _RESULTS_DIR / "entry_validation.txt"
    out_path.write_text(output)
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
