# Adaptive exit rules on the SPY-core overlay (gate 10%, max hold 504)

Rolling 5y windows vs SPY (SPY median rolling Sharpe 0.80). All rules keep the 504-session backstop; next-session fills; delisted names sold at their final print.

| rule | full CAGR | full Sharpe | full MaxDD | roll beat | med excess | med Sharpe | med DD | avg trade | win% | exits (full) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| fixed | +17.5% | 0.81 | -55% | 14/17 | +3.1% | 0.94 | -34% | +73.6% | 73% | {'hold': 50, 'delist': 2} |
| trail_15 | +11.6% | 0.62 | -53% | 9/17 | +0.7% | 0.72 | -34% | +7.9% | 42% | {'trail': 220, 'delist': 2, 'hold': 7} |
| trail_20 | +13.1% | 0.66 | -56% | 10/17 | +2.2% | 0.79 | -36% | +18.3% | 46% | {'trail': 154, 'delist': 2, 'hold': 15} |
| trail_25 | +10.4% | 0.56 | -55% | 9/17 | +0.7% | 0.81 | -35% | +16.0% | 41% | {'trail': 114, 'delist': 4, 'hold': 24} |
| trail_30 | +12.6% | 0.64 | -51% | 12/17 | +3.0% | 0.88 | -35% | +27.0% | 47% | {'trail': 81, 'hold': 36, 'delist': 1} |
| sma50 | +9.0% | 0.49 | -69% | 8/17 | +0.0% | 0.70 | -34% | +3.0% | 55% | {'sma': 143} |
| recover | +14.4% | 0.70 | -56% | 13/17 | +1.4% | 0.75 | -32% | +38.6% | 78% | {'recover': 35, 'hold': 26, 'delist': 2} |

## Read

- Baseline (fixed 504): beat 14/17, median Sharpe 0.94, median DD -34%.
- Best by robust Sharpe: **fixed** — beat 14/17, median Sharpe 0.94, median DD -34%, median excess +3.1%.
- A rule only earns its place if it lifts median Sharpe / cuts drawdown WITHOUT lowering the beat-rate — trailing stops that fire too early clip recovery winners just like a take-profit did.
