from datetime import date

import pandas as pd

from validation.composite import score_snapshot
from validation.factor_tests import CompositeResult, evaluate_composite

# ── helpers ──────────────────────────────────────────────────────────────────

def _make_df(n: int, snap_date="2022-12-31", **factor_overrides) -> pd.DataFrame:
    base = {
        "ticker":             [f"T{i:02d}" for i in range(n)],
        "snapshot_date":      date.fromisoformat(snap_date),
        "forward_return_12m": [float(i) / n for i in range(n)],
        "revenue_growth_yoy": [float(i) / n for i in range(n)],
        "debt_to_equity":     [float(n - i) / n for i in range(n)],  # inverted
        "roe":                [float(i) / n for i in range(n)],
        "net_margin":         [float(i) / n for i in range(n)],
        "momentum_12m":       [float(i) / n for i in range(n)],
        "momentum_6m":        [float(i) / n for i in range(n)],
    }
    base.update(factor_overrides)
    return pd.DataFrame(base)


# ── score_snapshot ────────────────────────────────────────────────────────────

def test_percentile_ranks_range():
    """All rank columns should be in [0, 100]."""
    df = _make_df(20)
    scored = score_snapshot(df)
    for col in ["_rank_revenue_growth_yoy", "_rank_de", "_rank_roe", "_rank_net_margin", "_rank_momentum_12m"]:
        valid = scored[col].dropna()
        assert (valid >= 0).all() and (valid <= 100).all(), f"{col} out of range"


def test_de_rank_inverted():
    """Ticker with LOWEST D/E should get the HIGHEST _rank_de."""
    n = 10
    de_vals = list(range(1, n + 1))   # T00 has D/E=1 (lowest), T09 has D/E=10 (highest)
    df = _make_df(n, debt_to_equity=de_vals)
    scored = score_snapshot(df)
    best_de_ticker  = scored.loc[scored["_rank_de"].idxmax(), "ticker"]
    worst_de_ticker = scored.loc[scored["_rank_de"].idxmin(), "ticker"]
    assert best_de_ticker  == "T00"   # lowest D/E → highest rank
    assert worst_de_ticker == "T09"


def test_composite_missing_factor_redistributes_weight():
    """When one factor is NaN, its weight is redistributed; composite stays in [0,100]."""
    n = 20
    rev_vals = [float("nan")] * n     # revenue_growth missing for all tickers
    df = _make_df(n, revenue_growth_yoy=rev_vals)
    scored = score_snapshot(df)
    valid = scored["composite_score"].dropna()
    assert len(valid) == n
    assert (valid >= 0).all() and (valid <= 100).all()


def test_composite_excluded_when_too_few_factors():
    """Ticker with fewer than 3 of 4 weighted factors should have NaN composite."""
    n = 20
    # Make revenue_growth AND roe NaN → only 2 factors available → excluded
    df = _make_df(
        n,
        revenue_growth_yoy=[float("nan")] * n,
        roe=[float("nan")] * n,
    )
    scored = score_snapshot(df)
    assert scored["composite_score"].isna().all()


def test_composite_top_beats_bottom():
    """In a perfectly ordered universe, top-composite tickers have higher forward returns."""
    n = 40
    df = _make_df(n)
    scored = score_snapshot(df)
    ranked = scored.sort_values("composite_score", ascending=False)
    top_ret = ranked.head(4)["forward_return_12m"].mean()
    bot_ret = ranked.tail(4)["forward_return_12m"].mean()
    assert top_ret > bot_ret


# ── evaluate_composite ────────────────────────────────────────────────────────

def test_evaluate_composite_spread_positive():
    """Well-ordered universe → composite spread > 0."""
    df = _make_df(40)
    result = evaluate_composite(df)
    assert result.spreads[str(date(2022, 12, 31))] > 0


def test_evaluate_composite_insufficient_rows():
    """< 20 rows → spread is None."""
    df = _make_df(10)
    result = evaluate_composite(df)
    assert result.spreads[str(date(2022, 12, 31))] is None


def test_evaluate_composite_reliability_threshold():
    """Reliable requires ≥5/7 years positive (stricter than individual factors)."""
    # 5 positive years, 2 negative → exactly at threshold → reliable=True
    rows = []
    for year, sign in enumerate([1, 1, 1, 1, 1, -1, -1], start=2018):
        snap = f"{year}-12-31"
        n = 40
        fwd = [sign * float(i) / n for i in range(n)]
        rows.append(_make_df(n, snap_date=snap, forward_return_12m=fwd))
    df = pd.concat(rows, ignore_index=True)
    result = evaluate_composite(df)
    assert result.positive_years == 5
    assert result.total_years == 7
    assert result.reliable is True


def test_sensitivity_weights_accepted():
    """evaluate_composite accepts a weights override and returns a CompositeResult."""
    df = _make_df(40)
    alt_weights = {"revenue_growth_yoy": 0.25, "debt_to_equity": 0.25,
                   "momentum_12m": 0.25, "roe": 0.25}
    result = evaluate_composite(df, weights=alt_weights)
    assert isinstance(result, CompositeResult)
    assert result.spreads[str(date(2022, 12, 31))] is not None


def test_sensitivity_does_not_mutate_global_weights():
    """Passing a weights override must not permanently change composite.WEIGHTS."""
    import validation.composite as cm
    original = dict(cm.WEIGHTS)
    df = _make_df(40)
    evaluate_composite(df, weights={"revenue_growth_yoy": 1.0,
                                    "debt_to_equity": 0.0,
                                    "momentum_12m": 0.0,
                                    "roe": 0.0})
    assert dict(cm.WEIGHTS) == original
