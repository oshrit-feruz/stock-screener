from dataclasses import dataclass

import pandas as pd

# Theory-driven weights (NOT fitted to historical data).
# Growth is the primary driver, then balance sheet health and momentum, then profitability.
WEIGHTS: dict[str, float] = {
    "revenue_growth_yoy": 0.30,
    "debt_to_equity":     0.25,  # inverted before ranking
    "momentum_12m":       0.25,
    "roe":                0.20,
}

_MIN_FACTORS = 3  # ticker excluded from composite if fewer factors available


@dataclass
class CompositeScore:
    ticker: str
    snapshot_date: object          # date
    revenue_growth_rank: float     # percentile 0-100, NaN if missing
    de_rank: float                 # percentile 0-100, NaN if missing (inverted)
    roe_rank: float                # percentile 0-100, NaN if missing
    net_margin_rank: float         # percentile 0-100, NaN if missing (not in composite weight)
    momentum_12m_rank: float       # percentile 0-100, NaN if missing
    composite: float               # weighted average of available ranks
    factors_available: int         # how many of the 4 weighted factors contributed


def _percentile_ranks(series: pd.Series) -> pd.Series:
    """0-100 percentile rank within valid (non-NaN) values; NaN stays NaN."""
    return series.rank(pct=True, na_option="keep") * 100


def score_snapshot(df_year: pd.DataFrame) -> pd.DataFrame:
    """Add rank and composite columns to a single snapshot-date slice.

    Returns a new DataFrame with the original columns plus:
      _rank_revenue_growth_yoy, _rank_de, _rank_roe, _rank_net_margin,
      _rank_momentum_12m, composite_score, composite_factors_available
    """
    df = df_year.copy()

    df["_rank_revenue_growth_yoy"] = _percentile_ranks(df["revenue_growth_yoy"])
    df["_rank_de"]                 = _percentile_ranks(-df["debt_to_equity"])   # invert
    df["_rank_roe"]                = _percentile_ranks(df["roe"])
    df["_rank_net_margin"]         = _percentile_ranks(df["net_margin"])
    df["_rank_momentum_12m"]       = _percentile_ranks(df["momentum_12m"])

    rank_cols = {
        "revenue_growth_yoy": "_rank_revenue_growth_yoy",
        "debt_to_equity":     "_rank_de",
        "momentum_12m":       "_rank_momentum_12m",
        "roe":                "_rank_roe",
    }

    def _composite_row(row: pd.Series) -> tuple[float, int]:
        available = {f: row[col] for f, col in rank_cols.items() if pd.notna(row[col])}
        n = len(available)
        if n < _MIN_FACTORS:
            return float("nan"), n
        total_w = sum(WEIGHTS[f] for f in available)
        score = sum(WEIGHTS[f] * available[f] for f in available) / total_w
        return score, n

    results = df.apply(_composite_row, axis=1, result_type="expand")
    df["composite_score"]              = results[0]
    df["composite_factors_available"]  = results[1]

    return df


def build_composite_df(df: pd.DataFrame) -> pd.DataFrame:
    """Score every snapshot date and return the full DataFrame with rank/composite columns."""
    parts = []
    for _, group in df.groupby("snapshot_date"):
        parts.append(score_snapshot(group))
    if not parts:
        return df
    return pd.concat(parts, ignore_index=True)
