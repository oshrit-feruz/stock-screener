# Staged / confirmed entry on the SPY-core overlay (gate 10%, fixed 504)

Rolling 5y windows vs SPY (SPY median rolling Sharpe 0.80). Sleeve stats are over the full 2004-2024 run.

| variant | full CAGR | full Sharpe | full MaxDD | roll beat | med excess | med Sharpe | med DD | sleeves | avg trade | worst | <−30% | skipped |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| baseline | +16.1% | 0.77 | -55% | 14/50 | +3.2% | 0.90 | -33% | 50 | +65.2% | -99% | 14% | 0 |
| staged_3x10d | +15.4% | 0.75 | -56% | 14/50 | +3.2% | 0.92 | -33% | 50 | +58.5% | -99% | 14% | 0 |
| staged_4x5d | +15.4% | 0.75 | -56% | 14/50 | +2.9% | 0.91 | -33% | 50 | +59.2% | -99% | 16% | 0 |
| staged_5x10d | +15.4% | 0.75 | -56% | 14/50 | +3.2% | 0.95 | -32% | 50 | +57.0% | -99% | 14% | 0 |
| confirm_5d | +14.3% | 0.71 | -59% | 14/50 | +2.8% | 0.82 | -34% | 50 | +54.6% | -99% | 10% | 24 |
| confirm_10d | +13.8% | 0.71 | -60% | 14/50 | +3.0% | 0.91 | -31% | 50 | +50.1% | -99% | 14% | 16 |
| confirm_20d | +13.5% | 0.69 | -57% | 13/48 | +2.0% | 0.82 | -32% | 48 | +48.3% | -99% | 15% | 23 |
| confirm10+stg3 | +14.0% | 0.72 | -59% | 14/50 | +2.6% | 0.91 | -30% | 50 | +49.2% | -99% | 12% | 16 |

## Read

- Baseline: beat 14/50, median Sharpe 0.90, median DD -33%, avg sleeve +65.2%, worst -99%, 14% of sleeves lose >30%.
- Best by robust Sharpe: **staged_5x10d** — beat 14/50, median Sharpe 0.95, median DD -32%, avg sleeve +57.0%, worst -99%, 14% lose >30%.
- Staging should show up as a smaller share of >30% losers and a better worst trade; confirmation as fewer knife-catches (skipped) — the question is whether either buys that without giving back the beat-rate.
