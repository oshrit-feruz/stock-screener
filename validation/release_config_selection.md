# Best config to release — ranked by robust risk-adjusted return

Candidate configs judged on their distribution of Sharpe across **17 rolling 5-year windows** (2004-2024), clean survivorship-free universe, pure price signal. Ranked by **median rolling Sharpe**. SPY's median rolling Sharpe over the same windows: **0.80**.

| rank | config | med Sharpe | ±std | worst | med excess vs SPY | med DD | beat-SPY-Sharpe | avg trades |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| 1 | 2y / 5%×20 (diversified) | **0.71** | 0.33 | -0.08 | -1.4% | -32% | 6/17 | 33 |
| 2 | 2y / 10%×10 | **0.67** | 0.35 | -0.19 | -1.1% | -36% | 6/17 | 19 |
| 3 | 3y / 10%×10 | **0.66** | 0.34 | -0.12 | -2.1% | -31% | 3/17 | 10 |
| 4 | 2y / SL−40% / 10%×10 | **0.65** | 0.38 | -0.18 | -3.5% | -34% | 3/17 | 23 |
| 5 | 2y / SL−30% / 10%×10 | **0.64** | 0.36 | -0.08 | -0.7% | -29% | 3/17 | 25 |
| 6 | 1.5y / 10%×10 | **0.62** | 0.30 | -0.05 | +1.7% | -37% | 6/17 | 26 |
| 7 | 2y / 20%×5 (concentrated) | **0.60** | 0.40 | -0.25 | +1.9% | -39% | 6/17 | 10 |
| 8 | 2y / SL−40% / 20%×5 | **0.56** | 0.42 | -0.18 | -2.1% | -36% | 5/17 | 12 |
| 9 | 1y / SL−40% / 10%×10 | **0.53** | 0.32 | 0.00 | -0.4% | -37% | 6/17 | 42 |
| 10 | default 1y / 10%×10 | **0.49** | 0.31 | 0.10 | -0.2% | -37% | 6/17 | 37 |
| 11 | 3y / 20%×5 (concentrated) | **0.46** | 0.28 | -0.02 | -4.7% | -35% | 5/17 | 5 |

## Winner: **2y / 5%×20 (diversified)**

Median rolling Sharpe 0.71 vs SPY 0.80, std 0.33 (stability), worst-window Sharpe -0.08, median drawdown -32%, beats SPY's Sharpe in 6/17 windows.

Reminder: even the winner does not reliably beat SPY (the edge is regime-dependent, per run_clean_pit_rolling). This is the most stable risk-adjusted config to ship IF building on the signal regardless — not a claim that it beats the index.
