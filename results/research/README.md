# Research reports — point-in-time S&P 500 universe

One-off validation studies behind the point-in-time universe work. **These are
documentation only — none of the experimental knobs below are wired into the
product pipeline** (`scripts/run_portfolio_sim.py`). The scripts that generate
them live in `research/`.

| Report | Script | One-line verdict |
|---|---|---|
| `sp500_pit_universe.md` | `run_portfolio_sim.py` (wired) | Point-in-time S&P 500 membership replaces the 50 survivors → V1 falls from $293k (+16.6% CAGR) to $88,830 (−1.7%); most of the old edge was **survivorship bias**. |
| `universe_size_sweep.md` (+ `_SUMMARY`) | `run_universe_size_sweep.py` | Restricting to **Top-100 by point-in-time market cap** restores the edge (Sharpe 0.78 vs full-universe 0.09); Sharpe falls monotonically as the universe grows. |
| `combined_clean_universe.md` | `run_combined_clean_universe.py` | On the clean Top-100, **Top-100 + money market (Fed funds)** beats SPY on both CAGR (+16.9%) and Sharpe (0.79); Score-Plus adds little and slightly lowers Sharpe. **Money market is the main winner.** |
| `regime_filter.md` | `run_regime_filter.py` | SPY-200d-MA entry filter → **DO NOT USE.** It blocks the signal's best counter-trend entries (missed the Mar-2020 recovery); CAGR 16.8%→8.5%, Sharpe 0.82→0.62. |
| `threshold_sweep.md` | `run_threshold_sweep.py` | On 2018-2024, threshold **0.65** looked best (CAGR +17.6%, Sharpe 0.88). |
| `oos_2010_2017_threshold.md` | `run_oos_2010_2017.py` | Out-of-sample, **0.65 does NOT replicate** — gap is noise, < 3 signals/yr, score→return premise inverts. **Keep threshold 0.60** (treat 0.65 as overfitting). |
| `deflated_sharpe.md` | `run_deflated_sharpe.py` | Deflated Sharpe Ratio (Bailey & López de Prado 2014) over N=21 trials: DSR=84% (< 95%), PSR vs SPY=56%. **The Sharpe edge over SPY is NOT statistically distinguishable from luck** after selection correction. |

## Net product conclusions
- **Adopt:** point-in-time universe (membership) + Top-100 PIT size filter + money-market cash sleeve.
- **Reject:** regime filter (hurts), threshold 0.65 (overfit — stay at 0.60), Score-Plus (marginal, slightly worse Sharpe).

## Status vs the pipeline
As of this branch, only the **point-in-time universe membership** is wired into
`run_portfolio_sim.py`. Top-100 + money market are validated here but **not yet
wired** — that is a separate, not-yet-started follow-up.
