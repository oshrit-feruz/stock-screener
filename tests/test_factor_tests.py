from datetime import date

import pandas as pd
import pytest

from validation.factor_tests import _year_spread, evaluate_factors


def _make_df(n: int, factor_vals: list[float], fwd_vals: list[float], snap_date="2022-12-31") -> pd.DataFrame:
    return pd.DataFrame({
        "ticker":             [f"T{i:02d}" for i in range(n)],
        "snapshot_date":      date.fromisoformat(snap_date),
        "forward_return_12m": fwd_vals,
        "revenue_growth_yoy": factor_vals,
        "debt_to_equity":     [1.0] * n,
        "roe":                [0.1] * n,
        "net_margin":         [0.1] * n,
        "momentum_12m":       [0.05] * n,
        "momentum_6m":        [0.05] * n,
    })


def test_year_spread_top_minus_bottom():
    """Spread = mean forward return of top decile minus bottom decile."""
    # 20 stocks: top 2 (highest factor) have fwd=+20%, bottom 2 have fwd=-10%
    n = 20
    factor_vals = list(range(n))          # 0..19, higher = better
    fwd_vals    = [0.20 if i >= 18 else (-0.10 if i < 2 else 0.05) for i in range(n)]
    df = _make_df(n, factor_vals, fwd_vals)
    spread, n_valid = _year_spread(df, "revenue_growth_yoy", direction=1)
    assert spread == pytest.approx(0.20 - (-0.10), rel=1e-6)
    assert n_valid == n


def test_factor_direction_inverts_de():
    """For D/E (direction=-1), stocks with LOWEST D/E should be in top decile."""
    n = 20
    # Stocks 0-1 have lowest D/E (0.1, 0.2) and highest forward return (50%)
    # Stocks 18-19 have highest D/E (1.9, 2.0) and lowest forward return (-20%)
    de_vals  = [0.1 * (i + 1) for i in range(n)]   # 0.1 .. 2.0
    fwd_vals = [0.50 if i < 2 else (-0.20 if i >= 18 else 0.05) for i in range(n)]

    df = pd.DataFrame({
        "ticker":             [f"T{i:02d}" for i in range(n)],
        "snapshot_date":      date(2022, 12, 31),
        "forward_return_12m": fwd_vals,
        "debt_to_equity":     de_vals,
        "revenue_growth_yoy": [0.1] * n,
        "roe":                [0.1] * n,
        "net_margin":         [0.1] * n,
        "momentum_12m":       [0.05] * n,
        "momentum_6m":        [0.05] * n,
    })

    spread, n_valid = _year_spread(df, "debt_to_equity", direction=-1)
    # Top decile = low D/E stocks (high fwd return), bottom decile = high D/E (low fwd)
    assert spread == pytest.approx(0.50 - (-0.20), rel=1e-6)
    assert n_valid == n


def test_reliability_two_of_three():
    """Factor is reliable when spread > 0 in ≥ 2/3 years."""
    n = 20
    base_factor = list(range(n))
    base_fwd    = [0.20 if i >= 18 else (-0.10 if i < 2 else 0.05) for i in range(n)]

    rows = []
    for snap_str, fwd_sign in [("2022-12-31", 1), ("2023-12-31", 1), ("2024-12-31", -1)]:
        df_year = _make_df(n, base_factor, [x * fwd_sign for x in base_fwd], snap_str)
        rows.append(df_year)

    df = pd.concat(rows, ignore_index=True)
    results = evaluate_factors(df)
    rev_result = next(r for r in results if r.factor == "revenue_growth_yoy")
    assert rev_result.positive_years == 2
    assert rev_result.total_years == 3
    assert rev_result.reliable is True


def test_reliability_one_of_three():
    """Factor fails reliability when spread > 0 in only 1/3 years."""
    n = 20
    base_factor = list(range(n))
    base_fwd    = [0.20 if i >= 18 else (-0.10 if i < 2 else 0.05) for i in range(n)]

    rows = []
    for snap_str, fwd_sign in [("2022-12-31", 1), ("2023-12-31", -1), ("2024-12-31", -1)]:
        df_year = _make_df(n, base_factor, [x * fwd_sign for x in base_fwd], snap_str)
        rows.append(df_year)

    df = pd.concat(rows, ignore_index=True)
    results = evaluate_factors(df)
    rev_result = next(r for r in results if r.factor == "revenue_growth_yoy")
    assert rev_result.positive_years == 1
    assert rev_result.total_years == 3
    assert rev_result.reliable is False


def test_insufficient_data_returns_none():
    """Years with < 20 valid rows produce a None spread and are excluded from reliability."""
    n = 10  # below _MIN_ROWS
    df = _make_df(n, list(range(n)), [0.1] * n)
    spread, n_valid = _year_spread(df, "revenue_growth_yoy", direction=1)
    assert spread is None
    assert n_valid == n


def test_insufficient_data_excluded_from_reliability_count():
    """None spreads don't count toward total_years."""
    # 1 year with enough data (spread > 0), 2 years with too few rows
    n_good, n_bad = 20, 5
    good_factor = list(range(n_good))
    good_fwd    = [0.20 if i >= 18 else (-0.10 if i < 2 else 0.05) for i in range(n_good)]
    df_good = _make_df(n_good, good_factor, good_fwd, "2022-12-31")
    df_bad1 = _make_df(n_bad,  list(range(n_bad)), [0.1] * n_bad, "2023-12-31")
    df_bad2 = _make_df(n_bad,  list(range(n_bad)), [0.1] * n_bad, "2024-12-31")

    df = pd.concat([df_good, df_bad1, df_bad2], ignore_index=True)
    results = evaluate_factors(df)
    rev_result = next(r for r in results if r.factor == "revenue_growth_yoy")
    assert rev_result.total_years == 1
    assert rev_result.positive_years == 1
    assert rev_result.reliable is True  # 1/1 >= 2/3
