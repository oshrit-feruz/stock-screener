"""
Verify that field extraction works regardless of whether yfinance uses camelCase
(v1.x) or spaced names (older versions), and that all computed metrics are correct.
"""
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.data.fundamentals import PointInTimeFundamentals

_AS_OF = date(2023, 3, 31)  # well after both statement dates below
_COL_NEW = pd.Timestamp("2022-09-24")
_COL_OLD = pd.Timestamp("2021-09-25")

# Expected values (from the camelCase fixture)
_REV_NEW = 400e9
_REV_OLD = 350e9
_NI_NEW = 100e9
_DEBT_NEW = 120e9
_EQ_NEW = 50e9

_EXPECTED_REV_GROWTH = (_REV_NEW - _REV_OLD) / _REV_OLD
_EXPECTED_DE = _DEBT_NEW / _EQ_NEW
_EXPECTED_ROE = _NI_NEW / _EQ_NEW
_EXPECTED_MARGIN = _NI_NEW / _REV_NEW


def _make_ticker(revenue_key: str, income_key: str, debt_key: str, equity_key: str):
    income = pd.DataFrame(
        {
            _COL_NEW: {revenue_key: _REV_NEW, income_key: _NI_NEW},
            _COL_OLD: {revenue_key: _REV_OLD, income_key: 80e9},
        }
    )
    balance = pd.DataFrame(
        {
            _COL_NEW: {debt_key: _DEBT_NEW, equity_key: _EQ_NEW},
            _COL_OLD: {debt_key: 110e9, equity_key: 55e9},
        }
    )
    mock = MagicMock()
    mock.income_stmt = income
    mock.balance_sheet = balance
    return mock


@pytest.mark.parametrize(
    "revenue_key,income_key,debt_key,equity_key,label",
    [
        ("TotalRevenue", "NetIncome", "TotalDebt", "StockholdersEquity", "camelCase (yfinance 1.x)"),
        ("TotalRevenue", "NetIncome", "TotalDebt", "CommonStockEquity", "CommonStockEquity variant"),
        ("Total Revenue", "Net Income", "Total Debt", "Stockholders Equity", "spaced (legacy yfinance)"),
        ("Total Revenue", "Net Income Common Stockholders", "Long Term Debt", "Common Stock Equity", "legacy alt keys"),
    ],
)
def test_field_extraction(tmp_path, revenue_key, income_key, debt_key, equity_key, label):
    """All supported field name variants must yield the same computed metrics."""
    mock_ticker = _make_ticker(revenue_key, income_key, debt_key, equity_key)

    with patch("yfinance.Ticker", return_value=mock_ticker):
        pit = PointInTimeFundamentals(cache_dir=tmp_path)
        snap = pit.get_snapshot("TEST", _AS_OF)

    assert snap is not None, f"snapshot is None for variant: {label}"
    assert snap.statement_date == _COL_NEW.date(), f"wrong statement date for: {label}"
    assert snap.revenue_growth_yoy == pytest.approx(_EXPECTED_REV_GROWTH, rel=1e-6), label
    assert snap.debt_to_equity == pytest.approx(_EXPECTED_DE, rel=1e-6), label
    assert snap.roe == pytest.approx(_EXPECTED_ROE, rel=1e-6), label
    assert snap.net_margin == pytest.approx(_EXPECTED_MARGIN, rel=1e-6), label
