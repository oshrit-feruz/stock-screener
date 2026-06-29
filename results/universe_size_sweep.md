# Universe-size sweep — does the signal need large-caps?

Hypothesis: the recovery signal was designed for high-quality large-caps, and
applying it to all 500+ point-in-time S&P 500 names dilutes the edge. Test: same
V1 signal/sizing (10%/max10, 252d exit, no SL, $100k, 2018–2024) on the
point-in-time S&P 500 restricted to the top-N names by market cap.

| Variant | Final $ | Total ret | CAGR | Sharpe | Max DD | Trades | Win % | Sig/yr |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| Full S&P 500 (PIT) | $88,830 | −11.2% | −1.7% | 0.09 | −54.8% | 67 | 45% | 9.6 |
| **Top 100 by mcap** | **$401,976** | **+302.0%** | **+22.0%** | **0.92** | −31.5% | 43 | 74% | 6.1 |
| Top 150 by mcap | $332,135 | +232.1% | +18.7% | 0.79 | −33.4% | 47 | 66% | 6.7 |
| Top 200 by mcap | $289,442 | +189.4% | +16.4% | 0.70 | −42.8% | 54 | 63% | 7.7 |
| Original 50 survivors | $294,709 | +194.7% | +16.7% | 0.86 | −32.2% | 33 | 70% | 4.7 |
| SPY buy & hold | $245,098 | +145.1% | +13.7% | 0.76 | −33.7% | — | — | — |

## Key question — a top-N (100–200) that beats SPY on Sharpe?

**Yes on the raw numbers: Top 100 (Sharpe 0.92) and Top 150 (0.79) both beat SPY
(0.76); Top 100 also beats the 50 hand-picked survivors (0.86).** The effect is
cleanly monotonic — Sharpe falls 0.92 → 0.79 → 0.70 → 0.09 as the universe grows
100 → 150 → 200 → full, and win rate falls 74% → 66% → 63% → 45%. Directionally,
the signal clearly works *much* better on larger names.

**But this is NOT a clean, survivorship-free result, and the magnitude is not
trustworthy.** The size filter ranks by **current** market cap (a static value,
cached 30 days) applied to every historical date. Two consequences:

1. **Look-ahead.** Ranking 2018 membership by 2024 size puts the companies that
   *became* huge (NVDA, AVGO, …) at the top of 2018 — NVDA ranks #1 in Jan-2018
   here, which it was not. That Top 100 even out-earns the cherry-picked 50
   survivors is the tell: it is essentially "buy the eventual mega-winners."
2. **Delisted names dropped.** Names with no current cap (mostly companies that
   fell out of the index) get no rank and are excluded — removing exactly the bad
   outcomes the point-in-time universe was built to include.

So the honest reading: the *direction* (signal favours large, high-quality names;
the full-universe −1.7% is dominated by small/marginal names that dip and don't
recover) is credible and consistent with the design intent. The *level* (+22%
CAGR, Sharpe 0.92) is inflated by reintroduced survivorship/look-ahead bias and
should not be quoted as the signal's true large-cap edge.

### Signal density — small universes are not too thin

6.1–7.7 completed trades/year for the top-N variants vs 9.6 (full) and 4.7 (the
50 survivors). Thinness is not the binding constraint; the top-N variants have
*more* trades than the survivor baseline and comparable density to the full set.

### To get a clean answer

Rank by **point-in-time** market cap — historical price × shares outstanding from
EDGAR companyfacts (already cached for most names) — instead of current cap. That
removes the look-ahead in the ranking and keeps delisted names in contention,
giving an unbiased read on whether the signal genuinely has a large-cap edge.

Reproduce: `python scripts/run_universe_size_sweep.py`
