from __future__ import annotations

from dataclasses import dataclass
from datetime import date

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
    # NOTE: signature and semantics are frozen — scripts/run_dip_40_60_validation.py
    # monkey-patches this function, so it must remain callable as f(close).
    high_52w     = close.rolling(252).max()
    drawdown_abs = ((high_52w - close) / high_52w).clip(lower=0)

    # Tier structure validated by sensitivity analysis (pre-Stage 6).
    # Sweet spot 40-60%; 20-30% shallow band dropped (negative spread in testing).
    # np.select over one numpy array replaces five masked-Series assignments —
    # same tiers, same float constants, bit-identical output, ~none of the
    # per-assignment pandas overhead (verified equal on the full universe).
    dd = drawdown_abs.to_numpy()
    with np.errstate(invalid="ignore"):
        score = np.select(
            [dd < 0.30,
             dd < 0.40,          # implies >= 0.30 (earlier conditions win)
             dd <= 0.60,         # implies >= 0.40
             dd <= 0.70,         # implies >  0.60
             dd > 0.70],
            [0.0, 0.7, 1.0, 0.5, 0.0],
            default=np.nan,      # NaN drawdown matches no condition -> NaN
        )
    return pd.Series(score, index=close.index, dtype=float)


def _recovery_score_series(close: pd.Series) -> pd.Series:
    c = close.to_numpy(dtype=float)
    n = len(c)
    # w_k = close/close.shift(k) - 1, evaluated positionally on numpy (identical
    # to the former eight shifted-Series divisions, without eight Series allocs).
    pos_counts_recent = np.zeros(n)
    pos_counts_prior  = np.zeros(n)
    all_valid = np.ones(n, dtype=bool)
    with np.errstate(invalid="ignore", divide="ignore"):
        for k, bucket in ((5, "r"), (10, "r"), (15, "r"), (20, "r"),
                          (25, "p"), (30, "p"), (35, "p"), (40, "p")):
            w = np.full(n, np.nan)
            w[k:] = c[k:] / c[:-k] - 1
            valid = ~np.isnan(w)
            all_valid &= valid
            if bucket == "r":
                pos_counts_recent += (w > 0)
            else:
                pos_counts_prior += (w > 0)

    reversal_context = all_valid & (pos_counts_prior <= 1)   # prior was mostly down
    score = np.select(
        [reversal_context & (pos_counts_recent >= 3),
         reversal_context & (pos_counts_recent >= 2),
         all_valid],
        [1.0, 0.6, 0.0],
        default=np.nan,
    )
    return pd.Series(score, index=close.index, dtype=float)


# ── public API ────────────────────────────────────────────────────────────────

def passes_quality_gate(snap: FundamentalSnapshot | None) -> bool | None:
    """Return True/False/None (None = no fundamental data available at all).

    Fail-closed: de=None (negative/zero equity or missing LT-debt concept) → False.
    """
    if snap is None:
        return None
    rev  = snap.revenue_growth_yoy
    de   = snap.debt_to_equity
    nm   = snap.net_margin
    if rev is None or nm is None:
        return None
    if de is None:
        return False   # equity ≤0 or LT-debt not found → D/E unbounded → FAIL
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
    elif gate is not True:
        # Fail-closed: gate False (failed) OR None (no fundamental data) → SKIP.
        # Never recommend a buy without confirmed fundamentals.
        signal = "SKIP"
    elif comp >= BUY_THRESHOLD:
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
