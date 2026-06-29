# Out-of-sample test: threshold 0.65 vs 0.60 on 2010–2017

The 0.65 > 0.60 result came from 2018–2024. This re-tests it on a window we never
optimised on. Clean Top-100 PIT universe, flat 10% + Fed funds, 252d exit, no SL,
$100k, 2008 warmup. (Survivorship caveat: 178 of 669 union tickers have no usable
price history and are skipped, so early-year numbers are slightly optimistic.)

| Variant | Final $ | CAGR | Sharpe | Max DD | Trades | Win % | Sig/yr | Avg ret/trade |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| A · thr 0.60 (orig) | $134,017 | +3.7% | 0.35 | −23.7% | 26 | 62% | 3.2 | +12.6% |
| B · thr 0.65 (cand) | $135,021 | +3.8% | 0.40 | −24.9% | 21 | 57% | **2.6** | +14.6% |
| SPY buy & hold | $276,994 | +13.6% | 0.95 | −18.6% | — | — | — | — |

## Year-by-year return

| Year | thr 0.60 | thr 0.65 | B − A | SPY |
|---|--:|--:|--:|--:|
| 2010 | +0.2% | +0.0% | −0.2% | +13.1% |
| 2011 | +1.6% | −7.2% | **−8.8%** | +1.9% |
| 2012 | +1.6% | +15.6% | **+14.0%** | +16.0% |
| 2013 | +7.0% | +6.7% | −0.3% | +32.3% |
| 2014 | −2.0% | −0.6% | +1.4% | +13.5% |
| 2015 | −4.2% | −0.2% | +4.0% | +1.2% |
| 2016 | +24.1% | +14.3% | **−9.8%** | +12.0% |
| 2017 | +3.9% | +3.9% | −0.0% | +21.7% |

## Score distribution — raw in-universe pool (252d forward return)

| Bucket | Signals | per yr | Avg 252d ret | Win % |
|---|--:|--:|--:|--:|
| 0.60–0.64 | 117 | 14.6 | **+19.3%** | **75%** |
| 0.65+ | 84 | 10.5 | +12.9% | 61% |

## Key question — does 0.65 also beat 0.60 out-of-sample?

**No — not in any meaningful sense. Treat the 2018–2024 result as overfitting and
keep the threshold at 0.60.** On the bare sign of the difference, 0.65 is nominally
ahead (CAGR +3.8% vs +3.7%, Sharpe 0.40 vs 0.35) — but every check that matters
says don't trust it:

1. **The gap is noise.** +$1,004 on $100k over 8 years (+0.1 CAGR point). With 21–26
   trades total, a single trade swings it. 0.65 also has a *worse* max drawdown
   (−24.9% vs −23.7%) and a *lower* win rate (57% vs 62%).
2. **Below the reliability floor.** 0.65 fires **2.6 signals/yr — under the 3/yr
   minimum** the threshold study itself set. By that rule it's already disqualified.
3. **No consistency.** 0.65 won 2012 (+14 pts) and 2015 (+4) but lost 2011 (−9) and
   2016 (−10). It is not reliably better in any regime — the aggregate edge rides on
   one or two years.
4. **The premise inverts.** The whole case for raising the bar was "higher score →
   higher return." Out-of-sample that **reverses**: the 0.60–0.64 bucket averaged
   **+19.3% (75% win)** vs **+12.9% (61% win)** for 0.65+. On 2010–2017 the *lower*
   scores were the better signals.

### Context
Both variants badly trail SPY here (+3.7–3.8% vs +13.6% CAGR). 2010–2017 was a
steady bull with few deep dislocations, so a buy-the-recovery signal had little to
work with and sat in cash much of the time — the chart shows long flat stretches.
That is a separate concern from the threshold question, but it reinforces that the
0.65 vs 0.60 difference is being read off a thin, low-opportunity sample.

**Recommendation:** keep the entry threshold at **0.60**. The 0.65 advantage does
not replicate out-of-sample, so adopting it would be fitting to the 2018–2024 noise.

Reproduce: `python scripts/run_oos_2010_2017.py`
