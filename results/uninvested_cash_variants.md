# Uninvested-cash variants — V1 signal portfolio

**Strategy held fixed:** V1 base case — 10% of portfolio value per signal, max 10
concurrent positions, 252-trading-day hold. **$100,000 start, 2018-01-02 →
2024-12-31.** The *only* thing that changes across variants is what the idle cash
(capital not currently deployed in an active position) earns. Signals, sizing,
and exits are identical.

Reproduce: `python scripts/run_uninvested_cash_variants.py`
(price/EDGAR caches first populated via `scripts/_fetch_price_cache.py`).

## Comparison table

| Variant | Idle-cash treatment | Final $ | Total ret | CAGR | Max DD | Sharpe |
|---|---|--:|--:|--:|--:|--:|
| **Zero** (baseline V1) | earns 0% | $293,354 | +193.4% | +16.6% | −32.2% | 0.86 |
| **Money market** | 4.5%/yr, daily pro-rata | $343,924 | +243.9% | +19.3% | −31.0% | 0.98 |
| **SPY** (variant B) | parked in SPY; sold at signal-day price to fund, returns to SPY at close-day price | $342,085 | +242.1% | +19.2% | −39.0% | 0.80 |
| **SPY neutralized** | SPY's realized window CAGR (13.6%) as a smooth daily rate — path/timing removed | $467,141 | +367.1% | +24.7% | −30.2% | 1.20 |
| *SPY buy & hold (ref)* | *100% in SPY* | *$244,206* | *+144.2%* | *+13.6%* | *—* | *—* |

### Timing column — SPY variant only (cost of poor timing)

The 33 days on which cash was pulled out of the SPY sleeve to fund a new position:

| Metric | Value |
|---|--:|
| Sell events | 33 |
| Avg SPY level on sell day | 328.40 |
| Avg SPY level 30 calendar days later | 335.18 |
| **Avg per-event SPY move over next 30d** | **+3.86%** |
| Median per-event move | +1.87% |
| Share of sells followed by a higher SPY | 55% |

On average SPY traded **+3.86% higher one month after** each sell — the SPY sleeve
was liquidated into the very dislocations (Mar 2020, 2022) that fire the recovery
signal, i.e. sold near local bottoms and missed the bounce.

**Cost of SPY's actual path** = SPY neutralized − SPY = $467,141 − $342,085 =
**+$125,056**. Stripping out SPY's volatility/drawdowns and the sell-at-the-bottom
timing would have added ~$125k. The +3.86% sell-day timing figure above is one
documented component of that cost; the rest is the equity drawdown the sleeve
carries (it is down −39% at the trough vs −30% for the neutralized control).

## Note — does variant B (SPY) beat money market?

**No.** SPY ends at **$342,085** versus money market at **$343,924** — about
**$1,839 behind** — and it gets there with materially *worse* risk: max drawdown
−39.0% vs −31.0%, Sharpe 0.80 vs 0.98.

Because the answer is "no," the follow-up ("is the gap justified relative to the
risk sold at the bottom?") resolves cleanly: there is **no excess return to
justify**. Parking idle cash in SPY does earn the equity risk premium, but the
strategy is *structurally forced to realize that risk at the worst moments* —
selling the SPY sleeve into the same drawdowns that trigger the signal. That
forced selling (+3.86% average rebound missed) plus the added equity drawdown
exactly cancels the risk premium, leaving the volatile SPY sleeve a hair behind a
free, riskless 4.5% money-market sleeve.

The neutralized control makes the mechanism explicit: had the idle cash earned
SPY's *average* return without SPY's path (no drawdowns, no bad-timing sales), it
would have been worth **$467,141 (Sharpe 1.20)** — the best of all variants. The
$125k gap between that and the real SPY variant is the price of being a forced
seller at the bottom.

**Takeaway:** for this dip-buying strategy, the right treatment of idle cash is a
riskless yield (money market), *not* SPY. The signal already concentrates risk
into market dislocations; layering an equity cash sleeve on top doubles down on
the same drawdown and forces you to sell it precisely when it is cheapest.
