# Clean point-in-time backtest — S&P 500, 2000-2024

Window **2000-01-03..2024-12-31** (6289 trading days, ~25.0y). Start $100,000.

**Universe (clean):** PIT S&P 500 membership (incl. delisted), ranked each quarter by trailing 63-day median dollar-volume, top 100. No survivorship bias, no lookahead.

**Signal:** recovery composite >= 0.60, **no fundamental gate** (pure price signal). **Sizing:** V1 — 10%/signal, max 10, no stop-loss, next-session fills. Late signals that cannot complete the hold are excluded.

## Summary

| Metric | H252 | H378 | H504 | SPY |
|---|--:|--:|--:|--:|
| Final value | $505,102 | $785,477 | $1,925,209 | $632,301 |
| Total return | +405.1% | +685.5% | +1825.2% | +532.3% |
| CAGR | +6.7% | +8.6% | +12.6% | +7.7% |
| Sharpe | 0.37 | 0.44 | 0.59 | 0.48 |
| Max drawdown | -69.7% | -69.5% | -69.8% | -55.2% |
| Avg trade | +12.0% | +20.9% | +35.1% | — |
| Median trade | +8.7% | +14.7% | +15.7% | — |
| Win rate | 59% | 64% | 65% | — |
| Trades | 217 | 154 | 115 | — |
| …exited at final print (delisted) | 2 | 3 | 3 | — |
| Excluded (late) | 121 | 180 | 289 | — |

## Return distribution by holding bucket (share of trades)

| Bucket | H252 | H378 | H504 |
|---|--:|--:|--:|
| < -20% | 22% | 24% | 22% |
| -20..0% | 19% | 12% | 13% |
| 0..20% | 20% | 17% | 18% |
| 20..50% | 21% | 19% | 17% |
| 50..100% | 15% | 18% | 15% |
| >100% | 2% | 10% | 16% |

![portfolio](clean_pit_portfolio_2000.png)

## Interpretation (bias-free, ~25 years)

**The holding-period effect holds on the clean universe.** CAGR +6.7% -> +8.6% -> +12.6%, win rate 59% -> 64% -> 65% and the fat right tail (>100% trades) 2% -> 10% -> 16% all improve with hold length (H252 -> H378 -> H504).
Trades: 217 / 154 / 115 over ~25 years.

**Versus SPY:** H378, H504 beat SPY (+7.7% CAGR); H252 lose to it. The earlier 50-stock 2018-2024 test showed every variant crushing SPY — that was survivorship / selection bias.

**Tail risk is severe.** Max drawdowns of -70%..-70% (vs SPY -55%) — a concentrated, no-stop, dip-buying book got destroyed in 2002 and 2008-09. The 2018-2024 window never contained a 2008.

### Method + data caveats
- **Fills:** a signal computed on a bar's close fills at the NEXT session's close; the hold clock runs from the fill (no same-bar look-ahead).
- **Delistings:** a name that stops trading mid-hold is sold at its final print (counted above), never carried at a stale quote.
- **Size proxy:** ranked by dollar-volume, not exact market cap (unavailable free pre-2015). Highly correlated for mega-caps, not identical.
- **No fundamental gate:** pure price signal (the gate needs EDGAR data that would re-introduce survivorship bias over 25y).
- **Fetch coverage:** 1054/1090 ever-members fetched (96.7%); FB recovered by aliasing FB->META. The ~36 missing are mostly bankruptcies/acquisitions with no free continuing series (LEHMQ, RSHCQ, AAMRQ, APC, CAM, PCL...). Some of those would have been big *losers* had the signal fired on them, so their absence may slightly *flatter* results.
