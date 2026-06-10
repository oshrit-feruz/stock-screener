import pickle
from pathlib import Path

import pandas as pd
import yfinance as yf

_DEFAULT_CACHE = Path(__file__).parent.parent.parent / "data" / "cache" / "prices"


def _safe_ticker(ticker: str) -> str:
    """Strip path separators and dots to prevent cache path traversal."""
    return "".join(c for c in ticker if c.isalnum() or c in "-_")


class PriceData:
    def __init__(self, cache_dir: Path = _DEFAULT_CACHE):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, ticker: str, start: str, end: str) -> Path:
        return self.cache_dir / f"{_safe_ticker(ticker)}_{start}_{end}.pkl"

    def get_prices(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        path = self._cache_path(ticker, start, end)
        if path.exists():
            try:
                with open(path, "rb") as f:
                    return pickle.load(f)
            except Exception:
                pass
        try:
            df = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
            if df is None or df.empty:
                return pd.DataFrame()
            # Flatten MultiIndex columns (newer yfinance versions may add a ticker level)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            with open(path, "wb") as f:
                pickle.dump(df, f)
            return df
        except Exception:
            return pd.DataFrame()

    def get_return(self, ticker: str, start: str, end: str) -> float | None:
        df = self.get_prices(ticker, start, end)
        if df.empty or len(df) < 2:
            return None
        try:
            start_price = float(df["Close"].iloc[0])
            end_price = float(df["Close"].iloc[-1])
            if start_price == 0:
                return None
            return (end_price - start_price) / start_price
        except Exception:
            return None
