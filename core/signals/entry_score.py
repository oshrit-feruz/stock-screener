from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from core.data.prices import PriceData

# Theory-driven component weights — do NOT optimize to historical data.
WEIGHTS = {
    "trend":    0.30,
    "momentum": 0.30,
    "volume":   0.20,
    "rsi":      0.20,
}

BUY_THRESHOLD  = 0.65
LOW_THRESHOLD  = 0.40   # below this → LOW bucket in backtest


@dataclass
class EntrySignal:
    ticker: str
    as_of_date: date
    trend_score: float | None       # 0–1
    momentum_score: float | None    # 0–1
    volume_score: float | None      # 0–1
    rsi_score: float | None         # 0–1
    composite_score: float | None   # weighted average
    signal: str                     # "BUY" | "WAIT" | "INSUFFICIENT_DATA"


# ── indicator helpers ─────────────────────────────────────────────────────────

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    # When avg_loss=0 and avg_gain>0: RSI=100; both=0 (flat): RSI=50
    both_zero = (avg_gain == 0) & (avg_loss == 0)
    rs        = avg_gain / avg_loss.replace(0, 1e-10)
    rsi       = 100 - (100 / (1 + rs))
    rsi[both_zero] = 50.0
    return rsi


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


# ── scoring helpers (vectorized) ──────────────────────────────────────────────

def _trend_score_series(close: pd.Series, sma50: pd.Series, sma200: pd.Series) -> pd.Series:
    sma200_20d = sma200.shift(20)
    score = pd.Series(np.nan, index=close.index, dtype=float)
    above200_rising = (close > sma200) & (sma200 > sma200_20d)
    above200_flat   = (close > sma200) & (sma200 <= sma200_20d)
    between         = (close > sma50) & (close <= sma200)
    score[above200_rising] = 1.0
    score[above200_flat]   = 0.7
    score[between]         = 0.4
    score[(close <= sma50) & sma50.notna()] = 0.0
    return score


def _momentum_score_series(close: pd.Series, sma50: pd.Series) -> pd.Series:
    w1 = close / close.shift(5)  - 1
    w2 = close / close.shift(10) - 1
    w3 = close / close.shift(15) - 1
    w4 = close / close.shift(20) - 1
    pos_count = (
        (w1 > 0).astype(float) +
        (w2 > 0).astype(float) +
        (w3 > 0).astype(float) +
        (w4 > 0).astype(float)
    )
    base = pos_count / 4.0
    # SMA50 cross bonus: yesterday below, today above
    cross_bonus = ((close.shift(1) <= sma50.shift(1)) & (close > sma50)).astype(float) * 0.15
    return (base + cross_bonus).clip(upper=1.0)


def _volume_score_series(volume: pd.Series) -> pd.Series:
    vol10  = volume.rolling(10).mean()
    vol90  = volume.rolling(90).mean()
    ratio  = vol10 / vol90.replace(0, float("nan"))
    # np.select over one numpy array replaces five masked-Series assignments —
    # same tiers, same constants, bit-identical output (NaN ratio matches no
    # condition -> NaN), without the per-assignment pandas overhead.
    r = ratio.to_numpy()
    with np.errstate(invalid="ignore"):
        score = np.select(
            [r >= 2.0,
             r >= 1.5,          # implies < 2.0 (earlier conditions win)
             r >= 1.0,
             r >= 0.8,
             r < 0.8],
            [1.0, 0.8, 0.5, 0.3, 0.1],
            default=np.nan,
        )
    return pd.Series(score, index=volume.index, dtype=float)


def _rsi_score_series(rsi: pd.Series) -> pd.Series:
    score = pd.Series(np.nan, index=rsi.index, dtype=float)
    score[rsi.notna() & (rsi < 30)]                    = 1.0
    score[rsi.notna() & (rsi >= 30) & (rsi < 45)]     = 0.8
    score[rsi.notna() & (rsi >= 45) & (rsi < 60)]     = 0.6
    score[rsi.notna() & (rsi >= 60) & (rsi < 70)]     = 0.3
    score[rsi.notna() & (rsi >= 70)]                   = 0.0
    return score


# ── public API ────────────────────────────────────────────────────────────────

def compute_signals(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Add indicator and score columns to a full OHLCV DataFrame.

    Input must have Close, High, Low, Volume columns (DatetimeIndex).
    Returns a copy with added columns:
      sma_50, sma_200, rsi_14, atr_14, vol_ratio,
      trend_score, momentum_score, volume_score, rsi_score,
      composite_score, signal
    """
    df = ohlcv.copy()
    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    df["sma_50"]  = close.rolling(50).mean()
    df["sma_200"] = close.rolling(200).mean()
    df["rsi_14"]  = _rsi(close)
    df["atr_14"]  = _atr(high, low, close)

    vol10 = volume.rolling(10).mean()
    vol90 = volume.rolling(90).mean()
    df["vol_ratio"] = vol10 / vol90.replace(0, float("nan"))

    df["trend_score"]    = _trend_score_series(close, df["sma_50"], df["sma_200"])
    df["momentum_score"] = _momentum_score_series(close, df["sma_50"])
    df["volume_score"]   = _volume_score_series(volume)
    df["rsi_score"]      = _rsi_score_series(df["rsi_14"])

    has_all = (
        df["trend_score"].notna() &
        df["momentum_score"].notna() &
        df["volume_score"].notna() &
        df["rsi_score"].notna()
    )
    composite = (
        WEIGHTS["trend"]    * df["trend_score"].fillna(0) +
        WEIGHTS["momentum"] * df["momentum_score"].fillna(0) +
        WEIGHTS["volume"]   * df["volume_score"].fillna(0) +
        WEIGHTS["rsi"]      * df["rsi_score"].fillna(0)
    )
    df["composite_score"] = composite.where(has_all)

    def _sig(row: pd.Series) -> str:
        if pd.isna(row["composite_score"]):
            return "INSUFFICIENT_DATA"
        return "BUY" if row["composite_score"] >= BUY_THRESHOLD else "WAIT"

    df["signal"] = df.apply(_sig, axis=1)
    return df


def get_signal(ticker: str, as_of_date: date, prices: PriceData) -> EntrySignal:
    """Return entry signal for ticker as of as_of_date."""
    ohlcv = prices.get_prices(ticker, "2016-01-01", as_of_date.isoformat())
    if ohlcv is None or ohlcv.empty or len(ohlcv) < 200:
        return EntrySignal(ticker, as_of_date, None, None, None, None, None, "INSUFFICIENT_DATA")

    scored = compute_signals(ohlcv)
    mask   = scored.index <= pd.Timestamp(as_of_date)
    if not mask.any():
        return EntrySignal(ticker, as_of_date, None, None, None, None, None, "INSUFFICIENT_DATA")

    row = scored.loc[mask].iloc[-1]

    def _fv(col: str) -> float | None:
        v = row.get(col)
        return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)

    return EntrySignal(
        ticker          = ticker,
        as_of_date      = as_of_date,
        trend_score     = _fv("trend_score"),
        momentum_score  = _fv("momentum_score"),
        volume_score    = _fv("volume_score"),
        rsi_score       = _fv("rsi_score"),
        composite_score = _fv("composite_score"),
        signal          = str(row.get("signal", "INSUFFICIENT_DATA")),
    )
