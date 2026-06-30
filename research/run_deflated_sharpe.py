#!/usr/bin/env python3
"""Deflated Sharpe Ratio (Bailey & López de Prado, 2014) for the best variant.

Analysis only — no product/pipeline changes. Implements:

  * PSR  (Probabilistic Sharpe Ratio), Bailey & López de Prado (2012), reused as
    eq. (2)-(3) in the 2014 DSR paper:
        PSR(SR0) = Z[ (SRh - SR0)·sqrt(n-1) / sqrt(1 - g3·SRh + (g4-1)/4·SRh^2) ]
    with SRh, SR0 in per-observation (here daily) units; g3 = skewness,
    g4 = kurtosis (non-excess, normal=3); Z = standard-normal CDF.

  * Deflated benchmark SR0 = E[max Sharpe] under the null of no skill across N
    trials, Bailey & López de Prado (2014), eq. (5):
        SR0 = sqrt(V)·[ (1-γ)·Z^{-1}(1 - 1/N) + γ·Z^{-1}(1 - 1/(N·e)) ]
    where V = cross-trial variance of the (per-observation) Sharpe estimates,
    γ = Euler-Mascheroni ≈ 0.5772, e = Euler's number, N = number of trials.

  * DSR = PSR(SR0).

Best variant = Top-100 point-in-time universe + threshold 0.60 + money market
(Fed funds), flat 10%, 2018-2024 (the documented combined-clean "B" run, Sharpe
~0.82). Its daily-return series is regenerated here for the exact g3/g4.
"""
from __future__ import annotations

import math
import sys
import warnings
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.tickers import VALIDATION_UNIVERSE
from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from data.sp500_universe import get_universe, get_universe_top_n, prefetch_pit_market_caps
from research.run_combined_clean_universe import _TOP_N, simulate as sim_cash
from scripts.run_combined_validation import load_fedfunds
from scripts.run_portfolio_sim import compute_metrics, load_all_data
from scripts.run_portfolio_sim import simulate as sim_plain

_SIM_START = pd.Timestamp("2018-01-01")
_SIM_END = pd.Timestamp("2024-12-31")
_GAMMA_EM = 0.5772156649015329
_Z = NormalDist()  # standard normal


def daily_sharpe(dv: pd.Series) -> float:
    r = dv.pct_change().dropna().values
    return float(np.mean(r) / np.std(r, ddof=1))


def moments(dv: pd.Series):
    r = dv.pct_change().dropna().values
    n = len(r)
    mu, sd0 = float(np.mean(r)), float(np.std(r, ddof=0))
    m2 = np.mean((r - mu) ** 2)
    g3 = float(np.mean((r - mu) ** 3) / m2 ** 1.5)            # skewness
    g4 = float(np.mean((r - mu) ** 4) / m2 ** 2)              # kurtosis (normal=3)
    sr_daily = mu / sd0                                        # per-obs Sharpe (ddof=0)
    return n, sr_daily, g3, g4


def psr(sr_hat, sr0, n, g3, g4):
    """Probabilistic Sharpe Ratio — all SR in per-observation units."""
    denom = math.sqrt(1.0 - g3 * sr_hat + (g4 - 1.0) / 4.0 * sr_hat ** 2)
    return _Z.cdf((sr_hat - sr0) * math.sqrt(n - 1) / denom)


def expected_max_sr(V_daily, N):
    """E[max Sharpe] under the null across N trials (BLdP 2014 eq. 5), per-obs."""
    a = _Z.inv_cdf(1.0 - 1.0 / N)
    b = _Z.inv_cdf(1.0 - 1.0 / (N * math.e))
    return math.sqrt(V_daily) * ((1.0 - _GAMMA_EM) * a + _GAMMA_EM * b)


def main():
    prices_obj = PriceData()
    fund = EdgarFundamentals(fallback=PointInTimeFundamentals())
    spy_close = prices_obj.get_prices("SPY", "2016-01-01", "2024-12-31")["Close"]
    master_cal = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)].index
    ann = math.sqrt(252.0)

    # ── Regenerate the two missing trial Sharpes: 50-survivor V2 (20%/5), V3 (5%/20)
    print("Regenerating 50-survivor V2/V3 Sharpes...")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cr50, pw50, _ = load_all_data(prices_obj, fund, list(VALIDATION_UNIVERSE))
    mm50 = {(d.year, d.month): set(VALIDATION_UNIVERSE) for d in master_cal}
    v2 = compute_metrics(sim_plain(cr50, pw50, master_cal, 0.20, 5, month_members=mm50)["daily_values"], 100_000)["sharpe"]
    v3 = compute_metrics(sim_plain(cr50, pw50, master_cal, 0.05, 20, month_members=mm50)["daily_values"], 100_000)["sharpe"]
    print(f"  V2 (20%/5) Sharpe={v2:.2f}   V3 (5%/20) Sharpe={v3:.2f}")

    # ── Regenerate the BEST variant's daily series for exact skew/kurtosis
    print("Regenerating best variant (Top-100 PIT + thr0.60 + Fed funds)...")
    fmonths = {}
    for ts in master_cal:
        fmonths.setdefault((ts.year, ts.month), ts)
    union_full = sorted({t for ts in fmonths.values() for t in get_universe(ts.date().isoformat())})
    prefetch_pit_market_caps(union_full, [ts.date().isoformat() for ts in fmonths.values()])
    month_members = {k: set(get_universe_top_n(ts.date().isoformat(), _TOP_N)) for k, ts in fmonths.items()}
    union = sorted(set().union(*month_members.values()))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cr, pw, _ = load_all_data(prices_obj, fund, union)
    rate = load_fedfunds().reindex(master_cal, method="ffill").values.astype(float)
    best = sim_cash(cr, pw, master_cal, "flat", "fed_funds", rate, month_members, entry_threshold=0.60)
    n, sr_daily, g3, g4 = moments(best["daily_values"])
    sr_ann = sr_daily * ann
    print(f"  best: n={n}  daily SR={sr_daily:.4f}  annual SR={sr_ann:.3f}  skew={g3:.3f}  kurt={g4:.3f}")

    # ── Trial set (distinct configs run on 2018-2024), annualized Sharpes ───────
    trials = [
        ("50surv flat10 cash0 (V1 base)",            0.86),
        ("50surv flat10 money-mkt 4.5%",             0.98),
        ("50surv flat10 SPY-sleeve",                 0.80),
        ("50surv flat10 SPY-neutralized (control)",  1.20),
        ("50surv 20%/5 cash0 (V2)",                  round(v2, 2)),
        ("50surv 5%/20 cash0 (V3)",                  round(v3, 2)),
        ("50surv Score-Plus cash0",                  0.84),
        ("50surv Score-Plus money-mkt",              0.95),
        ("full-PIT flat10 cash0",                    0.09),
        ("Top-100 flat10 cash0",                     0.78),
        ("Top-150 flat10 cash0",                     0.65),
        ("Top-200 flat10 cash0",                     0.63),
        ("Top-100 flat10 Fed-funds (BEST)",          round(sr_ann, 2)),
        ("Top-100 Score-Plus cash0",                 0.76),
        ("Top-100 Score-Plus Fed-funds",             0.79),
        ("Top-100 flat10 Fed-funds thr0.65",         0.88),
        ("Top-100 flat10 Fed-funds thr0.70",         0.85),
        ("Top-100 flat10 Fed-funds thr0.75",         1.28),
        ("Top-100 flat10 Fed-funds regime-ON",       0.62),
        ("50surv flat10 cash0 hold-378",             0.70),
        ("50surv flat10 cash0 hold-504",             0.92),
    ]
    sr_ann_list = np.array([s for _, s in trials], dtype=float)
    N = len(trials)
    V_ann = float(np.var(sr_ann_list, ddof=1))     # cross-trial variance (annualized)
    V_daily = V_ann / 252.0
    sr_best_daily = sr_ann / ann                    # consistent per-obs SR for PSR

    sr0_daily = expected_max_sr(V_daily, N)
    sr0_ann = sr0_daily * ann
    dsr = psr(sr_best_daily, sr0_daily, n, g3, g4)
    psr_vs_spy = psr(sr_best_daily, 0.76 / ann, n, g3, g4)
    psr_vs_zero = psr(sr_best_daily, 0.0, n, g3, g4)

    div = "=" * 84
    print("\n" + div)
    print("DEFLATED SHARPE RATIO — best variant (Top-100 PIT + thr0.60 + Fed funds), 2018-2024")
    print(div)
    print(f"\n  N trials enumerated: {N}")
    for name, s in trials:
        print(f"    {s:>5.2f}   {name}")
    print(f"\n  Cross-trial Sharpe stats: mean {sr_ann_list.mean():.3f}  "
          f"std {sr_ann_list.std(ddof=1):.3f}  var(V, annualized) {V_ann:.4f}")
    print(f"  Observed best Sharpe: {sr_ann:.3f} annual  ({sr_best_daily:.4f} daily), n={n} days")
    print(f"  Return non-normality: skew g3={g3:.3f}  kurt g4={g4:.3f} (excess {g4-3:.2f})")
    print()
    print(f"  SR0 deflated benchmark (E[max] of {N} trials under null):  "
          f"{sr0_ann:.3f} annual  ({sr0_daily:.4f} daily)")
    print(f"  DSR = PSR(SR0)                     = {dsr:.4f}   ({dsr*100:.1f}%)")
    print(f"  PSR vs SPY (SR0 = 0.76 annual)     = {psr_vs_spy:.4f}   ({psr_vs_spy*100:.1f}%)")
    print(f"  PSR vs zero (SR0 = 0)              = {psr_vs_zero:.4f}   ({psr_vs_zero*100:.1f}%)")
    print()
    print(div)
    print("VERDICT")
    print(div)
    sig = dsr >= 0.95
    print(f"  DSR = {dsr*100:.1f}%  →  {'SIGNIFICANT' if sig else 'NOT significant'} at the 95% level.")
    print(f"  Deflated hurdle SR0={sr0_ann:.2f} sits {'below' if sr0_ann < 0.76 else 'above'} SPY (0.76);")
    print(f"  the more demanding bar is SPY itself: PSR vs SPY = {psr_vs_spy*100:.1f}% "
          f"({'clears' if psr_vs_spy>=0.95 else 'does NOT clear'} 95%).")
    print()


if __name__ == "__main__":
    main()
