"""Unit tests for core/signals/recovery_score.py — synthetic data only, no network."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from core.data.fundamentals import FundamentalSnapshot
from core.signals.recovery_score import (
    BUY_THRESHOLD,
    compute_recovery_signals,
    passes_quality_gate,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_prices(prices: list[float], start: str = "2016-01-01") -> pd.DataFrame:
    n   = len(prices)
    idx = pd.date_range(start, periods=n, freq="B")
    closes = pd.Series(prices, index=idx)
    return pd.DataFrame(
        {"Open": closes, "High": closes * 1.005, "Low": closes * 0.995,
         "Close": closes, "Volume": pd.Series(1_000_000.0, index=idx)}
    )


def _rising(n: int, start: float = 100.0, pct: float = 0.003) -> list[float]:
    return [start * (1 + pct) ** i for i in range(n)]


def _falling(n: int, start: float, pct: float = 0.015) -> list[float]:
    return [start * (1 - pct) ** i for i in range(n)]


def _flat(n: int, value: float) -> list[float]:
    return [value] * n


# ── dip_score ─────────────────────────────────────────────────────────────────

def test_dip_score_sweet_spot():
    """35% below 252-day high → dip_score = 1.0."""
    # Build: 300 days rising to peak, then 50 days falling ~35%
    base   = _rising(300)
    peak   = base[-1]
    target = peak * (1 - 0.35)
    # Linear drop over 50 days
    drop   = [peak - (peak - target) * i / 49 for i in range(50)]
    scored = compute_recovery_signals(_make_prices(base + drop))
    last   = scored.iloc[-1]
    assert last["dip_score"] == pytest.approx(1.0), f"drawdown={last['drawdown_52w']:.3f}"


def test_dip_score_shallow_dip():
    """10% below 252-day high → dip_score = 0.0."""
    base   = _rising(300)
    peak   = base[-1]
    drop   = [peak * (1 - 0.10)] * 10
    scored = compute_recovery_signals(_make_prices(base + drop))
    last   = scored.iloc[-1]
    assert last["dip_score"] == pytest.approx(0.0)


def test_dip_score_catastrophic():
    """80% below 252-day high → dip_score = 0.0."""
    base   = _rising(300)
    peak   = base[-1]
    drop   = [peak * 0.20] * 10   # 80% drawdown
    scored = compute_recovery_signals(_make_prices(base + drop))
    last   = scored.iloc[-1]
    assert last["dip_score"] == pytest.approx(0.0)


def test_dip_score_moderate_dip():
    """25% below 252-day high → dip_score = 0.7."""
    base   = _rising(300)
    peak   = base[-1]
    target = peak * (1 - 0.25)
    drop   = [peak - (peak - target) * i / 19 for i in range(20)]
    scored = compute_recovery_signals(_make_prices(base + drop))
    last   = scored.iloc[-1]
    assert last["dip_score"] == pytest.approx(0.7)


# ── recovery_score ────────────────────────────────────────────────────────────

def test_recovery_score_strong_reversal():
    """3/4 recent weeks up, 0/4 prior weeks up → recovery_score = 1.0."""
    # Build series: 300-day base, then 4 down weeks, then 3 up weeks
    base = _rising(300)
    # Prior 4 weeks (w5-w8): 4 down weeks → 20 days falling
    down = _falling(20, base[-1], pct=0.010)
    # Recent 3 weeks (w1-w3): 15 days rising + 1 flat
    up   = _rising(15, down[-1], pct=0.008) + [down[-1] * 1.008 ** 14]
    scored = compute_recovery_signals(_make_prices(base + down + up))
    last   = scored.iloc[-1]
    assert last["recovery_score"] == pytest.approx(1.0), \
        f"recovery_score={last['recovery_score']}"


def test_recovery_score_no_reversal():
    """Prior 4 weeks were up (no reversal context) → recovery_score = 0.0."""
    # All rising: prior 4 weeks up, recent 4 weeks up → n_pos_prior = 4 > 1 → score = 0.0
    prices = _rising(340)
    scored = compute_recovery_signals(_make_prices(prices))
    last   = scored.iloc[-1]
    assert last["recovery_score"] == pytest.approx(0.0)


# ── quality_gate ──────────────────────────────────────────────────────────────

def test_quality_gate_passes():
    """Healthy fundamentals → passes_quality_gate = True."""
    snap = FundamentalSnapshot(
        statement_date     = date(2022, 9, 30),
        revenue_growth_yoy = 0.10,
        debt_to_equity     = 1.5,
        roe                = 0.15,
        net_margin         = 0.08,
    )
    assert passes_quality_gate(snap) is True


def test_quality_gate_fails_negative_revenue():
    """Negative revenue growth → passes_quality_gate = False."""
    snap = FundamentalSnapshot(
        statement_date     = date(2022, 9, 30),
        revenue_growth_yoy = -0.05,
        debt_to_equity     = 1.5,
        roe                = 0.10,
        net_margin         = 0.05,
    )
    assert passes_quality_gate(snap) is False


# ── composite / signal ────────────────────────────────────────────────────────

def test_insufficient_data_signal():
    """< 252 rows → composite_score = NaN for all rows."""
    prices = _rising(100)
    scored = compute_recovery_signals(_make_prices(prices))
    assert scored["composite_score"].isna().all()
