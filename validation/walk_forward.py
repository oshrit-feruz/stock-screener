from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData


@dataclass
class SnapshotRow:
    ticker: str
    snapshot_date: date
    statement_date: date | None
    revenue_growth_yoy: float | None
    debt_to_equity: float | None
    roe: float | None
    net_margin: float | None
    momentum_12m: float | None
    momentum_6m: float | None
    forward_return_12m: float | None


def _iso(d: date) -> str:
    return d.isoformat()


class WalkForwardEngine:
    def __init__(
        self,
        tickers: list[str],
        snapshot_dates: list[str],
        prices: PriceData | None = None,
        fundamentals: PointInTimeFundamentals | None = None,
    ) -> None:
        self.tickers = tickers
        self.snapshot_dates = [date.fromisoformat(s) for s in snapshot_dates]
        self.prices = prices or PriceData()
        self.fundamentals = fundamentals or PointInTimeFundamentals()

    def build_snapshot_df(self) -> pd.DataFrame:
        rows: list[dict] = []
        for snap_date in self.snapshot_dates:
            fwd_start = _iso(snap_date + timedelta(days=2))
            fwd_end   = _iso(snap_date + timedelta(days=365))
            mom12_start = _iso(snap_date - timedelta(days=365))
            mom6_start  = _iso(snap_date - timedelta(days=182))
            # Skip most-recent month (standard 12-1 momentum avoids short-term reversal)
            mom_end = _iso(snap_date - timedelta(days=2))

            for ticker in self.tickers:
                try:
                    snap = self.fundamentals.get_snapshot(ticker, snap_date)
                    if snap is None:
                        continue

                    fwd = self.prices.get_return(ticker, fwd_start, fwd_end)
                    if fwd is None:
                        continue

                    mom12 = self.prices.get_return(ticker, mom12_start, mom_end)
                    mom6  = self.prices.get_return(ticker, mom6_start, mom_end)

                    rows.append({
                        "ticker":             ticker,
                        "snapshot_date":      snap_date,
                        "statement_date":     snap.statement_date,
                        "revenue_growth_yoy": snap.revenue_growth_yoy,
                        "debt_to_equity":     snap.debt_to_equity,
                        "roe":                snap.roe,
                        "net_margin":         snap.net_margin,
                        "momentum_12m":       mom12,
                        "momentum_6m":        mom6,
                        "forward_return_12m": fwd,
                    })
                except Exception:
                    continue

        if not rows:
            return pd.DataFrame(columns=[f.name for f in SnapshotRow.__dataclass_fields__.values()])
        return pd.DataFrame(rows)
