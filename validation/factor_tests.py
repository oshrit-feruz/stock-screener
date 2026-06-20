from dataclasses import dataclass, field
from math import ceil

import pandas as pd

from validation.composite import build_composite_df

# Direction: +1 = higher value is better signal, -1 = lower value is better.
# D/E is inverted so that all spreads are directionally comparable:
# positive spread always means "factor predicted correctly".
FACTORS: dict[str, int] = {
    "revenue_growth_yoy":           1,
    "debt_to_equity":              -1,
    "roe":                          1,
    "net_margin":                   1,
    "momentum_12m":                 1,
    "momentum_6m":                  1,
    "revenue_growth_acceleration":  1,
    "margin_improvement":           1,
}

_MIN_ROWS = 20  # minimum valid rows to compute a meaningful decile spread
_COMPOSITE_RELIABILITY = 5 / 7  # stricter threshold for composite


@dataclass
class FactorResult:
    factor: str
    spreads: dict[str, float | None] = field(default_factory=dict)
    valid_rows: dict[str, int] = field(default_factory=dict)   # rows used per year
    positive_years: int = 0
    total_years: int = 0
    reliable: bool = False


def _year_spread(df_year: pd.DataFrame, factor: str, direction: int) -> tuple[float | None, int]:
    if factor not in df_year.columns:
        return None, 0
    sub = df_year[["forward_return_12m", factor]].dropna()
    n = len(sub)
    if n < _MIN_ROWS:
        return None, n

    decile_n = max(1, ceil(n * 0.10))

    ranked = sub.sort_values(factor, ascending=(direction == -1))
    top    = ranked.head(decile_n)["forward_return_12m"].mean()
    bottom = ranked.tail(decile_n)["forward_return_12m"].mean()
    return float(top - bottom), n


def evaluate_factors(df: pd.DataFrame) -> list[FactorResult]:
    results: list[FactorResult] = []

    for factor, direction in FACTORS.items():
        result = FactorResult(factor=factor)

        for snap_date, group in df.groupby("snapshot_date"):
            date_str = str(snap_date)
            spread, n = _year_spread(group, factor, direction)
            result.spreads[date_str] = spread
            result.valid_rows[date_str] = n
            if spread is not None:
                result.total_years += 1
                if spread > 0:
                    result.positive_years += 1

        if result.total_years > 0:
            result.reliable = (result.positive_years / result.total_years) >= (2 / 3)

        results.append(result)

    return results


@dataclass
class CompositeResult:
    spreads: dict[str, float | None] = field(default_factory=dict)
    top_means: dict[str, float | None] = field(default_factory=dict)
    bottom_means: dict[str, float | None] = field(default_factory=dict)
    valid_rows: dict[str, int] = field(default_factory=dict)
    positive_years: int = 0
    total_years: int = 0
    reliable: bool = False


def evaluate_composite(df: pd.DataFrame, weights: dict[str, float] | None = None) -> CompositeResult:
    """Rank stocks by composite score; return top vs bottom decile spread per year.

    If weights is supplied it overrides the defaults in composite.WEIGHTS, allowing
    sensitivity tests without modifying module state.
    """

    # Temporarily swap weights if an override is given
    if weights is not None:
        import validation.composite as _cm
        _orig = dict(_cm.WEIGHTS)
        _cm.WEIGHTS.clear()
        _cm.WEIGHTS.update(weights)

    try:
        scored = build_composite_df(df)
    finally:
        if weights is not None:
            _cm.WEIGHTS.clear()
            _cm.WEIGHTS.update(_orig)

    result = CompositeResult()

    for snap_date, group in scored.groupby("snapshot_date"):
        date_str = str(snap_date)
        sub = group[["forward_return_12m", "composite_score"]].dropna()
        n = len(sub)
        result.valid_rows[date_str] = n

        if n < _MIN_ROWS:
            result.spreads[date_str] = None
            result.top_means[date_str] = None
            result.bottom_means[date_str] = None
            continue

        decile_n = max(1, ceil(n * 0.10))
        ranked   = sub.sort_values("composite_score", ascending=False)
        top_ret  = ranked.head(decile_n)["forward_return_12m"].mean()
        bot_ret  = ranked.tail(decile_n)["forward_return_12m"].mean()
        spread   = float(top_ret - bot_ret)

        result.spreads[date_str]      = spread
        result.top_means[date_str]    = float(top_ret)
        result.bottom_means[date_str] = float(bot_ret)
        result.total_years += 1
        if spread > 0:
            result.positive_years += 1

    if result.total_years > 0:
        result.reliable = (result.positive_years / result.total_years) >= _COMPOSITE_RELIABILITY

    return result
