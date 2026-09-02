# Out-of-sample validation of the dislocation gate D

SPY-core + conditional overlay, 2y sleeves, 10%×10, clean universe.

## A. Threshold sensitivity — rolling 5y windows (is 10% a plateau?)

| D (market DD gate) | beat SPY | median excess | median Sharpe |
|---|--:|--:|--:|
| 5% | 14/17 | +3.7% | 0.83 |
| 8% | 12/17 | +4.8% | 0.81 |
| 10% | 14/17 | +3.2% | 0.90 |
| 12% | 13/17 | +2.8% | 0.87 |
| 15% | 11/17 | +0.6% | 0.75 |
| 20% | 7/17 | +0.0% | 0.83 |

## B. Train / test split (D chosen on train, measured on test)

| train (pick D) | best D | train excess | test | test excess | test Sharpe | vs SPY | verdict |
|---|--:|--:|---|--:|--:|--:|:--|
| 2004-2013 | 5% | +4.3% | 2014-2024 | +9.1% | 0.96 | 0.81 | ✅ BEATS |
| 2014-2024 | 10% | +10.0% | 2004-2013 | +1.6% | 0.48 | 0.45 | ✅ BEATS |

## Read

- Positive, majority-beating D values: 5%, 8%, 10%, 12%, 15% — a plateau here (not a lone 10% spike) means the gate is robust, not fit.
- If the train-chosen D still beats SPY on the untouched test half in BOTH directions, the threshold generalizes out-of-sample.
