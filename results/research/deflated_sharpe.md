# Deflated Sharpe Ratio — does the edge survive selection correction?

Tests whether the best variant's Sharpe edge survives correction for (a) the number
of configurations tried, (b) the finite sample, and (c) non-normal returns —
following **Bailey & López de Prado (2014), "The Deflated Sharpe Ratio: Correcting
for Selection Bias, Backtest Overfitting and Non-Normality," *Journal of Portfolio
Management* 40(5)**. Analysis only; no product/pipeline code changed.
Reproduce: `python research/run_deflated_sharpe.py`.

## Formulas implemented (and where they come from)

**Probabilistic Sharpe Ratio** — Bailey & López de Prado (2012, *J. of Risk*),
restated as eqs. (2)–(3) of the 2014 DSR paper:

> PSR(SR₀) = Z[ (SR̂ − SR₀)·√(n−1) ⁄ √(1 − γ₃·SR̂ + ((γ₄−1)/4)·SR̂²) ]

with SR̂, SR₀ in **per-observation (daily)** units, γ₃ = skewness, γ₄ = kurtosis
(non-excess, normal = 3), Z = standard-normal CDF, n = number of returns.

**Deflated benchmark SR₀** = expected maximum Sharpe under the null of *no skill*
across N trials — Bailey & López de Prado (2014), the "False Strategy" / expected-
maximum result (eq. 5):

> SR₀ = √V · [ (1−γ)·Z⁻¹(1 − 1/N) + γ·Z⁻¹(1 − 1/(N·e)) ]

V = cross-trial variance of the (per-observation) Sharpe estimates, γ = Euler–
Mascheroni ≈ 0.5772, e = Euler's number. **DSR = PSR(SR₀).**

## 1. N = 21 distinct configurations (the trial count)

All on 2018-2024 (the window the selection was done on). Annualized Sharpe each:

| SR | Configuration | SR | Configuration |
|--:|---|--:|---|
| 0.86 | 50surv flat10 cash0 (V1) | 0.82 | **Top-100 flat10 Fed-funds (BEST)** |
| 0.98 | 50surv flat10 money-mkt 4.5% | 0.76 | Top-100 Score-Plus cash0 |
| 0.80 | 50surv flat10 SPY-sleeve | 0.79 | Top-100 Score-Plus Fed-funds |
| 1.20 | 50surv SPY-neutralized (control) | 0.88 | Top-100 Fed-funds thr0.65 |
| 0.85 | 50surv 20%/5 (V2) | 0.85 | Top-100 Fed-funds thr0.70 |
| 0.80 | 50surv 5%/20 (V3) | 1.28 | Top-100 Fed-funds thr0.75 |
| 0.84 | 50surv Score-Plus cash0 | 0.62 | Top-100 Fed-funds regime-ON |
| 0.95 | 50surv Score-Plus money-mkt | 0.70 | 50surv hold-378 |
| 0.09 | full-PIT flat10 cash0 | 0.92 | 50surv hold-504 |
| 0.78 | Top-100 flat10 cash0 | | |
| 0.65 | Top-150 flat10 cash0 | | |
| 0.63 | Top-200 flat10 cash0 | | |

**Inputs exact vs approximated:** 19 of the 21 Sharpes are exact (from this repo's
results/ reports); **V2 (0.85) and V3 (0.80) were regenerated here**; hold-378/504
come from PR #10's report (a slightly different "completion rule"). SPY-neutralized
is a non-tradeable control, included because it was a reported run. Distinct
configs only — re-runs of the identical config across studies are de-duplicated.
Different *windows* (2010-2024, the 2010-2017 OOS) are excluded from N because
selection bias is per-dataset.

## 2–4. Computed inputs (exact, regenerated)

| Quantity | Value |
|---|--:|
| **N** (trials) | 21 |
| Cross-trial Sharpe variance **V** (annualized) | 0.0537 (std 0.232, mean 0.812) |
| Best-variant observed Sharpe | **0.816 annual** (0.0514 daily), n = 1,759 days |
| Skewness γ₃ | **+0.905** |
| Kurtosis γ₄ | **16.03** (excess +13.0 — very fat-tailed) |

## 5. Deflated Sharpe Ratio

| Benchmark | Value |
|---|--:|
| **SR₀ deflated hurdle** (E[max] of 21 null trials) | **0.445 annual** (0.0281 daily) |
| **DSR = PSR(SR₀)** | **84.1%** |
| PSR vs SPY (SR₀ = 0.76) | **56.0%** |
| PSR vs zero (SR₀ = 0) | 98.6% |

## Verdict — the edge does NOT survive

**No. The Sharpe edge over SPY is not statistically distinguishable from luck once
you account for the 21 configurations tried and the fat-tailed returns.**

- **DSR = 84.1%** — against the selection-deflated no-skill hurdle (SR₀ = 0.445),
  the best variant clears it with only 84% confidence, **below the 95% bar.** So
  even versus a *zero-skill* benchmark inflated for 21 trials, it is not significant.
- **The binding test is SPY, and it fails badly.** The deflated hurdle (0.445)
  actually sits *below* SPY's 0.76, so beating it isn't even "beating SPY." Tested
  directly against SPY, **PSR = 56.0%** — a coin flip. The 0.82-vs-0.76 gap is noise.
- The strategy *does* have a reliably positive Sharpe versus literally zero
  (98.6%), but in a 2018-2024 bull market that is a trivial bar.

**Bottom line:** the ~0.82 Sharpe is real in-sample, but the *edge over SPY* falls
well within the range explainable by the number of configurations we tried plus
non-normality. Treat "beats SPY on Sharpe" as **unproven**, consistent with the OOS
threshold result (0.65 didn't replicate) — both point to overfitting risk.

### Robustness notes
- **The PSR-vs-SPY result (56%) does not depend on N or V at all** — only on the
  observed Sharpe, n, skew, kurtosis. It is the cleanest, most robust finding here:
  ~7 years is simply too short to distinguish a 0.82 Sharpe from 0.76.
- The 21 trials are **not independent** (overlapping data, nested configs), so the
  *effective* number of trials is < 21. That makes SR₀ slightly overstated and the
  DSR slightly understated (conservative) — correcting it would only nudge DSR up
  toward ~0.85, still short of 0.95, and would not touch the vs-SPY conclusion.
- Adding the superseded current-cap universe runs or the other-window studies would
  *raise* N and SR₀, lowering the DSR further. The "not significant" conclusion is
  robust in both directions.
