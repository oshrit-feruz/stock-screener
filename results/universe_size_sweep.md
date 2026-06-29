# Universe-size sweep — point-in-time market-cap ranking

Tests whether the recovery signal has a genuine large-cap edge, with the
size ranking now computed **point-in-time** (the earlier version ranked by
*current* market cap, which leaked look-ahead). Same V1 signal/sizing
(10%/max10, 252d exit, no SL, $100k, 2018–2024).

**Ranking = raw (unadjusted) close × shares outstanding from EDGAR**, with the
same 90-day filing lag as the quality gate. Raw prices are un-split from Yahoo's
(split-adjusted) `quote.close` so future-splitters (NVDA 40:1, AMZN/GOOGL 20:1)
are not deflated. If either price or EDGAR shares is missing, the ticker is
excluded — no fallback to current market cap.

### Sanity check (2018-01-02) — passes
Top by PIT market cap: **AAPL $890B (#1), GOOGL, GOOG, MSFT, AMZN (#2–5), JPM
(#6)**. **NVDA is rank 35 (~$127B)** — not top-5, exactly as required. (Under the
old current-cap ranking NVDA floated to the top; that look-ahead is gone.)

## Results

| Variant | Final $ | Total ret | CAGR | Sharpe | Max DD | Trades | Win % | Sig/yr | Avg PIT mcap |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Full S&P 500 (PIT) | $88,830 | −11.2% | −1.7% | 0.09 | −54.8% | 67 | 45% | 9.6 | $31B |
| **Top 100 by mcap** | $278,769 | +178.8% | +15.8% | **0.78** | −31.5% | 41 | 66% | 5.9 | $210B |
| Top 150 by mcap | $247,155 | +147.2% | +13.8% | 0.65 | −36.2% | 48 | 58% | 6.9 | $166B |
| Top 200 by mcap | $247,196 | +147.2% | +13.8% | 0.63 | −33.7% | 55 | 58% | 7.9 | $141B |
| Original 50 survivors | $294,709 | +194.7% | +16.7% | 0.86 | −32.2% | 33 | 70% | 4.7 | $260B |
| SPY buy & hold | $245,098 | +145.1% | +13.7% | 0.76 | −33.7% | — | — | — | — |

The **Avg PIT mcap** column confirms the ranking is working: $31B (full) → $210B
(top-100) → $166B → $141B → $260B (survivors). Signaled tickers are tens-to-low-
hundreds of $B — not trillions — so the NVDA-style inflation is gone.

## Key question — does a top-N (100–200) beat SPY on Sharpe, cleanly?

**Barely, and only at N=100.** Top-100 Sharpe 0.78 vs SPY 0.76 — a 0.02 edge.
Top-150 (0.65) and Top-200 (0.63) fall **below** SPY. Sharpe is still cleanly
monotonic in size (0.78 → 0.65 → 0.63 → 0.09 from top-100 → 150 → 200 → full), so
the *direction* holds: **the signal works better on larger, higher-quality
names.** But the *magnitude* of the large-cap edge over SPY is marginal, not the
dramatic edge the biased version implied.

### What the look-ahead was worth
Removing it cut the top-100 result from **$401,976 / Sharpe 0.92** (current-cap
ranking) to **$278,769 / Sharpe 0.78** (point-in-time). That ~$123k and 0.14 of
Sharpe *was* the bias. The 50 hand-picked survivors (Sharpe 0.86) still beat every
point-in-time variant — expected, since that list is itself curated.

### Signal density — small universes are not too thin
5.9–7.9 trades/yr for the top-N variants (vs 9.6 full, 4.7 survivors). Thinness is
not the binding constraint.

## Honest bottom line
With both layers of survivorship bias removed (point-in-time membership **and**
point-in-time size ranking), the signal does **not** have a robust edge over SPY:
the best case (top-100) clears SPY's Sharpe by 0.02, every broader universe
trails it, and the full S&P 500 is sharply negative. The earlier "+22% CAGR,
Sharpe 0.92" for top-100 was mostly look-ahead.

Reproduce: `python scripts/run_universe_size_sweep.py`
