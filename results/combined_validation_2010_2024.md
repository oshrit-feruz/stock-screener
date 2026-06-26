# Combined validation 2010–2024 — Score-Plus × real Fed funds cash sleeve

Two changes vs the earlier 2018–2024 study:
1. **Real historical interest.** The fixed 4.5% sleeve is replaced by the actual
   effective **federal funds rate** (FRED series `FEDFUNDS`) in force on each date,
   accrued daily pro-rata. The rate ranges from ~0.05% (ZIRP, 2010–2015 & 2020–21)
   to 5.33% (2023–24), mean **1.23%** over the window.
2. **Longer window.** Backtest extended to **2010–2024** (15y) to include the
   2008–09 recovery aftermath — the kind of dislocation the signal targets.

All four variants run on identical conditions: 50 tickers, threshold 0.60, exit at
day 252, no stop-loss, $100,000 start, max 10 concurrent positions.

| Variant | Sizing | Cash | Final $ | Total ret | CAGR | Sharpe | Max DD |
|---|---|---|--:|--:|--:|--:|--:|
| **A** · V1 baseline | flat | 0% | $550,480 | +450.5% | +12.1% | 0.82 | −32.2% |
| **B** · Score-Plus only | score_plus | 0% | $573,509 | +473.5% | +12.4% | 0.80 | −33.6% |
| **C** · Fed funds only | flat | hist. | $606,355 | +506.4% | +12.8% | 0.87 | −32.0% |
| **D** · Full combo (main) | score_plus | hist. | **$630,460** | **+530.5%** | **+13.1%** | 0.84 | −33.5% |
| *SPY buy & hold (ref)* | — | — | *$681,279* | *+581.3%* | *+13.7%* | *0.84* | *−33.7%* |

### Average actual Fed funds rate by year (validation — rate applied to idle cash)

| 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 |
|--:|--:|--:|--:|--:|--:|--:|--:|
| 0.18% | 0.10% | 0.14% | 0.11% | 0.09% | 0.13% | 0.40% | 1.00% |

| 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 |
|--:|--:|--:|--:|--:|--:|--:|
| 1.83% | 2.16% | 0.37% | 0.08% | 1.70% | 5.03% | 5.14% |

## Linear-expectation check — does the stack add up?

| | Final $ | Total ret |
|---|--:|--:|
| Baseline A | $550,480 | +450.5% |
| + Score-Plus improvement (B − A) | +$23,030 | +23.0% |
| + Fed-funds improvement (C − A) | +$55,876 | +55.9% |
| **= Expected D (linear sum)** | **$629,385** | **+529.4%** |
| Actual D (measured) | $630,460 | +530.5% |
| **Interaction (actual − expected)** | **+$1,074** | **+1.1%** |

**Essentially additive** (interaction ~1% of the combined lift). As in the
2018–2024 run, the two levers nearly cancel: Score-Plus deploys more idle cash
(less for the sleeve to earn on — a drag), while each lever enlarges the base the
other compounds on (a boost). The Fed funds sleeve does the heavy lifting
(+$55.9k); Score-Plus adds a smaller, slightly higher-risk increment (+$23.0k,
with drawdown rising to −33.6% and Sharpe dipping to 0.80).

## The headline finding over the full window

**Over 2010–2024 every variant trails SPY buy & hold ($681k, +581%).** The
strategy sits in cash between dislocations, and a 15-year bull market punishes that
idleness — the Fed funds sleeve narrows the gap (D reaches $630k, 92% of SPY's
terminal value) but does not close it. The cash sleeve matters *most* exactly when
the strategy is *least* deployed: note the long flat stretches in 2013–2016 on the
chart, where the portfolio is mostly idle cash earning near-zero ZIRP rates, so the
sleeve barely helps there; almost all of its contribution comes from the 2023–24
period when rates hit ~5%.

## Caveats — read before trusting these numbers

1. **Survivorship bias (worst pre-2015).** The 50 tickers are firms that *survived*
   to 2024 (AAPL, NVDA, JPM, …). Names that blew up, were delisted, or were acquired
   after a crash are absent, so the recovery signal only ever sees dips that
   eventually recovered — it never bets on one that went to zero. This inflates
   returns, and the distortion is worst in the early years: **pre-2015 results are
   optimistic upper bounds, not achievable performance.** (Several universe members
   — META, TSLA, ABBV, AVGO — did not even trade for part of 2010–2012, so the early
   sample is also thin: only 27 of 50 tickers ever fire a BUY crossing.)
2. **EDGAR fundamentals thin out before ~2015.** Where a fundamental needed by the
   quality gate is missing, the **existing fail-closed logic is unchanged**: gate
   `None` → `False` → signal rejected. Some otherwise-valid early signals are simply
   dropped — conservative for returns, but it further thins the early sample.

Reproduce: `python scripts/run_combined_validation.py`
(price cache built via `python scripts/_fetch_price_cache.py 2008-01-01 2024-12-31`;
Fed funds pulled from FRED and cached under `data/cache/fred/`).
