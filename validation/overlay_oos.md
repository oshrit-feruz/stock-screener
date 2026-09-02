# Out-of-sample validation of the dislocation gate

SPY-core + conditional overlay, 2y sleeves, 10%×10, clean universe, next-session fills.

## A. Threshold sensitivity — rolling 5y windows (is 10% a plateau?)

| gate (market DD) | beat SPY | median excess | median Sharpe |
|---|--:|--:|--:|
| 5% | 15/17 | +4.5% | 0.81 |
| 8% | 12/17 | +4.8% | 0.84 |
| 10% | 14/17 | +3.1% | 0.94 |
| 12% | 13/17 | +2.4% | 0.87 |
| 15% | 8/17 | +0.0% | 0.74 |
| 20% | 7/17 | +0.0% | 0.83 |

## B. Train / test split (gate chosen on train, measured on test)

| train (pick gate) | best gate | train excess | test | test excess | test Sharpe | vs SPY | verdict |
|---|--:|--:|---|--:|--:|--:|:--|
| 2004-2013 | 5% | +3.6% | 2014-2024 | +9.8% | 0.95 | 0.81 | ✅ BEATS |
| 2014-2024 | 10% | +13.0% | 2004-2013 | +1.5% | 0.48 | 0.45 | ✅ BEATS |

## Read

- Positive, majority-beating gate values: 5%, 8%, 10%, 12%. A plateau (rather than a lone 10% spike) is evidence of parameter INSENSITIVITY — it supports robustness but does not by itself establish that the gate is not fit.
- The train-chosen gate beats SPY on the untouched test half in both directions. With only two splits this is supporting evidence, not proof of generalisation.
- Caveats: the 17 five-year windows overlap (each year appears in up to five of them), so they are far from independent samples; and the edge is concentrated in a handful of dislocations (2008-09, 2020, 2022), so the effective sample of regime events is small.
