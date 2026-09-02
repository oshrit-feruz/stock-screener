# Adaptive exit rules on the SPY-core overlay (gate 10%, max hold 504)

Rolling 5y windows vs SPY (SPY median rolling Sharpe 0.80). All rules keep the 504-day backstop.

| rule | full CAGR | full Sharpe | full MaxDD | roll beat | med excess | med Sharpe | med DD | avg trade | win% | exits (full) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| fixed | +15.8% | 0.76 | -55% | 14/17 | +3.2% | 0.92 | -33% | +67.5% | 70% | {'backstop': 47} |
| trail_15 | +10.8% | 0.60 | -52% | 8/17 | +0.0% | 0.73 | -32% | +6.9% | 44% | {'trail': 209, 'backstop': 8} |
| trail_20 | +13.2% | 0.69 | -54% | 11/17 | +2.3% | 0.75 | -34% | +18.2% | 45% | {'trail': 138, 'backstop': 17} |
| trail_25 | +11.6% | 0.61 | -55% | 9/17 | +0.8% | 0.74 | -33% | +19.4% | 46% | {'trail': 111, 'backstop': 22} |
| trail_30 | +11.9% | 0.64 | -54% | 11/17 | +2.6% | 0.89 | -33% | +26.9% | 49% | {'trail': 70, 'backstop': 35} |
| sma50 | +9.9% | 0.52 | -67% | 8/17 | +0.0% | 0.72 | -34% | +4.4% | 57% | {'sma': 142} |
| recover | +14.1% | 0.69 | -57% | 13/17 | +2.3% | 0.73 | -32% | +39.4% | 79% | {'recover': 34, 'backstop': 27} |

## Read

- Baseline (fixed 504): beat 14/17, median Sharpe 0.92, median DD -33%.
- Best by robust Sharpe: **fixed** — beat 14/17, median Sharpe 0.92, median DD -33%, median excess +3.2%.
- A rule only earns its place if it lifts median Sharpe / cuts drawdown WITHOUT lowering the beat-rate — trailing stops that fire too early clip recovery winners just like a take-profit did.
