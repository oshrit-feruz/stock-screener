# Clean point-in-time backtest — S&P 500, 2004-2024

Window **2004-01-02..2024-12-31** (5285 trading days, ~21.0y). Start $100,000.

**Universe (clean):** PIT S&P 500 membership (incl. delisted), ranked each quarter by trailing 63-day median dollar-volume, top 100. No survivorship bias, no lookahead.

**Signal:** recovery composite >= 0.60, **no fundamental gate** (pure price signal). **Sizing:** V1 — 10%/signal, max 10, no stop-loss, next-session fills. Late signals that cannot complete the hold are excluded.

## Summary

| Metric | H252 | H378 | H504 | SPY |
|---|--:|--:|--:|--:|
| Final value | $613,080 | $553,916 | $1,086,632 | $782,447 |
| Total return | +513.1% | +453.9% | +986.6% | +682.4% |
| CAGR | +9.0% | +8.5% | +12.0% | +10.3% |
| Sharpe | 0.46 | 0.45 | 0.58 | 0.62 |
| Max drawdown | -69.7% | -68.5% | -65.5% | -55.2% |
| Avg trade | +14.8% | +20.4% | +32.6% | — |
| Median trade | +12.0% | +11.9% | +16.2% | — |
| Win rate | 60% | 61% | 62% | — |
| Trades | 177 | 127 | 97 | — |
| …exited at final print (delisted) | 2 | 3 | 6 | — |
| Excluded (late) | 121 | 180 | 289 | — |

## Return distribution by holding bucket (share of trades)

| Bucket | H252 | H378 | H504 |
|---|--:|--:|--:|
| < -20% | 19% | 26% | 23% |
| -20..0% | 21% | 13% | 15% |
| 0..20% | 21% | 17% | 14% |
| 20..50% | 21% | 15% | 14% |
| 50..100% | 15% | 20% | 14% |
| >100% | 3% | 9% | 19% |

![portfolio](clean_pit_portfolio_2004.png)

## Interpretation (bias-free, ~21 years)

**The holding-period effect is NOT monotonic on the clean universe.** CAGR +9.0% -> +8.5% -> +12.0%, win rate 60% -> 61% -> 62%, >100% share 3% -> 9% -> 19% (H252 -> H378 -> H504).
Trades: 177 / 127 / 97 over ~21 years.

**Versus SPY:** H504 beat SPY (+10.3% CAGR); H252, H378 lose to it. The earlier 50-stock 2018-2024 test showed every variant crushing SPY — that was survivorship / selection bias.

**Tail risk is severe.** Max drawdowns of -70%..-66% (vs SPY -55%) — a concentrated, no-stop, dip-buying book got destroyed in 2008-09. The 2018-2024 window never contained a 2008.

### Method + data caveats
- **Fills:** a signal computed on a bar's close fills at the NEXT session's close; the hold clock runs from the fill (no same-bar look-ahead).
- **Delistings:** a name that stops trading mid-hold is sold at its final print (counted above), never carried at a stale quote.
- **Size proxy:** ranked by dollar-volume, not exact market cap (unavailable free pre-2015). Highly correlated for mega-caps, not identical.
- **No fundamental gate:** pure price signal (the gate needs EDGAR data that would re-introduce survivorship bias over 25y).
- **Fetch coverage:** 1054/1090 ever-members fetched (96.7%); FB recovered by aliasing FB->META. The ~36 missing are mostly bankruptcies/acquisitions with no free continuing series (LEHMQ, RSHCQ, AAMRQ, APC, CAM, PCL...). Some of those would have been big *losers* had the signal fired on them, so their absence may slightly *flatter* results.
