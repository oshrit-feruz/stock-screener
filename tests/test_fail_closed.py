from datetime import date
from unittest.mock import patch

import pandas as pd

from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData


def test_prices_network_exception_returns_empty(tmp_path):
    prices = PriceData(cache_dir=tmp_path)
    with patch("yfinance.download", side_effect=Exception("network error")):
        df = prices.get_prices("AAPL", "2022-01-01", "2022-12-31")
    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_prices_empty_response_returns_empty(tmp_path):
    prices = PriceData(cache_dir=tmp_path)
    with patch("yfinance.download", return_value=pd.DataFrame()):
        df = prices.get_prices("AAPL", "2022-01-01", "2022-12-31")
    assert df.empty


def test_get_return_network_exception_returns_none(tmp_path):
    prices = PriceData(cache_dir=tmp_path)
    with patch("yfinance.download", side_effect=Exception("network error")):
        result = prices.get_return("AAPL", "2022-01-01", "2022-12-31")
    assert result is None


def test_fundamentals_network_exception_returns_none(tmp_path):
    pit = PointInTimeFundamentals(cache_dir=tmp_path)
    with patch("yfinance.Ticker", side_effect=Exception("network error")):
        snapshot = pit.get_snapshot("AAPL", date(2022, 12, 31))
    assert snapshot is None


def test_fundamentals_empty_income_stmt_returns_none(tmp_path):
    from unittest.mock import MagicMock
    mock_ticker = MagicMock()
    mock_ticker.income_stmt = pd.DataFrame()
    mock_ticker.balance_sheet = pd.DataFrame()

    pit = PointInTimeFundamentals(cache_dir=tmp_path)
    with patch("yfinance.Ticker", return_value=mock_ticker):
        snapshot = pit.get_snapshot("AAPL", date(2022, 12, 31))
    assert snapshot is None


def test_no_exception_propagates(tmp_path):
    """Belt-and-suspenders: nothing raises regardless of failure mode."""
    prices = PriceData(cache_dir=tmp_path)
    pit = PointInTimeFundamentals(cache_dir=tmp_path)

    with patch("yfinance.download", side_effect=RuntimeError("boom")):
        prices.get_prices("ZZZINVALID", "2022-01-01", "2022-12-31")
        prices.get_return("ZZZINVALID", "2022-01-01", "2022-12-31")

    with patch("yfinance.Ticker", side_effect=RuntimeError("boom")):
        pit.get_snapshot("ZZZINVALID", date(2022, 12, 31))
