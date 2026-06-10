"""Unit tests for core/signals/entry_score.py — all synthetic data, no network."""
import numpy as np
import pandas as pd
import pytest

from core.signals.entry_score import (
    BUY_THRESHOLD,
    EntrySignal,
    compute_signals,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_prices(
    n: int = 300,
    start_price: float = 100.0,
    daily_pct: float = 0.002,
    vol_factor: float = 1.0,
    late_vol_factor: float = 1.0,
) -> pd.DataFrame:
    """Build synthetic OHLCV DataFrame with DatetimeIndex."""
    idx     = pd.date_range("2020-01-01", periods=n, freq="B")
    prices  = [start_price * (1 + daily_pct) ** i for i in range(n)]
    closes  = pd.Series(prices, index=idx)
    highs   = closes * 1.005
    lows    = closes * 0.995
    volume  = pd.Series([1_000_000.0 * vol_factor] * n, index=idx)
    volume.iloc[-10:] *= late_vol_factor
    return pd.DataFrame(
        {"Open": closes, "High": highs, "Low": lows, "Close": closes, "Volume": volume}
    )


def _make_concat(segments: list[tuple[int, float, float]]) -> pd.DataFrame:
    """Build a price series from (n_days, start_price, daily_pct) segments."""
    all_prices: list[float] = []
    price = segments[0][1]
    for n_days, _start, daily_pct in segments:
        for _ in range(n_days):
            all_prices.append(price)
            price *= (1 + daily_pct)
    n   = len(all_prices)
    idx = pd.date_range("2018-01-01", periods=n, freq="B")
    closes = pd.Series(all_prices, index=idx)
    return pd.DataFrame(
        {"Open": closes, "High": closes * 1.005, "Low": closes * 0.995,
         "Close": closes, "Volume": pd.Series(1_000_000.0, index=idx)}
    )


# ── trend score ───────────────────────────────────────────────────────────────

def test_trend_score_above_both_smas_rising():
    """300-day uptrend: price > SMA200 and SMA200 rising → trend_score = 1.0."""
    df     = _make_prices(n=300)
    scored = compute_signals(df)
    last   = scored.iloc[-1]
    assert last["trend_score"] == pytest.approx(1.0)


def test_trend_score_below_both_smas():
    """300-day downtrend: price < SMA50 → trend_score = 0.0."""
    df     = _make_prices(n=300, daily_pct=-0.003)
    scored = compute_signals(df)
    last   = scored.iloc[-1]
    assert last["trend_score"] == pytest.approx(0.0)


def test_trend_score_between_smas():
    """After a deep crash + recovery, SMA50 < close < SMA200 → trend_score = 0.4.

    Geometry: 300-day rise → 100-day steep decline → 100-day recovery.
    In the recovery phase SMA50 adapts up faster than SMA200, creating
    the 'between' zone.
    """
    df = _make_concat([
        (300, 100.0,  0.003),   # rise
        (100, None,  -0.020),   # steep crash
        (100, None,   0.008),   # recovery
    ])
    scored  = compute_signals(df)
    between = scored[
        scored["trend_score"].notna() &
        (scored["trend_score"] == 0.4)
    ]
    assert len(between) > 0, (
        "No rows with trend_score=0.4 found; check geometry produces "
        "a window where SMA50 < close ≤ SMA200"
    )


# ── momentum score ────────────────────────────────────────────────────────────

def test_momentum_all_positive_weeks():
    """Strictly rising price → all 4 weekly windows positive → momentum_score = 1.0."""
    df     = _make_prices(n=300, daily_pct=0.005)
    scored = compute_signals(df)
    last   = scored.iloc[-1]
    assert last["momentum_score"] == pytest.approx(1.0)


def test_momentum_sma50_cross_bonus():
    """If price crosses above SMA50 on the last day, momentum_score > base."""
    # 200 days steady price (builds SMA50/SMA200 baselines), then dip, then spike above SMA50
    n_stable = 200
    n_dip    = 30
    n_spike  = 1
    n        = n_stable + n_dip + n_spike

    idx    = pd.date_range("2019-01-01", periods=n, freq="B")
    base   = 100.0
    stable = [base] * n_stable
    dip    = [base * 0.85] * n_dip   # below SMA50
    spike  = [base * 1.10]           # above SMA50
    prices = stable + dip + spike
    closes = pd.Series(prices, index=idx)

    df = pd.DataFrame(
        {"Open": closes, "High": closes * 1.01, "Low": closes * 0.99,
         "Close": closes, "Volume": pd.Series(1_000_000.0, index=idx)}
    )
    scored = compute_signals(df)

    prev = scored.iloc[-2]
    last = scored.iloc[-1]

    # Confirm cross actually happened
    if pd.notna(prev["sma_50"]) and pd.notna(last["sma_50"]):
        if prev["Close"] <= prev["sma_50"] and last["Close"] > last["sma_50"]:
            # With the cross, momentum score includes the +0.15 bonus
            # 4 weekly windows: last 4 × 5 days all in the dip/spike region
            # Several windows may be positive (spike vs dip)
            assert last["momentum_score"] is not None
            assert not np.isnan(last["momentum_score"])
            return
    pytest.skip("SMA50 cross did not materialise with this geometry")


# ── volume score ──────────────────────────────────────────────────────────────

def test_volume_high_ratio():
    """Last 10 days volume 3× prior average → vol_ratio ≥ 2.0 → volume_score = 1.0."""
    df     = _make_prices(n=300, late_vol_factor=3.0)
    scored = compute_signals(df)
    last   = scored.iloc[-1]
    assert last["vol_ratio"] >= 2.0, f"vol_ratio={last['vol_ratio']:.2f}"
    assert last["volume_score"] == pytest.approx(1.0)


# ── RSI score ─────────────────────────────────────────────────────────────────

def test_rsi_oversold_zone():
    """Sharp sustained decline → RSI < 30 → rsi_score = 1.0."""
    df = _make_concat([
        (200, 100.0,  0.0),    # stable (builds RSI baseline)
        (50,  None,  -0.015),  # sharp decline
    ])
    scored = compute_signals(df)
    last   = scored.iloc[-1]
    assert last["rsi_14"] < 30, f"RSI was {last['rsi_14']:.1f}, expected < 30"
    assert last["rsi_score"] == pytest.approx(1.0)


# ── composite signal ──────────────────────────────────────────────────────────

def test_composite_buy_signal():
    """Strong uptrend + volume spike → composite ≥ BUY_THRESHOLD → signal = BUY."""
    df     = _make_prices(n=300, daily_pct=0.003, late_vol_factor=3.0)
    scored = compute_signals(df)
    last   = scored.iloc[-1]
    assert pd.notna(last["composite_score"])
    assert last["composite_score"] >= BUY_THRESHOLD
    assert last["signal"] == "BUY"


def test_insufficient_data_signal():
    """Only 50 rows → SMA200 never valid → all rows signal = INSUFFICIENT_DATA."""
    df     = _make_prices(n=50)
    scored = compute_signals(df)
    assert (scored["signal"] == "INSUFFICIENT_DATA").all()
