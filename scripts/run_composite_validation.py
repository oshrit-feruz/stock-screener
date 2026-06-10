#!/usr/bin/env python3
"""
Multi-factor composite scoring validation.

Runs individual factor recap, composite score (theory-driven weights),
factor momentum, and sensitivity tests across VALIDATION_UNIVERSE for
7 snapshot years (2018-2024). Saves to results/validation_composite.txt.
"""
import sys
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.tickers import SNAPSHOT_DATES, VALIDATION_UNIVERSE
from validation.composite import WEIGHTS
from validation.factor_tests import (
    FACTORS,
    CompositeResult,
    FactorResult,
    evaluate_composite,
    evaluate_factors,
)
from validation.walk_forward import WalkForwardEngine

_RESULTS_DIR = Path(__file__).parent.parent / "results"

_SENSITIVITY_WEIGHTS: dict[str, dict[str, float]] = {
    "A  Equal weights   (25/25/25/25)": {
        "revenue_growth_yoy": 0.25,
        "debt_to_equity":     0.25,
        "momentum_12m":       0.25,
        "roe":                0.25,
    },
    "B  Momentum-heavy  (10/20/50/20)": {
        "revenue_growth_yoy": 0.10,
        "debt_to_equity":     0.20,
        "momentum_12m":       0.50,
        "roe":                0.20,
    },
    "C  Quality-heavy   (40/30/10/20)": {
        "revenue_growth_yoy": 0.40,
        "debt_to_equity":     0.30,
        "momentum_12m":       0.10,
        "roe":                0.20,
    },
}


def _fmt(val: float | None, pct: bool = True) -> str:
    if val is None:
        return "   N/A  "
    if pct:
        sign = "+" if val >= 0 else ""
        return f"{sign}{val:.1%}"
    return f"{val:.1f}"


def _verdict(r: FactorResult | CompositeResult) -> str:
    label = "YES" if r.reliable else "NO "
    return f"{label} ({r.positive_years}/{r.total_years})"


def _write_individual(buf: StringIO, results: list[FactorResult], date_cols: list) -> None:
    date_hdrs = "   ".join(f"{str(d):>10}" for d in date_cols)
    header  = f"{'Factor':<30} {date_hdrs}   Reliable?"
    divider = "-" * len(header)
    buf.write(header + "\n")
    buf.write(divider + "\n")
    for r in results:
        spreads_str = "   ".join(
            f"{_fmt(r.spreads.get(str(d))):>10}" for d in date_cols
        )
        buf.write(f"{r.factor:<30} {spreads_str}   {_verdict(r)}\n")
        counts_str = "   ".join(
            f"{'n='+str(r.valid_rows.get(str(d), 0)):>10}" for d in date_cols
        )
        buf.write(f"{'':30} {counts_str}\n")
    buf.write(divider + "\n")


def _write_composite(buf: StringIO, cr: CompositeResult, date_cols: list, label: str = "") -> None:
    buf.write(f"\n{'Year':<12} {'Top decile':>12} {'Bot decile':>12} {'Spread':>10} {'n':>6}\n")
    buf.write("-" * 56 + "\n")
    for d in date_cols:
        ds = str(d)
        spread = cr.spreads.get(ds)
        top    = cr.top_means.get(ds)
        bot    = cr.bottom_means.get(ds)
        n      = cr.valid_rows.get(ds, 0)
        buf.write(
            f"{ds:<12} {_fmt(top):>12} {_fmt(bot):>12} {_fmt(spread):>10} {n:>6}\n"
        )
    buf.write("-" * 56 + "\n")
    spreads_valid = [v for v in cr.spreads.values() if v is not None]
    avg = sum(spreads_valid) / len(spreads_valid) if spreads_valid else None
    buf.write(f"Reliable: {_verdict(cr)}   Avg spread: {_fmt(avg)}\n")


def main() -> None:
    print(f"Universe: {len(VALIDATION_UNIVERSE)} tickers")
    print(f"Snapshot dates: {SNAPSHOT_DATES}")
    print("Building snapshot DataFrame…\n")

    engine = WalkForwardEngine(VALIDATION_UNIVERSE, SNAPSHOT_DATES)
    df = engine.build_snapshot_df()
    print(f"Rows collected: {len(df)}  "
          f"({df['ticker'].nunique()} tickers × {df['snapshot_date'].nunique()} dates)\n")

    date_cols = sorted(df["snapshot_date"].unique())

    buf = StringIO()

    # ── 1. Individual factors (recap) ────────────────────────────────────────
    buf.write("=" * 80 + "\n")
    buf.write("=== INDIVIDUAL FACTORS (recap) ===\n")
    buf.write("=" * 80 + "\n\n")

    ind_results = evaluate_factors(df)
    _write_individual(buf, ind_results, date_cols)
    buf.write(
        "\nNote: D/E spread is direction-adjusted (low D/E = top decile).\n"
        "Reliable = spread positive in ≥2/3 of snapshot years.\n"
    )

    # ── 2. Composite score ───────────────────────────────────────────────────
    w_str = "  ".join(f"{k.replace('_yoy','').replace('_12m','')}: {int(v*100)}%" for k, v in WEIGHTS.items())
    buf.write(f"\n{'=' * 80}\n")
    buf.write(f"=== COMPOSITE SCORE  ({w_str}) ===\n")
    buf.write("=" * 80 + "\n")
    buf.write("Weights are theory-driven; NOT fitted to this data.\n")
    buf.write("Reliability threshold: ≥5/7 years (stricter than individual factors).\n")

    comp_result = evaluate_composite(df)
    _write_composite(buf, comp_result, date_cols)

    # ── 3. Factor momentum ───────────────────────────────────────────────────
    buf.write(f"\n{'=' * 80}\n")
    buf.write("=== FACTOR MOMENTUM ===\n")
    buf.write("=" * 80 + "\n\n")
    buf.write("Measures whether improving fundamentals (acceleration) predict returns.\n\n")

    fm_factors = {k: v for k, v in FACTORS.items()
                  if k in ("revenue_growth_acceleration", "margin_improvement")}
    fm_results = [r for r in ind_results if r.factor in fm_factors]
    _write_individual(buf, fm_results, date_cols)

    # ── 4. Sensitivity test ──────────────────────────────────────────────────
    buf.write(f"\n{'=' * 80}\n")
    buf.write("=== SENSITIVITY TEST ===\n")
    buf.write("=" * 80 + "\n")
    buf.write("Purpose: verify composite signal is not fragile to weight choices.\n")
    buf.write("Similar spreads across A/B/C → robust.  Very different → weight-sensitive.\n\n")

    date_hdrs = "   ".join(f"{str(d):>10}" for d in date_cols)
    buf.write(f"{'Scheme':<40} {date_hdrs}   Reliable?\n")
    buf.write("-" * (40 + 13 * len(date_cols) + 12) + "\n")

    # Baseline (theory weights)
    baseline_row = "   ".join(
        f"{_fmt(comp_result.spreads.get(str(d))):>10}" for d in date_cols
    )
    buf.write(f"{'Baseline (30/25/25/20)':<40} {baseline_row}   {_verdict(comp_result)}\n")

    for scheme_name, scheme_weights in _SENSITIVITY_WEIGHTS.items():
        cr = evaluate_composite(df, weights=scheme_weights)
        row_str = "   ".join(
            f"{_fmt(cr.spreads.get(str(d))):>10}" for d in date_cols
        )
        buf.write(f"{scheme_name:<40} {row_str}   {_verdict(cr)}\n")

    buf.write("\n")
    buf.write("Interpretation: if all schemes show similar pattern, signal is robust.\n")

    output = buf.getvalue()
    print(output)

    _RESULTS_DIR.mkdir(exist_ok=True)
    out_path = _RESULTS_DIR / "validation_composite.txt"
    out_path.write_text(output)
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
