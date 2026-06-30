# Market-regime filter (SPY 200-day MA) — clean Top-100 PIT

Tests blocking new entries while SPY closes below its 200-day SMA (existing
positions held, idle cash still earns Fed funds). Baseline is the previous best
(flat 10% + Fed funds). 2018–2024, 252d exit, no SL, $100k.

| Variant | Final $ | CAGR | Sharpe | Max DD | Trades | Win % | Sig/yr |
|---|--:|--:|--:|--:|--:|--:|--:|
| A · No filter (best baseline) | $296,397 | +16.8% | **0.82** | −31.4% | 41 | 66% | 5.9 |
| B · Regime filter | $177,059 | +8.5% | 0.62 | −27.4% | 31 | 58% | 4.4 |
| SPY buy & hold | $245,098 | +13.7% | 0.76 | −33.7% | — | — | — |

## Regime breakdown — SPY vs 200-day MA, signals blocked per year

SPY was **above** its 200d MA on **80%** of trading days (1,413) and **below** on
**20%** (347). The filter blocked **235 signals** total:

| Year | Days below MA | Signals blocked |
|---|--:|--:|
| 2018 | 41 | 7 |
| 2019 | 27 | 6 |
| 2020 | 59 | **70** |
| 2021 | 0 | 0 |
| 2022 | 204 | **145** |
| 2023 | 16 | 7 |
| 2024 | 0 | 0 |
| **Total** | 347 | 235 |

## Year-by-year return

| Year | A no-filter | B regime | B − A | SPY |
|---|--:|--:|--:|--:|
| 2018 | −1.3% | +2.0% | +3.3% | −5.2% |
| 2019 | +33.0% | +13.9% | −19.1% | +31.2% |
| 2020 | +70.4% | +5.8% | **−64.6%** | +18.3% |
| 2021 | +12.7% | +18.9% | +6.2% | +28.7% |
| 2022 | −28.2% | −23.1% | **+5.1%** | −18.2% |
| 2023 | +40.6% | +35.4% | −5.2% | +26.2% |
| 2024 | +16.5% | +16.4% | −0.1% | +25.3% |

## Key question — higher Sharpe & lower Max DD without hurting CAGR?

**No — emphatically.** The filter shaves max drawdown by 4 points (−31.4% →
−27.4%) but **tanks both CAGR (16.8% → 8.5%, −8.3 pts) and Sharpe (0.82 → 0.62,
−0.20)**. Filtered, the strategy now *underperforms SPY* on both return and Sharpe.

### Why the filter backfires
This recovery signal is **counter-trend by design** — it buys dips during and
right after market dislocations, which is *exactly* when SPY is below its 200-day
MA. The filter blocks the signal at its best moments:

- **2020 is the killer.** The COVID bottom (March 2020) is below the 200d MA; the
  filter blocked 70 signals and B made just +5.8% vs A's +70.4% — a 64.6-point
  hole that the rest of the backtest never fills.
- **2019** repeats it on a smaller scale: early-2019 recovery signals (after the
  late-2018 selloff) were blocked → −19.1 pts.
- **2022 is the one win** (+5.1 pts): blocking 145 falling-knife signals during
  the bear genuinely helped. But that single good year is dwarfed by the 2020 and
  2019 costs.

**Conclusion:** a trend filter is the wrong tool for a mean-reversion/recovery
signal — it suppresses precisely the entries that make the signal work. Do not
add it. (If tail-risk control is the goal, prefer position-level stops or sizing,
not a market-trend gate.)

Reproduce: `python scripts/run_regime_filter.py`
