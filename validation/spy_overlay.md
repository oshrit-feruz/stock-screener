# SPY-core + conditional signal overlay vs pure SPY

Always fully invested in SPY; recovery signals fire only when the market is in a dislocation (SPY drawdown ≥ gate from its trailing 252-session high on the signal day), fill at the next session's close funded by rotating out of SPY, and rotate back on exit. Clean survivorship-free signal universe, 2-year sleeves, delisted names sold at their final print. **SPY over 2004-2024: CAGR +10.3%, Sharpe 0.62, MaxDD -55%. SPY median rolling 5y Sharpe 0.80.**

| config | full CAGR | full Sharpe | full MaxDD | rolling beat-SPY | med excess | med Sharpe | med DD |
|---|--:|--:|--:|--:|--:|--:|--:|
| always-deploy (idle→SPY, no gate) | +13.7% | 0.63 | -66% | 14/17 | +2.4% | 0.70 | -39% |
| gate: market DD ≥ 10% | +17.5% | 0.81 | -55% | 14/17 | +3.1% | 0.94 | -34% |
| gate: market DD ≥ 15% | +10.9% | 0.58 | -61% | 8/17 | +0.0% | 0.74 | -32% |
| gate: market DD ≥ 20% | +13.7% | 0.64 | -62% | 7/17 | +0.0% | 0.83 | -34% |
| gate ≥15% / 20% sleeves | +11.1% | 0.58 | -62% | 8/17 | +0.0% | 0.76 | -35% |

## Read

- SPY's own rolling 5y Sharpe is 0.80; a config only helps if it beats SPY in a clear majority of windows AND lifts the median Sharpe above that.
- Best config: **gate: market DD ≥ 10%** — beats SPY in 14/17 windows, median excess +3.1%, median Sharpe 0.94 (vs SPY 0.80).
- Compare to the STANDALONE signal (run_clean_pit_rolling). If the overlay lifts the beat-rate and median excess clearly above the standalone's, the sparse-alpha / wrong-deployment diagnosis holds.
