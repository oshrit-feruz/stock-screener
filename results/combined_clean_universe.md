# Combined validation on the clean universe (Top-100 point-in-time)

The three validated improvements, run together on the survivorship-free universe
(point-in-time S&P 500, restricted each month to the top-100 by point-in-time
market cap). Same params: 2018–2024, V1 10%/max10, 252d exit, no SL, $100k.

| Variant | Final $ | Total ret | CAGR | Sharpe | Max DD |
|---|--:|--:|--:|--:|--:|
| A · Clean baseline (flat, 0%) | $278,769 | +178.8% | +15.8% | 0.78 | −31.5% |
| B · Money market (flat, FedFunds) | $296,397 | +196.4% | +16.8% | **0.82** | −31.4% |
| C · Score-Plus (0%) | $281,603 | +181.6% | +15.9% | 0.76 | −32.3% |
| **D · Full combo (Score-Plus + FedFunds)** | **$298,439** | **+198.4%** | **+16.9%** | 0.79 | −32.1% |
| SPY buy & hold | $245,098 | +145.1% | +13.7% | 0.76 | −33.7% |

## Linearity check — still additive

| | Final $ |
|---|--:|
| Baseline A | $278,769 |
| + Money-market lift (B − A) | +$17,628 |
| + Score-Plus lift (C − A) | +$2,834 |
| **= Expected D (linear)** | **$299,231** |
| Actual D | $298,439 |
| **Interaction** | **−$792** (additive) |

The two levers remain additive on the clean universe — the interaction is a
rounding error (−$792 on a ~$20k combined lift).

## Key question — does D beat SPY on both CAGR and Sharpe, survivorship-free?

**YES.** D: CAGR +16.9%, Sharpe 0.79 vs SPY +13.7%, 0.76 — ahead on both, with no
survivorship bias in either the membership or the size ranking. It also beats SPY
in every year-agnostic sense here (higher return, higher Sharpe, shallower max
drawdown −32.1% vs −33.7%).

## Read before making product decisions

1. **The edge is real but modest.** D clears SPY by +3.2 CAGR points and +0.03
   Sharpe. On the survivor universe the same stack looked far stronger; most of
   that was bias. This is the honest size of the edge.
2. **Money market does almost all the lifting** (+$17.6k of the +$19.7k combined).
   Score-Plus adds only +$2.8k on the clean universe (vs ~$11k on survivors) and
   slightly *lowers* Sharpe (C 0.76 < A 0.78; D 0.79 < B 0.82) — it buys a little
   return with a little more risk.
3. **Best risk-adjusted variant is B, not D.** Money-market-only has the highest
   Sharpe (0.82) and nearly D's return. If the product optimises for Sharpe,
   B (flat 10% + Fed funds sleeve) is the cleaner choice; Score-Plus is optional.

Reproduce: `python scripts/run_combined_clean_universe.py`
