# Best standalone config to release — ranked by robust risk-adjusted return

Candidate configs judged on their distribution of Sharpe across **17 rolling 5-year windows** (2004-2024), clean survivorship-free universe, pure price signal, next-session fills. Ranked by **median rolling Sharpe**. SPY's median rolling Sharpe over the same windows: **0.80**.

| rank | config | med Sharpe | ±std | worst | med excess vs SPY | med DD | beat-SPY-Sharpe |
|---|---|--:|--:|--:|--:|--:|--:|
| 1 | 2y / SL−40% / 20%×5 | **0.77** | 0.39 | -0.23 | +2.9% | -37% | 7/17 |
| 2 | 2y / 20%×5 (concentrated) | **0.71** | 0.37 | -0.27 | +1.9% | -38% | 6/17 |
| 3 | 2y / 5%×20 (diversified) | **0.69** | 0.36 | -0.12 | -1.2% | -30% | 5/17 |
| 4 | 3y / 10%×10 | **0.69** | 0.34 | -0.12 | -2.6% | -32% | 3/17 |
| 5 | 2y / 10%×10 | **0.63** | 0.36 | -0.36 | -0.6% | -37% | 5/17 |
| 6 | 2y / SL−40% / 10%×10 | **0.63** | 0.35 | -0.17 | -1.9% | -34% | 5/17 |
| 7 | 2y / SL−30% / 10%×10 | **0.59** | 0.37 | -0.30 | -0.8% | -32% | 7/17 |
| 8 | default 1y / 10%×10 | **0.53** | 0.33 | 0.06 | -0.4% | -36% | 7/17 |
| 9 | 1y / SL−40% / 10%×10 | **0.53** | 0.33 | 0.09 | -1.2% | -34% | 6/17 |
| 10 | 1.5y / 10%×10 | **0.52** | 0.28 | -0.06 | +0.1% | -37% | 6/17 |
| 11 | 3y / 20%×5 (concentrated) | **0.48** | 0.26 | -0.03 | -4.8% | -37% | 5/17 |

## Winner: **2y / SL−40% / 20%×5**

Median rolling Sharpe 0.77 vs SPY 0.80, std 0.39, worst-window Sharpe -0.23, median drawdown -37%, beats SPY's Sharpe in 7/17 windows (ranked on median Sharpe only; the other columns are context, not tie-breakers).

Reminder: the winner's median rolling Sharpe is below SPY's, and the standalone edge is regime-dependent (per run_clean_pit_rolling). This is the most stable risk-adjusted STANDALONE config to ship IF building on the signal regardless — not a claim that it beats the index. The SPY-core overlay (run_spy_overlay) is the deployment that does.
