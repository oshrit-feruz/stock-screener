from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from core.data.fundamentals import FundamentalSnapshot
from core.data.prices import PriceData
from core.signals.entry_score import (
    _atr,
    _momentum_score_series,
    _rsi,
    _volume_score_series,
)

# Theory-driven weights — do NOT optimize to historical data.
# v2: recovery_score removed (ablation showed it adds noise; dip is the primary driver).
WEIGHTS = {
    "dip":      0.50,
    "momentum": 0.30,
    "volume":   0.20,
}

BUY_THRESHOLD = 0.60
LOW_THRESHOLD = 0.35


@dataclass
class RecoverySignal:
    ticker: str
    as_of_date: date
    dip_score: float | None        # 0–1
    recovery_score: float | None   # 0–1
    momentum_score: float | None   # 0–1
    volume_score: float | None     # 0–1
    composite_score: float | None  # weighted average
    quality_gate: bool | None      # True=passed, False=failed, None=unknown
    signal: str                    # "BUY"|"WAIT"|"SKIP"|"INSUFFICIENT_DATA"


# ── scoring helpers ───────────────────────────────────────────────────────────

def _dip_score_series(close: pd.Series) -> pd.Series:
    high_52w     = close.rolling(252).max()
    drawdown_abs = ((high_52w - close) / high_52w).clip(lower=0)

    score = pd.Series(np.nan, index=close.index, dtype=float)
    valid = drawdown_abs.notna()
    score[valid & (drawdown_abs < 0.20)]                             = 0.0
    score[valid & (drawdown_abs >= 0.20) & (drawdown_abs < 0.30)]   = 0.7
    score[valid & (drawdown_abs >= 0.30) & (drawdown_abs <= 0.50)]  = 1.0
    score[valid & (drawdown_abs > 0.50)  & (drawdown_abs <= 0.70)]  = 0.5
    score[valid & (drawdown_abs > 0.70)]                             = 0.0
    return score


def _recovery_score_series(close: pd.Series) -> pd.Series:
    w1 = close / close.shift(5)  - 1
    w2 = close / close.shift(10) - 1
    w3 = close / close.shift(15) - 1
    w4 = close / close.shift(20) - 1
    w5 = close / close.shift(25) - 1
    w6 = close / close.shift(30) - 1
    w7 = close / close.shift(35) - 1
    w8 = close / close.shift(40) - 1

    n_pos_recent = (
        (w1 > 0).astype(float) + (w2 > 0).astype(float) +
        (w3 > 0).astype(float) + (w4 > 0).astype(float)
    )
    n_pos_prior = (
        (w5 > 0).astype(float) + (w6 > 0).astype(float) +
        (w7 > 0).astype(float) + (w8 > 0).astype(float)
    )

    # Only compute where all 8 windows have data
    all_valid = w1.notna() & w2.notna() & w3.notna() & w4.notna() & \
                w5.notna() & w6.notna() & w7.notna() & w8.notna()

    score = pd.Series(np.nan, index=close.index, dtype=float)
    score[all_valid] = 0.0

    reversal_context = all_valid & (n_pos_prior <= 1)   # prior was mostly down
    score[reversal_context & (n_pos_recent >= 2)] = 0.6
    score[reversal_context & (n_pos_recent >= 3)] = 1.0
    return score


# ── public API ────────────────────────────────────────────────────────────────

def passes_quality_gate(snap: FundamentalSnapshot | None) -> bool | None:
    """Return True/False/None (None = no data available)."""
    if snap is None:
        return None
    rev  = snap.revenue_growth_yoy
    de   = snap.debt_to_equity
    nm   = snap.net_margin
    if rev is None or de is None or nm is None:
        return None
    return bool(rev > 0 and de < 3 and nm > 0)


def compute_recovery_signals(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Add recovery-detector indicator and score columns to a full OHLCV DataFrame.

    Input: Close, High, Low, Volume columns with DatetimeIndex.
    Adds: high_52w, drawdown_52w, sma_50, rsi_14, atr_14,
          dip_score, recovery_score, momentum_score, volume_score, composite_score
    """
    df     = ohlcv.copy()
    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    df["sma_50"]     = close.rolling(50).mean()
    df["rsi_14"]     = _rsi(close)
    df["atr_14"]     = _atr(high, low, close)
    df["high_52w"]   = close.rolling(252).max()
    df["drawdown_52w"] = ((df["high_52w"] - close) / df["high_52w"]).clip(lower=0)

    df["dip_score"]      = _dip_score_series(close)
    df["recovery_score"] = _recovery_score_series(close)
    df["momentum_score"] = _momentum_score_series(close, df["sma_50"])
    df["volume_score"]   = _volume_score_series(volume)

    has_all = (
        df["dip_score"].notna() &
        df["momentum_score"].notna() &
        df["volume_score"].notna()
    )
    composite = (
        WEIGHTS["dip"]      * df["dip_score"].fillna(0) +
        WEIGHTS["momentum"] * df["momentum_score"].fillna(0) +
        WEIGHTS["volume"]   * df["volume_score"].fillna(0)
    )
    df["composite_score"] = composite.where(has_all)
    return df


def get_recovery_signal(
    ticker: str,
    as_of_date: date,
    prices: PriceData,
    fundamentals=None,
) -> RecoverySignal:
    """Return RecoverySignal for ticker as of as_of_date."""
    ohlcv = prices.get_prices(ticker, "2016-01-01", as_of_date.isoformat())
    if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
        return RecoverySignal(
            ticker, as_of_date, None, None, None, None, None, None,
            "INSUFFICIENT_DATA",
        )

    scored = compute_recovery_signals(ohlcv)
    mask   = scored.index <= pd.Timestamp(as_of_date)
    if not mask.any():
        return RecoverySignal(
            ticker, as_of_date, None, None, None, None, None, None,
            "INSUFFICIENT_DATA",
        )

    row = scored.loc[mask].iloc[-1]

    def _fv(col: str) -> float | None:
        v = row.get(col)
        return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)

    comp  = _fv("composite_score")
    gate  = None
    if fundamentals is not None:
        snap = fundamentals.get_snapshot(ticker, as_of_date)
        gate = passes_quality_gate(snap)

    if comp is None:
        signal = "INSUFFICIENT_DATA"
    elif gate is False:
        signal = "SKIP"
    elif comp >= BUY_THRESHOLD and gate is not False:
        signal = "BUY"
    else:
        signal = "WAIT"

    return RecoverySignal(
        ticker          = ticker,
        as_of_date      = as_of_date,
        dip_score       = _fv("dip_score"),
        recovery_score  = _fv("recovery_score"),
        momentum_score  = _fv("momentum_score"),
        volume_score    = _fv("volume_score"),
        composite_score = comp,
        quality_gate    = gate,
        signal          = signal,
    )
