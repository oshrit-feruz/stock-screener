import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

_DEFAULT_CACHE = Path(__file__).parent.parent.parent / "data" / "cache" / "fundamentals"
_PUBLICATION_LAG_DAYS = 90

# yfinance 1.x uses camelCase; older versions used spaced names — try both
_REVENUE_KEYS = ["TotalRevenue", "Total Revenue", "OperatingRevenue", "Operating Revenue"]
_NET_INCOME_KEYS = [
    "NetIncome",
    "Net Income",
    "NetIncomeCommonStockholders",
    "Net Income Common Stockholders",
    "NetIncomeFromContinuingOperationNetMinorityInterest",
]
_TOTAL_DEBT_KEYS = ["TotalDebt", "Total Debt", "LongTermDebt", "Long Term Debt"]
_EQUITY_KEYS = [
    "StockholdersEquity",
    "Stockholders Equity",
    "CommonStockEquity",
    "Common Stock Equity",
    "TotalEquityGrossMinorityInterest",
    "Total Stockholder Equity",
]


@dataclass
class FundamentalSnapshot:
    statement_date: date
    revenue_growth_yoy: float | None
    debt_to_equity: float | None
    roe: float | None
    net_margin: float | None


def _safe_get(df: pd.DataFrame, keys: list[str], col) -> float | None:
    for key in keys:
        if key in df.index:
            try:
                val = df.loc[key, col]
                if pd.notna(val):
                    return float(val)
            except Exception:
                continue
    return None


def _col_to_date(col) -> date:
    return col.date() if hasattr(col, "date") else pd.Timestamp(col).date()


class PointInTimeFundamentals:
    def __init__(self, cache_dir: Path = _DEFAULT_CACHE):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, ticker: str) -> Path:
        return self.cache_dir / f"{ticker}.json"

    def _fetch_and_process(self, ticker: str) -> list[dict] | None:
        try:
            stock = yf.Ticker(ticker)
            income = stock.income_stmt
            balance = stock.balance_sheet
            if income is None or income.empty:
                return None
        except Exception:
            return None

        snapshots: list[dict] = []
        dates = list(income.columns)  # Descending: most recent first

        for i, col in enumerate(dates):
            stmt_date = _col_to_date(col)

            revenue_current = _safe_get(income, _REVENUE_KEYS, col)
            revenue_prior = None
            if i + 1 < len(dates):
                revenue_prior = _safe_get(income, _REVENUE_KEYS, dates[i + 1])

            revenue_growth = None
            if revenue_current is not None and revenue_prior is not None and revenue_prior != 0:
                revenue_growth = (revenue_current - revenue_prior) / abs(revenue_prior)

            net_income = _safe_get(income, _NET_INCOME_KEYS, col)

            debt_to_equity = None
            roe = None
            if balance is not None and not balance.empty and col in balance.columns:
                equity = _safe_get(balance, _EQUITY_KEYS, col)
                total_debt = _safe_get(balance, _TOTAL_DEBT_KEYS, col)
                if equity is not None and equity != 0:
                    if total_debt is not None:
                        debt_to_equity = total_debt / equity
                    if net_income is not None:
                        roe = net_income / equity

            net_margin = None
            if net_income is not None and revenue_current is not None and revenue_current != 0:
                net_margin = net_income / revenue_current

            # Skip sparse columns (oldest year from yfinance is often nearly empty).
            # A column with no revenue has no computable growth or margin; storing it
            # would cause get_snapshot to return an all-None entry instead of None.
            if revenue_current is None:
                continue

            snapshots.append(
                {
                    "statement_date": stmt_date.isoformat(),
                    "revenue_growth_yoy": revenue_growth,
                    "debt_to_equity": debt_to_equity,
                    "roe": roe,
                    "net_margin": net_margin,
                }
            )

        return snapshots

    def _load_snapshots(self, ticker: str) -> list[dict] | None:
        path = self._cache_path(ticker)
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        snapshots = self._fetch_and_process(ticker)
        if snapshots is not None:
            try:
                with open(path, "w") as f:
                    json.dump(snapshots, f, indent=2)
            except Exception:
                pass
        return snapshots

    def get_snapshot(self, ticker: str, as_of_date: date | str) -> FundamentalSnapshot | None:
        if isinstance(as_of_date, str):
            as_of_date = date.fromisoformat(as_of_date)

        cutoff = as_of_date - timedelta(days=_PUBLICATION_LAG_DAYS)

        try:
            snapshots = self._load_snapshots(ticker)
            if not snapshots:
                return None

            eligible = [
                s for s in snapshots
                if date.fromisoformat(s["statement_date"]) <= cutoff
            ]
            if not eligible:
                return None

            best = max(eligible, key=lambda s: s["statement_date"])
            return FundamentalSnapshot(
                statement_date=date.fromisoformat(best["statement_date"]),
                revenue_growth_yoy=best["revenue_growth_yoy"],
                debt_to_equity=best["debt_to_equity"],
                roe=best["roe"],
                net_margin=best["net_margin"],
            )
        except Exception:
            return None
