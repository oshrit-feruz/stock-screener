from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from core.data.edgar import EdgarFundamentals
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
        fundamentals: PointInTimeFundamentals | EdgarFundamentals | None = None,
        use_edgar: bool = True,
    ) -> None:
        self.tickers = tickers
        self.snapshot_dates = [date.fromisoformat(s) for s in snapshot_dates]
        self.prices = prices or PriceData()
        if fundamentals is not None:
            self.fundamentals = fundamentals
        elif use_edgar:
            self.fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())
        else:
            self.fundamentals = PointInTimeFundamentals()

    def build_snapshot_df(self) -> pd.DataFrame:
        # Collect raw rows first; factor-momentum columns require the full cross-section
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
            empty_cols = list(SnapshotRow.__dataclass_fields__) + [
                "revenue_growth_acceleration", "margin_improvement"
            ]
            return pd.DataFrame(columns=empty_cols)

        df = pd.DataFrame(rows)
        df = self._add_factor_momentum(df)
        return df

    def _add_factor_momentum(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add revenue_growth_acceleration and margin_improvement columns.

        Each is defined as the factor value at the current snapshot minus the
        value at the prior year's snapshot (same ticker, one year back).
        """
        df = df.sort_values(["ticker", "snapshot_date"]).copy()
        prior = df[["ticker", "snapshot_date", "revenue_growth_yoy", "net_margin"]].copy()
        prior["snapshot_date"] = prior["snapshot_date"].apply(
            lambda d: date(d.year + 1, d.month, d.day)
        )
        prior = prior.rename(columns={
            "revenue_growth_yoy": "_prior_rev_growth",
            "net_margin":         "_prior_net_margin",
        })
        df = df.merge(prior, on=["ticker", "snapshot_date"], how="left")
        df["revenue_growth_acceleration"] = df["revenue_growth_yoy"] - df["_prior_rev_growth"]
        df["margin_improvement"]          = df["net_margin"] - df["_prior_net_margin"]
        df = df.drop(columns=["_prior_rev_growth", "_prior_net_margin"])
        return df
