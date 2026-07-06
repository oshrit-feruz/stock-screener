from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData


def _mock_price_df():
    idx = pd.bdate_range("2022-01-03", "2022-01-07")
    return pd.DataFrame({"Close": [170.0, 172.0, 174.0, 176.0, 178.0]}, index=idx)


def _mock_income():
    # yfinance 1.x camelCase labels
    return pd.DataFrame(
        {
            pd.Timestamp("2022-09-24"): {"TotalRevenue": 394e9, "NetIncome": 99e9},
            pd.Timestamp("2021-09-25"): {"TotalRevenue": 366e9, "NetIncome": 95e9},
        }
    )


def _mock_balance():
    return pd.DataFrame(
        {
            pd.Timestamp("2022-09-24"): {"TotalDebt": 132e9, "StockholdersEquity": 51e9},
            pd.Timestamp("2021-09-25"): {"TotalDebt": 137e9, "StockholdersEquity": 63e9},
        }
    )


def test_price_second_call_uses_cache(tmp_path):
    """The EODHD fetch must be called exactly once; second call reads from cache."""
    prices = PriceData(cache_dir=tmp_path)

    with patch("core.data.prices.fetch_eod", return_value=_mock_price_df()) as mock_dl:
        first = prices.get_prices("AAPL", "2022-01-03", "2022-01-07")
        assert mock_dl.call_count == 1

        second = prices.get_prices("AAPL", "2022-01-03", "2022-01-07")
        assert mock_dl.call_count == 1  # No additional network hit

    assert not first.empty
    assert first.equals(second)


def test_fundamentals_second_call_uses_cache(tmp_path):
    """yfinance.Ticker must be constructed exactly once; second call reads from JSON cache."""
    mock_ticker = MagicMock()
    mock_ticker.income_stmt = _mock_income()
    mock_ticker.balance_sheet = _mock_balance()

    with patch("yfinance.Ticker", return_value=mock_ticker) as mock_cls:
        pit = PointInTimeFundamentals(cache_dir=tmp_path)
        s1 = pit.get_snapshot("AAPL", date(2022, 12, 31))
        assert mock_cls.call_count == 1

        s2 = pit.get_snapshot("AAPL", date(2022, 12, 31))
        assert mock_cls.call_count == 1  # No additional network hit

    assert s1 is not None
    assert s1 == s2


def test_cache_file_is_created(tmp_path):
    """Verify that cache files are written to disk after the first fetch."""
    prices = PriceData(cache_dir=tmp_path)

    with patch("core.data.prices.fetch_eod", return_value=_mock_price_df()):
        prices.get_prices("AAPL", "2022-01-03", "2022-01-07")

    cache_files = list(tmp_path.glob("*.pkl"))
    assert len(cache_files) == 1


def test_fundamentals_cache_file_is_created(tmp_path):
    mock_ticker = MagicMock()
    mock_ticker.income_stmt = _mock_income()
    mock_ticker.balance_sheet = _mock_balance()

    with patch("yfinance.Ticker", return_value=mock_ticker):
        pit = PointInTimeFundamentals(cache_dir=tmp_path)
        pit.get_snapshot("AAPL", date(2022, 12, 31))

    cache_files = list(tmp_path.glob("*.json"))
    assert len(cache_files) == 1
    assert cache_files[0].name == "AAPL.json"
