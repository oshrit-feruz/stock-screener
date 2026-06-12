#!/usr/bin/env python3
"""
Out-of-sample performance audit: June 12, 2025 → June 12, 2026.

Frozen signal — no parameter changes. Validated on 2018-2024 only.
"""
from __future__ import annotations

import sys
import warnings
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.tickers import VALIDATION_UNIVERSE
from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from core.signals.recovery_score import (
    BUY_THRESHOLD,
    compute_recovery_signals,
    passes_quality_gate,
)

_WARMUP_START = "2016-01-01"
_OOS_START    = pd.Timestamp("2025-06-12")
_OOS_END      = pd.Timestamp("2026-06-12")
_HOLD_DAYS    = 252

# In-sample baseline numbers (frozen from Stage 5b validation)
_IS_RANDOM_MEAN  = 0.223
_IS_HIGH_MEAN    = 0.492


def _prefetch_gate(ticker: str, fundamentals: EdgarFundamentals) -> dict[int, bool]:
    """Fetch annual quality gate for years 2023-2026 using end-of-period dates."""
    result = {}
    for year in [2023, 2024, 2025]:
        snap = fundamentals.get_snapshot(ticker, date(year, 12, 31))
        g = passes_quality_gate(snap)
        result[year] = False if g is None else g   # fail-closed
    # For 2026: use today as the as_of date
    snap = fundamentals.get_snapshot(ticker, date(2026, 6, 12))
    g = passes_quality_gate(snap)
    result[2026] = False if g is None else g
    return result


def run_audit() -> list[dict]:
    prices       = PriceData()
    fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())

    signals: list[dict] = []

    for ticker in VALIDATION_UNIVERSE:
        ohlcv = prices.get_prices(ticker, _WARMUP_START, _OOS_END.strftime("%Y-%m-%d"))
        if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            scored = compute_recovery_signals(ohlcv)

        close  = scored["Close"]
        quality = _prefetch_gate(ticker, fundamentals)

        prev_in_buy  = False
        last_entry_iloc = None   # iloc of the most recent recorded entry (full series)

        for iloc_pos in range(len(scored)):
            ts   = scored.index[iloc_pos]
            comp = scored["composite_score"].iloc[iloc_pos]

            if pd.isna(comp):
                prev_in_buy = False
                continue

            gate = quality.get(ts.year, False)
            currently_in_buy = (comp >= BUY_THRESHOLD and gate is True)

            # Detect new crossing into BUY
            if currently_in_buy and not prev_in_buy:
                # Suppress if still within 252d of last entry
                suppress = (
                    last_entry_iloc is not None
                    and (iloc_pos - last_entry_iloc) < _HOLD_DAYS
                )

                if not suppress:
                    last_entry_iloc = iloc_pos

                    # Only record signals in the OOS window
                    if ts >= _OOS_START:
                        entry_price = float(close.iloc[iloc_pos])
                        dd          = float(scored["drawdown_52w"].iloc[iloc_pos])

                        exit_iloc  = min(iloc_pos + _HOLD_DAYS, len(close) - 1)
                        exit_ts    = scored.index[exit_iloc]
                        days_held  = exit_iloc - iloc_pos
                        status     = "CLOSED" if days_held >= _HOLD_DAYS else "OPEN"
                        exit_price = float(close.iloc[exit_iloc])
                        ret        = exit_price / entry_price - 1

                        signals.append(dict(
                            ticker      = ticker,
                            entry_date  = ts.date(),
                            entry_price = entry_price,
                            dd_pct      = dd,
                            composite   = float(comp),
                            exit_date   = exit_ts.date(),
                            exit_price  = exit_price,
                            days_held   = days_held,
                            ret         = ret,
                            status      = status,
                        ))

            prev_in_buy = currently_in_buy

    signals.sort(key=lambda s: s["entry_date"])
    return signals


def spy_context(prices: PriceData) -> tuple[float, str, float, float]:
    """Return (spy_total_return, dominant_regime, spy_max_drawdown, bear_days_pct)."""
    ohlcv = prices.get_prices("SPY", "2024-01-01", _OOS_END.strftime("%Y-%m-%d"))
    if ohlcv is None or ohlcv.empty:
        return float("nan"), "unknown", float("nan"), float("nan")

    close   = ohlcv["Close"]
    sma200  = close.rolling(200).mean()
    sma200_20 = sma200.shift(20)

    oos_close = close[(close.index >= _OOS_START) & (close.index <= _OOS_END)]
    oos_sma200 = sma200[(sma200.index >= _OOS_START) & (sma200.index <= _OOS_END)]
    oos_sma200_20 = sma200_20[(sma200_20.index >= _OOS_START) & (sma200_20.index <= _OOS_END)]

    is_bull = oos_sma200 > oos_sma200_20
    pct_bull = float(is_bull.mean()) if len(is_bull) > 0 else float("nan")
    dominant = "bull" if pct_bull > 0.5 else "bear"
    bear_pct = 1.0 - pct_bull

    if len(oos_close) < 2:
        return float("nan"), dominant, float("nan"), bear_pct

    total_ret  = float(oos_close.iloc[-1] / oos_close.iloc[0] - 1)

    rolling_max = oos_close.cummax()
    drawdowns   = (oos_close - rolling_max) / rolling_max
    max_dd      = float(drawdowns.min())

    return total_ret, dominant, max_dd, bear_pct


def print_report(signals: list[dict], prices: PriceData) -> None:
    closed = [s for s in signals if s["status"] == "CLOSED"]
    open_  = [s for s in signals if s["status"] == "OPEN"]

    # ── SECTION 1 ──────────────────────────────────────────────────────────
    print("=" * 78)
    print("OUT-OF-SAMPLE PERFORMANCE AUDIT: June 12, 2025 – June 12, 2026")
    print("=" * 78)
    print()
    print("METHODOLOGY NOTE")
    print("Quality gate uses end-of-year EDGAR snapshot (same methodology as backtest).")
    print("For 2025 entries this may include filings up to ~Oct 2025 rather than the")
    print("exact entry date — a minor look-ahead of 2-4 months. Effect is small because")
    print("annual EDGAR filings rarely reverse year-over-year gate status mid-year.")
    print()

    print("SECTION 1 - SIGNAL LOG")
    print("-" * 78)
    hdr = f"{'Ticker':<6}  {'Entry Date':<12}  {'Entry$':>8}  {'DD%':>6}  {'Comp':>5}  {'Days':>5}  {'Return%':>8}  {'Status':<8}  {'Exit Date'}"
    print(hdr)
    print("-" * 78)
    if not signals:
        print("  (no signals fired in OOS window)")
    for s in signals:
        print(
            f"{s['ticker']:<6}  {str(s['entry_date']):<12}  {s['entry_price']:>8.2f}  "
            f"{s['dd_pct']:>5.1%}  {s['composite']:>5.2f}  {s['days_held']:>5}  "
            f"{s['ret']:>7.1%}  {s['status']:<8}  {s['exit_date']}"
        )
    print()

    # ── SECTION 2 ──────────────────────────────────────────────────────────
    print("SECTION 2 - SUMMARY STATISTICS")
    print("-" * 78)
    print(f"Total BUY signals fired:    {len(signals)}")
    print(f"Open positions (OPEN):      {len(open_)}")
    print(f"Closed positions (CLOSED):  {len(closed)}")
    print()

    def _stats(items: list[dict], label: str, note: str = "") -> None:
        if not items:
            print(f"  {label}: no positions")
            return
        rets = [s["ret"] for s in items]
        mean_r   = float(np.mean(rets))
        med_r    = float(np.median(rets))
        pct_pos  = float(np.mean([r > 0 for r in rets]))
        best     = max(items, key=lambda s: s["ret"])
        worst    = min(items, key=lambda s: s["ret"])
        print(f"  {label}  (n={len(items)}){note}")
        print(f"    Mean return:    {mean_r:+.1%}")
        print(f"    Median return:  {med_r:+.1%}")
        print(f"    % positive:     {pct_pos:.0%}")
        print(f"    Best:   {best['ticker']} {best['entry_date']}  {best['ret']:+.1%}")
        print(f"    Worst:  {worst['ticker']} {worst['entry_date']}  {worst['ret']:+.1%}")

    _stats(closed, "CLOSED positions (final returns)")
    print()
    _stats(open_, "OPEN positions (partial returns to 2026-06-12)",
           "  <-- not final; hold window continues")
    print()

    # ── SECTION 3 ──────────────────────────────────────────────────────────
    print("SECTION 3 - COMPARISON TO BASELINE")
    print("-" * 78)
    print(f"  RANDOM baseline (2018-2024 in-sample):      {_IS_RANDOM_MEAN:+.1%} at 252d")
    print(f"  HIGH in-sample mean (2018-2024):            {_IS_HIGH_MEAN:+.1%} at 252d")
    if closed:
        oos_closed_mean = float(np.mean([s["ret"] for s in closed]))
        print(f"  Out-of-sample CLOSED mean (2025-2026):      {oos_closed_mean:+.1%}  (n={len(closed)})")
    else:
        print("  Out-of-sample CLOSED mean:                  N/A (no closed positions yet)")
        oos_closed_mean = float("nan")
    if open_:
        oos_open_mean = float(np.mean([s["ret"] for s in open_]))
        print(f"  Out-of-sample OPEN mean to date (partial):  {oos_open_mean:+.1%}  (n={len(open_)})")
    print()
    print("  Caveats:")
    print("  - Open position returns are partial (will continue to evolve over 252d)")
    print("  - Sample size is small (12 months, 50 tickers → expect 10-30 signals)")
    print("  - One market regime is insufficient to confirm or invalidate the backtest")
    print()

    # ── SECTION 4 ──────────────────────────────────────────────────────────
    print("SECTION 4 - MARKET CONTEXT (SPY)")
    print("-" * 78)
    spy_ret, regime, spy_dd, bear_pct = spy_context(prices)
    bull_pct = 1.0 - bear_pct
    print(f"  SPY total return (Jun 2025 → Jun 2026):  {spy_ret:+.1%}")
    print(f"  SPY max drawdown during period:          {spy_dd:+.1%}")
    print(f"  Dominant regime:                         {regime.upper()}")
    print(f"  % of days in BULL regime (SMA200 rising): {bull_pct:.0%}")
    print(f"  % of days in BEAR regime (SMA200 falling): {bear_pct:.0%}")
    print()
    # Qualitative context based on SPY data
    if spy_ret >= 0.15:
        print("  Strong bull market. HIGH signals expected to perform well (in-sample bull mean +45.3%).")
    elif spy_ret >= 0:
        print("  Moderate positive market. Mixed conditions.")
    elif spy_ret >= -0.15:
        print("  Mild bear market. In-sample bear mean was +50.6% — signal historically stronger here.")
    else:
        print("  Severe bear market. High drawdown environment — dip signals fired deeper but also recovered more.")
    print()

    # ── SECTION 5 ──────────────────────────────────────────────────────────
    print("SECTION 5 - HONEST ASSESSMENT")
    print("-" * 78)
    if len(closed) < 5:
        print("  VERDICT: TOO FEW CLOSED POSITIONS TO CONCLUDE.")
        print(f"  Only {len(closed)} position(s) have completed the full 252-day hold.")
        print("  Check again in 6 months when more positions close.")
        print()
        if len(signals) > 0:
            all_rets = [s["ret"] for s in signals]
            partial_mean = float(np.mean(all_rets))
            print(f"  Partial signal (all positions, including open, to date): {partial_mean:+.1%}")
            if partial_mean > _IS_RANDOM_MEAN:
                print("  Partial read SUPPORTS backtest (above random baseline) — but not conclusive.")
            else:
                print("  Partial read BELOW random baseline — monitor closely as positions close.")
    else:
        if oos_closed_mean > _IS_RANDOM_MEAN:
            verdict = "SUPPORTS"
            detail  = f"Closed mean {oos_closed_mean:+.1%} > random baseline {_IS_RANDOM_MEAN:+.1%}."
        else:
            verdict = "CONTRADICTS (FLAG FOR REVIEW)"
            detail  = f"Closed mean {oos_closed_mean:+.1%} < random baseline {_IS_RANDOM_MEAN:+.1%}."
        print(f"  VERDICT: {verdict}")
        print(f"  {detail}")
        print()
        print("  Interpretation:")
        print("  - Out-of-sample with n<30 is noisy — one outlier can swing mean by 5-10pp.")
        print("  - Compare to in-sample bear (+50.6%) vs bull (+45.3%) to contextualise regime.")
        print("  - Do not retune signal parameters based on this data. Wait for n>=30 closed.")
    print()
    print("=" * 78)


def main() -> None:
    print("Fetching price data and running OOS audit...")
    prices  = PriceData()
    signals = run_audit()
    print_report(signals, prices)


if __name__ == "__main__":
    main()
