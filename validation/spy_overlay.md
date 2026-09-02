# SPY-core + conditional signal overlay vs pure SPY

Always fully invested in SPY; recovery signals fire only when the market is in a dislocation (SPY drawdown ≥ D from its trailing 252-day high), funded by rotating out of SPY and rotating back on exit. Clean survivorship-free signal universe, 2-year sleeves. **SPY over 2004-2024: CAGR +10.3%, Sharpe 0.62, MaxDD -55%. SPY median rolling 5y Sharpe 0.80.**

| config | full CAGR | full Sharpe | full MaxDD | rolling beat-SPY | med excess | med Sharpe |
|---|--:|--:|--:|--:|--:|--:|
| always-deploy (idle→SPY, no gate) | +15.2% | 0.68 | -64% | 13/17 | +2.2% | 0.68 |
| gate: market DD ≥ 10% | +16.1% | 0.77 | -55% | 14/17 | +3.2% | 0.90 |
| gate: market DD ≥ 15% | +11.1% | 0.59 | -62% | 11/17 | +0.6% | 0.75 |
| gate: market DD ≥ 20% | +12.9% | 0.62 | -62% | 7/17 | +0.0% | 0.83 |
| gate ≥15% / 20% sleeves | +10.1% | 0.55 | -62% | 8/17 | +0.0% | 0.78 |

## Read

- SPY's own rolling 5y Sharpe is 0.80; a config only helps if it beats SPY in a clear majority of windows AND lifts the median Sharpe above that.
- Best gating config: **gate: market DD ≥ 10%** — beats SPY in 14/17 windows, median excess +3.2%, median Sharpe 0.90 (vs SPY 0.80).
- Compare to the STANDALONE signal (run_clean_pit_rolling): 8/17 windows, median excess ≈ 0. If the overlay lifts the beat-rate and median excess clearly above that, the sparse-alpha / wrong-deployment diagnosis holds.
