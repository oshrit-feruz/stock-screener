#!/usr/bin/env python3
"""
Walk-forward factor validation.

Runs point-in-time factor tests across VALIDATION_UNIVERSE for each
snapshot year, prints a spread table, and saves results to
results/validation_output.txt.
"""
import sys
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.tickers import SNAPSHOT_DATES, VALIDATION_UNIVERSE
from validation.factor_tests import FACTORS, evaluate_factors
from validation.walk_forward import WalkForwardEngine

_RESULTS_DIR = Path(__file__).parent.parent / "results"


def _fmt_spread(val: float | None) -> str:
    if val is None:
        return "   N/A  "
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1%}"


def main() -> None:
    print(f"Universe: {len(VALIDATION_UNIVERSE)} tickers")
    print(f"Snapshot dates: {SNAPSHOT_DATES}")
    print("Building snapshot DataFrame (this may take a while on first run)…\n")

    engine = WalkForwardEngine(VALIDATION_UNIVERSE, SNAPSHOT_DATES)
    df = engine.build_snapshot_df()

    print(f"Rows collected: {len(df)}  "
          f"({df['ticker'].nunique()} tickers × {df['snapshot_date'].nunique()} dates)\n")

    results = evaluate_factors(df)

    # ── header ────────────────────────────────────────────────────────────
    date_cols = sorted(df["snapshot_date"].unique())
    date_hdrs = "   ".join(str(d) for d in date_cols)
    header = f"{'Factor':<22} {date_hdrs}   Reliable?"
    divider = "-" * len(header)

    buf = StringIO()
    buf.write(header + "\n")
    buf.write(divider + "\n")

    for r in results:
        spreads_str = "   ".join(
            f"{_fmt_spread(r.spreads.get(str(d))):>10}" for d in date_cols
        )
        verdict = (
            f"YES ({r.positive_years}/{r.total_years})"
            if r.reliable
            else f"NO  ({r.positive_years}/{r.total_years})"
        )
        buf.write(f"{r.factor:<22} {spreads_str}   {verdict}\n")

    buf.write(divider + "\n")
    buf.write(
        "\nNote: D/E spread is direction-adjusted (low D/E = top decile).\n"
        "Positive spread = factor correctly predicted higher returns.\n"
        "Reliable = spread positive in ≥2/3 of snapshot years.\n"
    )

    output = buf.getvalue()
    print(output)

    _RESULTS_DIR.mkdir(exist_ok=True)
    out_path = _RESULTS_DIR / "validation_output.txt"
    out_path.write_text(output)
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
