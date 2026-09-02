# Staged / confirmed entry on the SPY-core overlay (gate 10%, fixed 504)

Rolling 5y windows vs SPY (SPY median rolling Sharpe 0.80). Sleeve stats are over the full 2004-2024 run. Baseline = full sleeve at the first session after the signal.

| variant | full CAGR | full Sharpe | full MaxDD | roll beat | med excess | med Sharpe | med DD | sleeves | avg trade | worst | <−30% | skipped |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| baseline | +17.5% | 0.81 | -55% | 14/17 | +3.1% | 0.94 | -34% | 17 | +73.6% | -99% | 12% | 0 |
| staged_3x10d | +16.7% | 0.79 | -56% | 14/17 | +3.2% | 0.94 | -34% | 17 | +66.6% | -99% | 13% | 0 |
| staged_4x5d | +16.9% | 0.79 | -56% | 14/17 | +2.9% | 0.95 | -34% | 17 | +68.2% | -99% | 15% | 0 |
| staged_5x10d | +16.6% | 0.79 | -56% | 14/17 | +3.1% | 0.93 | -34% | 17 | +64.3% | -99% | 13% | 0 |
| confirm_5d | +15.4% | 0.75 | -59% | 14/17 | +2.1% | 0.81 | -34% | 17 | +60.2% | -99% | 12% | 26 |
| confirm_10d | +13.9% | 0.70 | -60% | 13/17 | +1.9% | 0.81 | -34% | 17 | +50.5% | -99% | 13% | 28 |
| confirm_20d | +13.2% | 0.67 | -58% | 11/17 | +1.0% | 0.78 | -34% | 17 | +48.4% | -99% | 14% | 26 |
| confirm10+stg3 | +13.7% | 0.70 | -59% | 11/17 | +2.1% | 0.82 | -34% | 17 | +48.1% | -99% | 13% | 28 |

## Read

- Baseline: beat 14/17, median Sharpe 0.94, median DD -34%, avg sleeve +73.6%, worst -99%, 12% of sleeves lose >30%.
- Best: **baseline** — beat 14/17, median Sharpe 0.94, median DD -34%, avg sleeve +73.6%, worst -99%, 12% lose >30%. (A variant counts as better only if it lifts median Sharpe by ≥0.03 without lowering the beat-rate or the average sleeve; smaller gaps are noise.)
- Staging should show up as a smaller share of >30% losers and a better worst trade; confirmation as fewer knife-catches (skipped) — the question is whether either buys that without giving back the beat-rate.
