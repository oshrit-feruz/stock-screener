# Composite-score entry-threshold sweep — clean Top-100 PIT

Does raising the entry bar above 0.60 improve signal quality? Four variants, all
Top-100 PIT, flat 10% + Fed-funds cash, 2018–2024, 252d exit, no SL, $100k.

| Variant | Final $ | CAGR | Sharpe | Max DD | Trades | Win % | Sig/yr | Avg ret/trade |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| A · thr 0.60 (base) | $296,397 | +16.8% | 0.82 | −31.4% | 41 | 66% | 5.9 | +34.0% |
| **B · thr 0.65** | **$310,842** | **+17.6%** | **0.88** | −27.4% | 35 | 69% | 5.0 | +39.4% |
| C · thr 0.70 | $278,611 | +15.8% | 0.85 | −23.0% | 33 | 61% | 4.7 | +38.4% |
| D · thr 0.75 | $159,029 | +6.9% | 1.28 | −5.5% | 7 | 86% | **1.0** | +49.9% |
| SPY buy & hold | $245,098 | +13.7% | 0.76 | −33.7% | — | — | — | — |

## Score distribution — raw in-universe signal pool (252d forward return)

Every in-universe BUY crossing (no suppression/capacity), by score bucket — shows
both signal quality and how the pool thins:

| Bucket | Signals | per yr | Avg 252d ret | Win % |
|---|--:|--:|--:|--:|
| 0.60–0.64 | 252 | 36.0 | +34.0% | 72% |
| 0.65–0.69 | 108 | 15.4 | +39.5% | 77% |
| 0.70–0.74 | 103 | 14.7 | +44.3% | 75% |
| 0.75+ | 10 | 1.4 | +42.9% | 80% |
| **Total** | 473 | 67.6 | — | — |

Signal quality rises with score (avg 252d return +34% → +39% → +44%, win 72% →
77% → 75% → 80%), confirming the hypothesis. But the ≥0.75 pool is tiny — only
~1.4 raw crossings/yr, and just 7 *entered* trades over 7 years after
one-position-per-ticker suppression.

## Year-by-year return

| Year | thr 0.60 | thr 0.65 | thr 0.70 | thr 0.75 | SPY |
|---|--:|--:|--:|--:|--:|
| 2018 | −1.3% | +0.5% | +2.0% | +1.8% | −5.2% |
| 2019 | +33.0% | +29.7% | +15.6% | +6.7% | +31.2% |
| 2020 | +70.4% | +54.1% | +57.1% | +14.9% | +18.3% |
| 2021 | +12.7% | +14.8% | +13.6% | +7.8% | +28.7% |
| 2022 | −28.2% | −23.1% | −15.0% | −2.8% | −18.2% |
| 2023 | +40.6% | +49.6% | +41.5% | +15.6% | +26.2% |
| 2024 | +16.5% | +17.2% | +10.2% | +5.2% | +25.3% |

Higher thresholds steadily soften the 2022 drawdown (−28% → −23% → −15% → −3%)
but also clip the big 2019/2020 recovery years — the classic frequency/quality
trade-off.

## Key question — a threshold > 0.60 that lifts BOTH Sharpe and CAGR with ≥3 signals/yr?

**Yes — 0.65.** It is the sweet spot: CAGR +17.6% (> 16.8% baseline) **and**
Sharpe 0.88 (> 0.82), with a shallower drawdown (−27.4%), higher win rate (69%),
higher per-trade return (+39.4%), and **5.0 signals/yr** — comfortably above the
3/yr reliability floor. It also beats SPY on both metrics.

### The other levels
- **0.70** improves *Sharpe* (0.85) but not *CAGR* (15.8% < baseline): it trades
  return for an even calmer ride (−23% max DD). Defensible if the product
  prioritises drawdown, but it fails the "both" test.
- **0.75 is a statistical mirage.** Headline Sharpe 1.28 and a −5.5% max drawdown
  look spectacular, but on **1.0 trade/yr (7 total)** the numbers are noise, and
  the strategy sits in cash so much that CAGR collapses to +6.9% — *below SPY*.
  Below the 3/yr floor; do not use.

**Recommendation:** move the entry threshold from 0.60 to **0.65**. It is the only
level that improves both return and risk-adjusted return while staying
statistically reliable.

Reproduce: `python scripts/run_threshold_sweep.py`
