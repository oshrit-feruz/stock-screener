#!/usr/bin/env python3
"""Uninvested-cash variants on the V1 signal portfolio (10%/max10, 252d hold).

The V1 baseline lets idle cash — capital not currently deployed in an active
position — earn 0%. This study re-runs the *same* signal-driven strategy under
four different treatments of that idle cash and compares the results:

  ZERO         idle cash earns 0%               (baseline V1)
  MONEY_MARKET idle cash earns 4.5%/yr, daily pro-rata
  SPY          idle cash sits in SPY, riding its actual day-to-day path. A new
               position is funded by selling SPY at the signal-day price; when a
               position closes on day 252 the proceeds return to SPY at the
               close-day price.
  SPY_NEUTRAL  idle cash earns SPY's realized full-window CAGR as a *smooth*
               daily rate — SPY's average return with the path (and therefore
               the sell-at-the-bottom timing) neutralised.

Only the idle-cash return differs across variants; signals, sizing (10% of
portfolio value, max 10 concurrent), and the 252-trading-day hold are identical.

For the SPY variant we also measure the average SPY price on days we sold SPY to
fund a position versus the SPY price 30 calendar days later, to quantify the cost
of being forced to sell at a market dislocation (the bottom).

Output: a comparison table, a quantified timing-cost line for the SPY variant,
and a portfolio-value chart (results/uninvested_cash_variants.png).
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from collections import defaultdict

from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from scripts.run_portfolio_sim import (
    _HOLD_DAYS,
    _INITIAL_CAP,
    _MIN_POSITION,
    _SIM_END,
    _SIM_START,
    load_all_data,
)

# V1 sizing (base case): 10% of portfolio value per signal, max 10 concurrent.
_PCT = 0.10
_MAX_POS = 10
_MM_RATE = 0.045          # money-market annual yield
_TIMING_WINDOW_DAYS = 30  # SPY sell-day vs N-days-later comparison


def _years_between(cal: pd.DatetimeIndex) -> float:
    return (cal[-1] - cal[0]).days / 365.25


def simulate_cash_variant(
    crossings_by_ticker: dict,
    prices_wide: pd.DataFrame,
    master_cal: pd.DatetimeIndex,
    spy_on_cal: np.ndarray,
    cash_mode: str,
    mm_annual_rate: float,
    spy_neutral_annual_rate: float,
) -> dict:
    """One portfolio run under a given idle-cash treatment.

    cash_mode in {"zero", "money_market", "spy", "spy_neutral"}.

    Returns daily portfolio values plus, for the SPY mode, the list of SPY prices
    on days cash was withdrawn from the SPY sleeve to fund a position.
    """
    events_by_date: dict = defaultdict(list)
    for ticker, crossings in crossings_by_ticker.items():
        for ts, comp, price, dd in crossings:
            if _SIM_START <= ts <= master_cal[-1]:
                events_by_date[ts].append((ticker, comp, price, dd))

    sim_prices = prices_wide.reindex(master_cal, method="ffill")
    prices_arr = sim_prices.values.astype(float)
    col_map = {c: i for i, c in enumerate(sim_prices.columns)}

    # Calendar-day gaps between consecutive trading days (for accrual modes).
    # Resolution-proof: derive days from the actual timestamps, not raw asi8
    # (pandas may store ns or us, so a fixed divisor is unsafe).
    day_gap = master_cal.to_series().diff().dt.total_seconds().values / 86400.0
    day_gap[0] = 0.0

    def _price(day_idx: int, ticker: str) -> float:
        ci = col_map.get(ticker)
        if ci is None:
            return float("nan")
        return float(prices_arr[day_idx, ci])

    def _positions_val(day_idx: int, positions: dict) -> float:
        v = 0.0
        for pos in positions.values():
            p = _price(day_idx, pos["ticker"])
            v += pos["shares"] * (p if not np.isnan(p) else pos["entry_price"])
        return v

    def _cash_factor(day_idx: int) -> float:
        """Growth applied to idle cash from the previous trading day to today."""
        if day_idx == 0:
            return 1.0
        dt = day_gap[day_idx]
        if cash_mode == "zero":
            return 1.0
        if cash_mode == "money_market":
            # Annual rate, accrued pro-rata per calendar day (daily compounding).
            return (1.0 + mm_annual_rate) ** (dt / 365.0)
        if cash_mode == "spy_neutral":
            return (1.0 + spy_neutral_annual_rate) ** (dt / 365.0)
        if cash_mode == "spy":
            prev, cur = spy_on_cal[day_idx - 1], spy_on_cal[day_idx]
            if prev > 0 and np.isfinite(prev) and np.isfinite(cur):
                return cur / prev
            return 1.0
        return 1.0

    cash = _INITIAL_CAP
    open_pos: dict[int, dict] = {}
    last_entry_cal_idx: dict = {}
    daily_values = np.zeros(len(master_cal))
    daily_cash = np.zeros(len(master_cal))
    pid_ctr = 0
    spy_sell_prices: list[tuple[pd.Timestamp, float]] = []  # (sell_day, spy_price)

    for day_idx, day in enumerate(master_cal):
        # 1. Accrue idle-cash return for the elapsed calendar gap.
        cash *= _cash_factor(day_idx)

        port_val = cash + _positions_val(day_idx, open_pos)

        # 2. Exit positions reaching day 252 -> proceeds back to cash.
        for pid in [k for k, v in list(open_pos.items()) if v["exit_idx"] == day_idx]:
            pos = open_pos.pop(pid)
            ep = _price(day_idx, pos["ticker"])
            if np.isnan(ep):
                ep = pos["entry_price"]
            cash += pos["shares"] * ep

        # 3. Process new signals (highest composite first).
        for ticker, comp, crossing_price, dd in sorted(
            events_by_date.get(day, []), key=lambda x: -x[1]
        ):
            if ticker in last_entry_cal_idx:
                if (day_idx - last_entry_cal_idx[ticker]) < _HOLD_DAYS:
                    continue
            if len(open_pos) >= _MAX_POS:
                continue
            alloc = min(port_val * _PCT, cash)
            if alloc < _MIN_POSITION:
                continue
            ep = _price(day_idx, ticker)
            if np.isnan(ep) or ep <= 0:
                ep = crossing_price
            if ep <= 0:
                continue

            shares = alloc / ep
            exit_idx = min(day_idx + _HOLD_DAYS, len(master_cal) - 1)
            pid_ctr += 1
            open_pos[pid_ctr] = {
                "ticker": ticker,
                "entry_idx": day_idx,
                "exit_idx": exit_idx,
                "entry_price": ep,
                "shares": shares,
            }
            last_entry_cal_idx[ticker] = day_idx
            cash -= alloc
            # In SPY mode, funding this position means selling SPY today.
            if cash_mode == "spy":
                spy_sell_prices.append((day, float(spy_on_cal[day_idx])))

        daily_values[day_idx] = cash + _positions_val(day_idx, open_pos)
        daily_cash[day_idx] = cash

    # Mark remaining open positions to market on the final day.
    last_idx = len(master_cal) - 1
    final_val = float(daily_values[last_idx])

    return {
        "daily_values": pd.Series(daily_values, index=master_cal),
        "daily_cash": pd.Series(daily_cash, index=master_cal),
        "final_value": final_val,
        "spy_sell_prices": spy_sell_prices,
    }


def variant_metrics(daily_values: pd.Series, years: float) -> dict:
    final_val = float(daily_values.iloc[-1])
    total_ret = final_val / _INITIAL_CAP - 1
    cagr = (final_val / _INITIAL_CAP) ** (1.0 / years) - 1
    rolling_max = daily_values.cummax()
    max_dd = float(((daily_values - rolling_max) / rolling_max).min())
    rets = daily_values.pct_change().dropna()
    std = float(rets.std())
    sharpe = float(rets.mean() * np.sqrt(252) / std) if std > 0 else 0.0
    cash_frac = None
    return {
        "final_value": final_val,
        "total_ret": total_ret,
        "cagr": cagr,
        "max_dd": max_dd,
        "sharpe": sharpe,
    }


def timing_cost(spy_sell_prices: list, spy_close: pd.Series) -> dict:
    """Average SPY price on sell days vs SPY price 30 calendar days later."""
    if not spy_sell_prices:
        return {"n": 0}
    sell_levels, later_levels, pct_changes = [], [], []
    for ts, sell_px in spy_sell_prices:
        later = spy_close.asof(ts + pd.Timedelta(days=_TIMING_WINDOW_DAYS))
        if pd.isna(later) or sell_px <= 0:
            continue
        sell_levels.append(sell_px)
        later_levels.append(float(later))
        pct_changes.append(float(later) / sell_px - 1)
    if not sell_levels:
        return {"n": 0}
    return {
        "n": len(sell_levels),
        "avg_sell": float(np.mean(sell_levels)),
        "avg_later": float(np.mean(later_levels)),
        "mean_pct_change": float(np.mean(pct_changes)),
        "median_pct_change": float(np.median(pct_changes)),
        "pct_higher_after": float(np.mean([1.0 if c > 0 else 0.0 for c in pct_changes])),
    }


def make_chart(series_by_variant: dict, spy_bh: pd.Series, out_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    fig, ax = plt.subplots(figsize=(12, 7))
    styles = {
        "ZERO (V1 baseline, 0%)": dict(color="#888888", lw=1.8, ls="--"),
        "MONEY_MARKET (4.5%)": dict(color="#2ca02c", lw=2.0),
        "SPY (idle cash in SPY)": dict(color="#1f77b4", lw=2.0),
        "SPY_NEUTRAL (smooth SPY CAGR)": dict(color="#9467bd", lw=2.0, ls="-."),
    }
    for label, s in series_by_variant.items():
        st = styles.get(label, {})
        ax.plot(s.index, s.values, label=label, **st)
    ax.plot(spy_bh.index, spy_bh.values, label="SPY buy & hold (reference)",
            color="#d62728", lw=1.3, ls=":")

    ax.set_title("Uninvested-cash variants — V1 signal portfolio (10%/max10, 252d hold)\n"
                 "$100k start, 2018-01-02 – 2024-12-31", fontsize=12)
    ax.set_ylabel("Portfolio value")
    ax.set_xlabel("Date")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v/1000:,.0f}k"))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def main() -> None:
    print("Loading price data and computing signals (cached)...")
    prices_obj = PriceData()
    fund = EdgarFundamentals(fallback=PointInTimeFundamentals())

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        crossings_by_ticker, prices_wide, spy_close = load_all_data(prices_obj, fund)

    spy_sim = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)]
    master_cal = spy_sim.index
    years = _years_between(master_cal)

    # SPY aligned to the trading calendar (for the SPY idle-cash path).
    spy_on_cal = spy_close.reindex(master_cal, method="ffill").values.astype(float)

    # Smooth SPY CAGR over the window (for the neutralised variant).
    spy_cagr = (spy_on_cal[-1] / spy_on_cal[0]) ** (1.0 / years) - 1
    spy_neutral_annual_rate = spy_cagr
    mm_annual_rate = _MM_RATE

    print(f"  Calendar: {master_cal[0].date()} – {master_cal[-1].date()} "
          f"({len(master_cal)} days, {years:.2f}y)")
    print(f"  SPY window CAGR (used for SPY_NEUTRAL): {spy_cagr:+.2%}")
    print()

    variants = [
        ("ZERO (V1 baseline, 0%)", "zero"),
        ("MONEY_MARKET (4.5%)", "money_market"),
        ("SPY (idle cash in SPY)", "spy"),
        ("SPY_NEUTRAL (smooth SPY CAGR)", "spy_neutral"),
    ]

    results, metrics, series = {}, {}, {}
    spy_timing = None
    for label, mode in variants:
        res = simulate_cash_variant(
            crossings_by_ticker, prices_wide, master_cal, spy_on_cal,
            mode, mm_annual_rate, spy_neutral_annual_rate,
        )
        results[label] = res
        metrics[label] = variant_metrics(res["daily_values"], years)
        series[label] = res["daily_values"]
        if mode == "spy":
            spy_timing = timing_cost(res["spy_sell_prices"], spy_close)
        m = metrics[label]
        print(f"  {label:<32}  final ${m['final_value']:>11,.0f}  CAGR {m['cagr']:+.2%}")

    # SPY buy & hold reference (full $100k in SPY).
    spy_bh = pd.Series(spy_on_cal / spy_on_cal[0] * _INITIAL_CAP, index=master_cal)
    spy_bh_final = float(spy_bh.iloc[-1])
    spy_bh_cagr = (spy_bh_final / _INITIAL_CAP) ** (1.0 / years) - 1

    # ── Report ────────────────────────────────────────────────────────────────
    div = "=" * 92
    print()
    print(div)
    print("UNINVESTED-CASH VARIANTS — V1 SIGNAL PORTFOLIO (10%/max10, 252d hold)")
    print("$100,000 start · 2018-01-02 – 2024-12-31 · only idle-cash treatment differs")
    print(div)
    print()

    hdr = (f"  {'Variant':<32}  {'Final $':>12}  {'TotRet':>8}  {'CAGR':>7}  "
           f"{'MaxDD':>7}  {'Sharpe':>7}")
    print(hdr)
    print("  " + "-" * 88)
    for label, _ in variants:
        m = metrics[label]
        print(f"  {label:<32}  ${m['final_value']:>11,.0f}  {m['total_ret']:>+7.1%}  "
              f"{m['cagr']:>+6.1%}  {m['max_dd']:>6.1%}  {m['sharpe']:>7.2f}")
    print(f"  {'SPY buy & hold (reference)':<32}  ${spy_bh_final:>11,.0f}  "
          f"{spy_bh_final/_INITIAL_CAP-1:>+7.1%}  {spy_bh_cagr:>+6.1%}  {'—':>6}  {'—':>7}")
    print()

    # ── SPY-variant timing column ──────────────────────────────────────────────
    print("  TIMING COST — SPY variant only")
    print("  " + "-" * 88)
    if spy_timing and spy_timing["n"] > 0:
        t = spy_timing
        print(f"    Sell events (cash pulled from SPY to fund a position): {t['n']}")
        print(f"    Avg SPY level on sell day:                 {t['avg_sell']:>8.2f}")
        print(f"    Avg SPY level {_TIMING_WINDOW_DAYS} calendar days later:    {t['avg_later']:>8.2f}")
        print(f"    Avg per-event SPY move over next {_TIMING_WINDOW_DAYS}d:     {t['mean_pct_change']:>+7.2%}")
        print(f"    Median per-event move:                     {t['median_pct_change']:>+7.2%}")
        print(f"    Share of sells followed by a higher SPY:   {t['pct_higher_after']:>7.0%}")
        print()
        print(f"    Interpretation: a positive average move means the strategy sold SPY *below*")
        print(f"    where it traded a month later — i.e. it liquidated the SPY sleeve into market")
        print(f"    weakness (the dislocation that triggered the signal) and missed the bounce.")
    else:
        print("    No SPY sell events recorded.")
    print()

    # Cost of SPY's path vs neutralised SPY return.
    spy_final = metrics["SPY (idle cash in SPY)"]["final_value"]
    neutral_final = metrics["SPY_NEUTRAL (smooth SPY CAGR)"]["final_value"]
    mm_final = metrics["MONEY_MARKET (4.5%)"]["final_value"]
    zero_final = metrics["ZERO (V1 baseline, 0%)"]["final_value"]
    path_cost = neutral_final - spy_final

    print("  COST OF SPY'S PATH (SPY_NEUTRAL − SPY)")
    print("  " + "-" * 88)
    print(f"    SPY_NEUTRAL final:  ${neutral_final:>11,.0f}")
    print(f"    SPY final:          ${spy_final:>11,.0f}")
    print(f"    Difference:         ${path_cost:>+11,.0f}  "
          f"({'neutralising the timing helps' if path_cost > 0 else 'real SPY path helped'})")
    print()

    # ── Note: does SPY beat money market, and is it justified? ──────────────────
    print(div)
    print("NOTE — Does the SPY variant beat money market, and is the gap justified?")
    print(div)
    spy_beats_mm = spy_final > mm_final
    gap = spy_final - mm_final
    print(f"  ZERO (baseline):  ${zero_final:>11,.0f}")
    print(f"  MONEY_MARKET:     ${mm_final:>11,.0f}   (+${mm_final-zero_final:,.0f} vs baseline)")
    print(f"  SPY:              ${spy_final:>11,.0f}   "
          f"({'+' if gap>=0 else ''}{gap:,.0f} vs money market)")
    print(f"  SPY_NEUTRAL:      ${neutral_final:>11,.0f}")
    print()
    if spy_beats_mm:
        print(f"  YES — the SPY variant beats money market by ${gap:,.0f} "
              f"({gap/mm_final:+.1%}).")
        print(f"  But the source of that edge matters. The SPY sleeve earns equity beta on idle")
        print(f"  cash, yet the strategy is structurally forced to SELL that beta at signal time —")
        print(f"  i.e. into the very dislocations (Mar 2020, 2022) that fire the recovery signal.")
        if spy_timing and spy_timing["n"] > 0 and spy_timing["mean_pct_change"] > 0:
            print(f"  On average SPY was {spy_timing['mean_pct_change']:+.1%} higher "
                  f"{_TIMING_WINDOW_DAYS} days after each sell, confirming the sleeve was")
            print(f"  liquidated near local bottoms. The SPY_NEUTRAL control — same average SPY")
            print(f"  return without that timing — ends at ${neutral_final:,.0f}, "
                  f"${path_cost:+,.0f} vs realised SPY.")
        verdict = ("The gap over money market is driven by equity risk premium, not skill, and it")
        print(f"  {verdict}")
        print(f"  is only partially 'justified': you are paid for bearing equity risk, but you")
        print(f"  systematically realise that risk at the worst moments (selling at the bottom),")
        print(f"  which is exactly the drawdown a 4.5% money-market sleeve avoids. Compare the")
        print(f"  max drawdowns above — the SPY sleeve adds equity drawdown to the portfolio that")
        print(f"  the money-market sleeve does not.")
    else:
        print(f"  NO — the SPY variant does NOT beat money market "
              f"(${gap:,.0f}). Forced selling of the SPY sleeve into signal-time")
        print(f"  dislocations erodes the equity-beta advantage; the steady 4.5% accrual wins.")
    print()
    print(div)

    out_path = Path(__file__).parent.parent / "results" / "uninvested_cash_variants.png"
    make_chart(series, spy_bh, out_path)
    print(f"Chart written: {out_path}")


if __name__ == "__main__":
    main()
