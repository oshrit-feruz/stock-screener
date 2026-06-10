from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from core.data.fundamentals import PointInTimeFundamentals


def _income_stmt():
    # yfinance 1.x uses camelCase row labels
    return pd.DataFrame(
        {
            pd.Timestamp("2022-09-24"): {"TotalRevenue": 394_328e6, "NetIncome": 99_803e6},
            pd.Timestamp("2021-09-25"): {"TotalRevenue": 365_817e6, "NetIncome": 94_680e6},
            pd.Timestamp("2020-09-26"): {"TotalRevenue": 274_515e6, "NetIncome": 57_411e6},
        }
    )


def _balance_sheet():
    return pd.DataFrame(
        {
            pd.Timestamp("2022-09-24"): {"TotalDebt": 132_480e6, "StockholdersEquity": 50_672e6},
            pd.Timestamp("2021-09-25"): {"TotalDebt": 136_522e6, "StockholdersEquity": 63_090e6},
            pd.Timestamp("2020-09-26"): {"TotalDebt": 112_436e6, "StockholdersEquity": 65_339e6},
        }
    )


def test_snapshot_respects_90_day_lag(tmp_path):
    """As-of 2022-12-31 → cutoff is 2022-10-02; FY2022 (2022-09-24) should be returned."""
    mock_ticker = MagicMock()
    mock_ticker.income_stmt = _income_stmt()
    mock_ticker.balance_sheet = _balance_sheet()

    with patch("yfinance.Ticker", return_value=mock_ticker):
        pit = PointInTimeFundamentals(cache_dir=tmp_path)
        snapshot = pit.get_snapshot("AAPL", date(2022, 12, 31))

    assert snapshot is not None
    # Core assertion: statement_date must not exceed the 90-day lag cutoff
    assert snapshot.statement_date <= date(2022, 10, 2)
    assert snapshot.statement_date == date(2022, 9, 24)


def test_future_statement_excluded(tmp_path):
    """A statement dated within 90 days of as_of_date must be excluded."""
    income = _income_stmt().copy()
    balance = _balance_sheet().copy()

    # 2022-10-15 is only 77 days before 2022-12-31 → must be excluded
    trap = pd.Timestamp("2022-10-15")
    income[trap] = {"TotalRevenue": 999e12, "NetIncome": 999e12}
    income = income[sorted(income.columns, reverse=True)]

    balance[trap] = {"TotalDebt": 1e9, "StockholdersEquity": 1e9}
    balance = balance[sorted(balance.columns, reverse=True)]

    mock_ticker = MagicMock()
    mock_ticker.income_stmt = income
    mock_ticker.balance_sheet = balance

    with patch("yfinance.Ticker", return_value=mock_ticker):
        pit = PointInTimeFundamentals(cache_dir=tmp_path)
        snapshot = pit.get_snapshot("AAPL", date(2022, 12, 31))

    assert snapshot is not None
    # The trap date must be excluded; most recent eligible is still 2022-09-24
    assert snapshot.statement_date == date(2022, 9, 24)


def test_no_eligible_statement_returns_none(tmp_path):
    """When all statements are too recent, return None."""
    # Only statement is 2022-11-01, which is 60 days before 2022-12-31 → excluded
    income = pd.DataFrame(
        {pd.Timestamp("2022-11-01"): {"TotalRevenue": 100e9, "NetIncome": 20e9}}
    )
    balance = pd.DataFrame(
        {pd.Timestamp("2022-11-01"): {"TotalDebt": 10e9, "StockholdersEquity": 50e9}}
    )

    mock_ticker = MagicMock()
    mock_ticker.income_stmt = income
    mock_ticker.balance_sheet = balance

    with patch("yfinance.Ticker", return_value=mock_ticker):
        pit = PointInTimeFundamentals(cache_dir=tmp_path)
        snapshot = pit.get_snapshot("AAPL", date(2022, 12, 31))

    assert snapshot is None
