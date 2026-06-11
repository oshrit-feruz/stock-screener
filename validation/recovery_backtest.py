from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from core.data.prices import PriceData
from core.signals.recovery_score import (
    BUY_THRESHOLD,
    LOW_THRESHOLD,
    WEIGHTS,
    compute_recovery_signals,
    passes_quality_gate,
)

_RANDOM_SEED  = 42
_RANDOM_PROB  = 0.10
_WARMUP_START = "2016-01-01"

CASE_STUDIES = [
    ("BA",   date(2020, 3, 23),  "COVID crash bottom"),
    ("NVDA", date(2022, 10, 13), "2022 bear market bottom"),
    ("META", date(2022, 11, 3),  "2022 restructuring bottom"),
    ("AAPL", date(2023, 1, 6),   "2023 recovery start"),
    ("NFLX", date(2022, 5, 12),  "2022 subscriber-shock bottom"),
]
_CASE_WINDOW_DAYS = 10  # ±10 trading days for "within 2 weeks" check


@dataclass
class RecoveryBacktestStats:
    label: str
    n_entries: int
    mean_return_12m: float | None
    mean_return_63d: float | None
    mean_return_21d: float | None
    pct_positive_12m: float | None
    recovery_capture_rate: float | None
    mean_max_drawdown: float | None


def _stats_from_records(label: str, records: list[dict]) -> RecoveryBacktestStats:
    if not records:
        return RecoveryBacktestStats(label, 0, None, None, None, None, None, None)

    r12  = [r["fwd_12m"] for r in records if r["fwd_12m"] is not None]
    r63  = [r["fwd_63d"] for r in records if r["fwd_63d"] is not None]
    r21  = [r["fwd_21d"] for r in records if r["fwd_21d"] is not None]
    dd   = [r["max_dd"]  for r in records if r["max_dd"]  is not None]
    cap  = [r["captured"] for r in records]   # all have this field

    n_captured = sum(cap)
    rcr = float(n_captured / len(cap)) if cap else None

    return RecoveryBacktestStats(
        label                 = label,
        n_entries             = len(records),
        mean_return_12m       = float(np.mean(r12))   if r12  else None,
        mean_return_63d       = float(np.mean(r63))   if r63  else None,
        mean_return_21d       = float(np.mean(r21))   if r21  else None,
        pct_positive_12m      = float(np.mean([x > 0 for x in r12])) if r12 else None,
        recovery_capture_rate = rcr,
        mean_max_drawdown     = float(np.mean(dd))    if dd   else None,
    )


def _fwd_return(close_arr: np.ndarray, i: int, horizon: int) -> float | None:
    j = i + horizon
    if j >= len(close_arr):
        return None
    ep = close_arr[i]
    return float(close_arr[j] / ep - 1) if ep else None


def _recovery_captured(close_arr: np.ndarray, i: int, horizon: int = 126) -> bool:
    """True if price hits +10% before -10%, within horizon days."""
    if i >= len(close_arr):
        return False
    ep = close_arr[i]
    up   = ep * 1.10
    down = ep * 0.90
    for j in range(i + 1, min(i + horizon + 1, len(close_arr))):
        price = close_arr[j]
        if price >= up:
            return True
        if price <= down:
            return False
    return False


def _max_drawdown_window(close_arr: np.ndarray, i: int, horizon: int) -> float | None:
    end    = min(i + horizon, len(close_arr))
    window = close_arr[i:end]
    if len(window) < 2:
        return None
    peak = np.maximum.accumulate(window)
    return float(((window - peak) / peak).min())


class RecoveryBacktester:
    def __init__(
        self,
        tickers: list[str],
        prices: PriceData,
        fundamentals=None,
        start_date: str = "2018-01-01",
        end_date:   str = "2024-12-31",
    ) -> None:
        self.tickers      = tickers
        self.prices       = prices
        self.fundamentals = fundamentals
        self.start_date   = pd.Timestamp(start_date)
        self.end_date     = pd.Timestamp(end_date)

    def run(self) -> dict:
        rng        = random.Random(_RANDOM_SEED)
        spy_regime = self._spy_regime()

        high_recs:   list[dict] = []
        low_recs:    list[dict] = []
        random_recs: list[dict] = []

        for ticker in self.tickers:
            ohlcv = self.prices.get_prices(
                ticker, _WARMUP_START, self.end_date.strftime("%Y-%m-%d")
            )
            if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
                continue

            scored    = compute_recovery_signals(ohlcv)
            close_arr = scored["Close"].to_numpy(dtype=float)
            comp_arr  = scored["composite_score"].to_numpy(dtype=float)
            dates     = scored.index

            quality_by_year = self._prefetch_quality(ticker)

            for i, (ts, comp) in enumerate(zip(dates, comp_arr)):
                if ts < self.start_date or ts > self.end_date:
                    continue

                gate = quality_by_year.get(ts.year)

                fwd_12m = _fwd_return(close_arr, i, 252)
                fwd_63d = _fwd_return(close_arr, i, 63)
                fwd_21d = _fwd_return(close_arr, i, 21)
                max_dd  = _max_drawdown_window(close_arr, i, 252)
                regime  = spy_regime.get(ts.date(), "unknown")
                captured = _recovery_captured(close_arr, i)

                rec = dict(
                    ticker   = ticker,
                    date     = ts.date(),
                    fwd_12m  = fwd_12m,
                    fwd_63d  = fwd_63d,
                    fwd_21d  = fwd_21d,
                    max_dd   = max_dd,
                    regime   = regime,
                    captured = captured,
                )

                if not np.isnan(comp) and gate is not False:
                    if comp >= BUY_THRESHOLD:
                        high_recs.append(dict(rec))
                    elif comp < LOW_THRESHOLD:
                        low_recs.append(dict(rec))

                if rng.random() < _RANDOM_PROB:
                    random_recs.append(dict(rec))

        buckets = {
            "HIGH":   _stats_from_records("HIGH",   high_recs),
            "LOW":    _stats_from_records("LOW",    low_recs),
            "RANDOM": _stats_from_records("RANDOM", random_recs),
        }

        return dict(
            buckets      = buckets,
            ablation     = self._ablation(),
            case_studies = self._run_case_studies(),
            regime       = self._regime_breakdown(high_recs),
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _prefetch_quality(self, ticker: str) -> dict[int, bool | None]:
        result: dict[int, bool | None] = {}
        if self.fundamentals is None:
            return result
        for year in range(2017, 2026):
            snap = self.fundamentals.get_snapshot(ticker, date(year, 12, 31))
            result[year] = passes_quality_gate(snap)
        return result

    def _spy_regime(self) -> dict:
        ohlcv = self.prices.get_prices("SPY", _WARMUP_START, self.end_date.strftime("%Y-%m-%d"))
        if ohlcv is None or ohlcv.empty:
            return {}
        sma200     = ohlcv["Close"].rolling(200).mean()
        sma200_20d = sma200.shift(20)
        bull       = sma200 > sma200_20d
        return {ts.date(): ("bull" if b else "bear") for ts, b in zip(ohlcv.index, bull)}

    def _ablation(self) -> dict[str, dict]:
        components = ["dip", "recovery", "momentum", "volume"]
        results: dict[str, dict] = {}

        for disabled in ["none"] + components:
            high_rets: list[float] = []
            low_rets:  list[float] = []

            for ticker in self.tickers:
                ohlcv = self.prices.get_prices(
                    ticker, _WARMUP_START, self.end_date.strftime("%Y-%m-%d")
                )
                if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
                    continue

                scored = compute_recovery_signals(ohlcv)
                if disabled != "none":
                    scored = scored.copy()
                    scored[f"{disabled}_score"] = 0.5

                has_all = (
                    scored["dip_score"].notna() & scored["recovery_score"].notna() &
                    scored["momentum_score"].notna() & scored["volume_score"].notna()
                )
                comp = (
                    WEIGHTS["dip"]      * scored["dip_score"].fillna(0) +
                    WEIGHTS["recovery"] * scored["recovery_score"].fillna(0) +
                    WEIGHTS["momentum"] * scored["momentum_score"].fillna(0) +
                    WEIGHTS["volume"]   * scored["volume_score"].fillna(0)
                ).where(has_all)

                quality_by_year = self._prefetch_quality(ticker)
                close_arr = scored["Close"].to_numpy(dtype=float)
                comp_arr  = comp.to_numpy(dtype=float)

                for i, (ts, c) in enumerate(zip(scored.index, comp_arr)):
                    if ts < self.start_date or ts > self.end_date:
                        continue
                    gate = quality_by_year.get(ts.year)
                    if np.isnan(c) or gate is False:
                        continue
                    fwd = _fwd_return(close_arr, i, 252)
                    if fwd is None:
                        continue
                    if c >= BUY_THRESHOLD:
                        high_rets.append(fwd)
                    elif c < LOW_THRESHOLD:
                        low_rets.append(fwd)

            results[disabled] = dict(
                high_mean = float(np.mean(high_rets)) if high_rets else None,
                low_mean  = float(np.mean(low_rets))  if low_rets  else None,
                high_n    = len(high_rets),
                low_n     = len(low_rets),
            )

        return results

    def _run_case_studies(self) -> list[dict]:
        results = []
        for ticker, bottom_date, description in CASE_STUDIES:
            end_str   = (bottom_date + pd.Timedelta(days=90)).strftime("%Y-%m-%d")
            ohlcv_full = self.prices.get_prices(ticker, _WARMUP_START, end_str)
            if ohlcv_full is None or ohlcv_full.empty:
                results.append(dict(ticker=ticker, date=bottom_date, description=description,
                                    dip_score=None, recovery_score=None, composite=None,
                                    gate=None, signal="NO_DATA", fwd_63d=None, max_window_score=None))
                continue

            scored = compute_recovery_signals(ohlcv_full)
            close_s = scored["Close"]

            # Score on exact bottom date (or nearest trading day)
            mask = scored.index <= pd.Timestamp(bottom_date)
            if not mask.any():
                row = None
            else:
                row = scored.loc[mask].iloc[-1]

            # Max score in ±window trading days
            window_start = pd.Timestamp(bottom_date) - pd.Timedelta(days=_CASE_WINDOW_DAYS * 2)
            window_end   = pd.Timestamp(bottom_date) + pd.Timedelta(days=_CASE_WINDOW_DAYS * 2)
            window_mask  = (scored.index >= window_start) & (scored.index <= window_end)
            window_scores = scored.loc[window_mask, "composite_score"].dropna()
            max_window = float(window_scores.max()) if len(window_scores) > 0 else None

            # Quality gate
            gate = None
            if self.fundamentals is not None:
                snap = self.fundamentals.get_snapshot(ticker, bottom_date)
                gate = passes_quality_gate(snap)

            # Forward 63-day return
            fwd_63d = None
            bottom_ts = pd.Timestamp(bottom_date)
            if bottom_ts in close_s.index:
                idx   = close_s.index.get_loc(bottom_ts)
            else:
                mask2 = close_s.index <= bottom_ts
                idx   = mask2.sum() - 1 if mask2.any() else -1

            if idx >= 0:
                ep = close_s.iloc[idx]
                j  = idx + 63
                if j < len(close_s):
                    fwd_63d = float(close_s.iloc[j] / ep - 1)

            comp  = float(row["composite_score"]) if row is not None and pd.notna(row["composite_score"]) else None
            dip   = float(row["dip_score"])       if row is not None and pd.notna(row["dip_score"])      else None
            rec   = float(row["recovery_score"])  if row is not None and pd.notna(row["recovery_score"]) else None

            # Signal on the exact date
            if comp is None:
                signal = "INSUFFICIENT_DATA"
            elif gate is False:
                signal = "SKIP"
            elif comp >= BUY_THRESHOLD:
                signal = "BUY"
            else:
                signal = "WAIT"

            # "BUY within window" check
            buy_in_window = False
            if max_window is not None and max_window >= BUY_THRESHOLD and gate is not False:
                buy_in_window = True

            results.append(dict(
                ticker           = ticker,
                date             = bottom_date,
                description      = description,
                dip_score        = dip,
                recovery_score   = rec,
                composite        = comp,
                gate             = gate,
                signal           = signal,
                fwd_63d          = fwd_63d,
                max_window_score = max_window,
                buy_in_window    = buy_in_window,
            ))

        return results

    @staticmethod
    def _regime_breakdown(high_recs: list[dict]) -> dict[str, dict]:
        bull = [r for r in high_recs if r["regime"] == "bull"]
        bear = [r for r in high_recs if r["regime"] == "bear"]

        def _s(recs: list[dict]) -> dict:
            r12 = [r["fwd_12m"] for r in recs if r["fwd_12m"] is not None]
            return dict(
                n            = len(recs),
                mean_12m     = float(np.mean(r12)) if r12 else None,
                pct_positive = float(np.mean([x > 0 for x in r12])) if r12 else None,
            )

        return {"bull": _s(bull), "bear": _s(bear)}
