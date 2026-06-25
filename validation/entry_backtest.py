from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.data.prices import PriceData
from core.signals.entry_score import (
    BUY_THRESHOLD,
    LOW_THRESHOLD,
    WEIGHTS,
    compute_signals,
)

_RANDOM_SEED   = 42
_RANDOM_PROB   = 0.10      # 10% of all dates sampled for RANDOM bucket
_ATR_MULT      = 3.0       # ATR trailing stop multiplier
_WARMUP_START  = "2016-01-01"


@dataclass
class BacktestStats:
    label: str
    n_entries: int
    mean_return_12m: float | None
    median_return_12m: float | None
    pct_positive_12m: float | None
    mean_max_drawdown: float | None
    mean_return_6m: float | None = None
    mean_return_atr: float | None = None


def _stats_from_records(label: str, records: list[dict]) -> BacktestStats:
    if not records:
        return BacktestStats(label, 0, None, None, None, None)

    r12 = [r["fwd_12m"] for r in records if r["fwd_12m"] is not None]
    r6  = [r["fwd_6m"]  for r in records if r["fwd_6m"]  is not None]
    dd  = [r["max_dd"]  for r in records if r["max_dd"]  is not None]

    return BacktestStats(
        label             = label,
        n_entries         = len(records),
        mean_return_12m   = float(np.mean(r12))   if r12 else None,
        median_return_12m = float(np.median(r12)) if r12 else None,
        pct_positive_12m  = float(np.mean([x > 0 for x in r12])) if r12 else None,
        mean_max_drawdown = float(np.mean(dd))    if dd  else None,
        mean_return_6m    = float(np.mean(r6))    if r6  else None,
    )


def _atr_exit_return(close_arr: np.ndarray, atr_arr: np.ndarray, entry_idx: int) -> float | None:
    """Simulate ATR 3× trailing stop from entry_idx; return exit return."""
    n = len(close_arr)
    if entry_idx >= n:
        return None
    entry_price  = close_arr[entry_idx]
    running_high = entry_price
    stop         = entry_price - _ATR_MULT * atr_arr[entry_idx]

    for i in range(entry_idx + 1, min(entry_idx + 253, n)):
        price = close_arr[i]
        if price > running_high:
            running_high = price
            atr_i = atr_arr[i]
            if not np.isnan(atr_i):
                stop = running_high - _ATR_MULT * atr_i
        if price < stop:
            return float(price / entry_price - 1)

    # max holding period reached — exit at close
    exit_idx = min(entry_idx + 252, n - 1)
    return float(close_arr[exit_idx] / entry_price - 1)


def _max_drawdown_window(close_arr: np.ndarray, entry_idx: int, horizon: int) -> float | None:
    end = min(entry_idx + horizon, len(close_arr))
    window = close_arr[entry_idx:end]
    if len(window) < 2:
        return None
    peak = np.maximum.accumulate(window)
    dd   = (window - peak) / peak
    return float(dd.min())


class EntryBacktester:
    def __init__(
        self,
        tickers: list[str],
        prices: PriceData,
        start_date: str = "2018-01-01",
        end_date:   str = "2024-12-31",
    ) -> None:
        self.tickers    = tickers
        self.prices     = prices
        self.start_date = pd.Timestamp(start_date)
        self.end_date   = pd.Timestamp(end_date)

    # ── public entry point ────────────────────────────────────────────────────

    def run(self) -> dict:
        """Return dict with keys: buckets, ablation, exits, regime."""
        rng = random.Random(_RANDOM_SEED)

        high_records:   list[dict] = []
        low_records:    list[dict] = []
        random_records: list[dict] = []

        spy_regime = self._spy_regime()

        for ticker in self.tickers:
            ohlcv = self.prices.get_prices(ticker, _WARMUP_START, self.end_date.strftime("%Y-%m-%d"))
            if ohlcv is None or ohlcv.empty or len(ohlcv) < 200:
                continue

            scored      = compute_signals(ohlcv)
            close_arr   = scored["Close"].to_numpy(dtype=float)
            atr_arr     = scored["atr_14"].to_numpy(dtype=float)
            dates       = scored.index
            comp_scores = scored["composite_score"].to_numpy(dtype=float)

            for i, (ts, comp) in enumerate(zip(dates, comp_scores)):
                if ts < self.start_date or ts > self.end_date:
                    continue

                fwd_12m = self._fwd_return(close_arr, i, 252)
                fwd_6m  = self._fwd_return(close_arr, i, 126)
                max_dd  = _max_drawdown_window(close_arr, i, 252)
                regime  = spy_regime.get(ts.date(), "unknown")

                rec = dict(
                    ticker  = ticker,
                    date    = ts.date(),
                    fwd_12m = fwd_12m,
                    fwd_6m  = fwd_6m,
                    max_dd  = max_dd,
                    atr_ret = None,
                    regime  = regime,
                )

                if not np.isnan(comp):
                    if comp >= BUY_THRESHOLD:
                        rec["atr_ret"] = _atr_exit_return(close_arr, atr_arr, i)
                        high_records.append(dict(rec))
                    elif comp < LOW_THRESHOLD:
                        low_records.append(dict(rec))

                if rng.random() < _RANDOM_PROB:
                    random_records.append(dict(rec))

        buckets = {
            "HIGH":   _stats_from_records("HIGH",   high_records),
            "LOW":    _stats_from_records("LOW",    low_records),
            "RANDOM": _stats_from_records("RANDOM", random_records),
        }

        # ATR mean for HIGH
        atr_rets = [r["atr_ret"] for r in high_records if r["atr_ret"] is not None]
        if atr_rets and buckets["HIGH"].n_entries > 0:
            buckets["HIGH"].mean_return_atr = float(np.mean(atr_rets))

        return dict(
            buckets  = buckets,
            ablation = self._ablation(spy_regime),
            regime   = self._regime_breakdown(high_records),
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fwd_return(close_arr: np.ndarray, i: int, horizon: int) -> float | None:
        # Right-truncation: entries within `horizon` trading days of the data
        # end have no full forward window and return None. _stats_from_records
        # averages only non-None values, so the 12m mean is over entries with a
        # full 252-day look-ahead. In a rising tape this biases the 12m mean
        # slightly upward (entries that would run into an end-of-window drawdown
        # are excluded). The exclusion is intentional but asymmetric.
        j = i + horizon
        if j >= len(close_arr):
            return None
        ep = close_arr[i]
        if ep == 0:
            return None
        return float(close_arr[j] / ep - 1)

    def _spy_regime(self) -> dict:
        """Return {date: 'bull'|'bear'} using SPY SMA200 slope (20d)."""
        ohlcv = self.prices.get_prices("SPY", _WARMUP_START, self.end_date.strftime("%Y-%m-%d"))
        if ohlcv is None or ohlcv.empty:
            return {}
        sma200     = ohlcv["Close"].rolling(200).mean()
        sma200_20d = sma200.shift(20)
        bull       = sma200 > sma200_20d
        return {ts.date(): ("bull" if b else "bear") for ts, b in zip(ohlcv.index, bull)}

    def _ablation(self, spy_regime: dict) -> dict[str, dict]:
        """Disable each component one at a time; report HIGH/LOW means."""
        components = ["trend", "momentum", "volume", "rsi"]
        results: dict[str, dict] = {}

        for disabled in ["none"] + components:
            high_rets: list[float] = []
            low_rets:  list[float] = []

            for ticker in self.tickers:
                ohlcv = self.prices.get_prices(
                    ticker, _WARMUP_START, self.end_date.strftime("%Y-%m-%d")
                )
                if ohlcv is None or ohlcv.empty or len(ohlcv) < 200:
                    continue

                scored = compute_signals(ohlcv)
                if disabled != "none":
                    scored = scored.copy()
                    scored[f"{disabled}_score"] = 0.5

                # Recompute composite with the neutralised component
                has_all = (
                    scored["trend_score"].notna() &
                    scored["momentum_score"].notna() &
                    scored["volume_score"].notna() &
                    scored["rsi_score"].notna()
                )
                comp = (
                    WEIGHTS["trend"]    * scored["trend_score"].fillna(0) +
                    WEIGHTS["momentum"] * scored["momentum_score"].fillna(0) +
                    WEIGHTS["volume"]   * scored["volume_score"].fillna(0) +
                    WEIGHTS["rsi"]      * scored["rsi_score"].fillna(0)
                ).where(has_all)

                close_arr   = scored["Close"].to_numpy(dtype=float)
                comp_arr    = comp.to_numpy(dtype=float)
                dates       = scored.index

                for i, (ts, c) in enumerate(zip(dates, comp_arr)):
                    if ts < self.start_date or ts > self.end_date:
                        continue
                    fwd = self._fwd_return(close_arr, i, 252)
                    if fwd is None or np.isnan(c):
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

    @staticmethod
    def _regime_breakdown(high_records: list[dict]) -> dict[str, dict]:
        """Split HIGH entries by bull/bear regime."""
        bull = [r for r in high_records if r["regime"] == "bull"]
        bear = [r for r in high_records if r["regime"] == "bear"]

        def _summary(recs: list[dict]) -> dict:
            r12 = [r["fwd_12m"] for r in recs if r["fwd_12m"] is not None]
            return dict(
                n            = len(recs),
                mean_12m     = float(np.mean(r12)) if r12 else None,
                pct_positive = float(np.mean([x > 0 for x in r12])) if r12 else None,
            )

        return {"bull": _summary(bull), "bear": _summary(bear)}
