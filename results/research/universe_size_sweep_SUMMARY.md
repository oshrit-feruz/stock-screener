# Universe-size sweep — final summary

**PR #12** — https://github.com/oshrit-feruz/stock-screener/pull/12
(branch `claude/sp500-pit-universe`, base `main`) — both pieces verified.

## What was built
- Extended `data/sp500_universe.py` with **`get_universe_top_n(date, n)`** — ranks a
  date's S&P 500 members by market cap, cached under `data/cache/market_cap/`
  (30-day TTL).
- `scripts/run_universe_size_sweep.py` — runs V1 across full-PIT / top-100 /
  top-150 / top-200, plus the 50 survivors and SPY, from one data load.

## Results (2018–2024, V1 10%/max10/252d, $100k start)

| Variant | Final $ | CAGR | Sharpe | Max DD | Trades | Win % | Sig/yr |
|---|--:|--:|--:|--:|--:|--:|--:|
| Full S&P 500 (PIT) | $88,830 | −1.7% | 0.09 | −54.8% | 67 | 45% | 9.6 |
| **Top 100 by mcap** | **$401,976** | **+22.0%** | **0.92** | −31.5% | 43 | 74% | 6.1 |
| Top 150 by mcap | $332,135 | +18.7% | 0.79 | −33.4% | 47 | 66% | 6.7 |
| Top 200 by mcap | $289,442 | +16.4% | 0.70 | −42.8% | 54 | 63% | 7.7 |
| Original 50 survivors | $294,709 | +16.7% | 0.86 | −32.2% | 33 | 70% | 4.7 |
| SPY buy & hold | $245,098 | +13.7% | 0.76 | −33.7% | — | — | — |

## Key question — answered, with a caveat
On the raw numbers, **yes**: Top-100 (Sharpe 0.92) and Top-150 (0.79) beat SPY
(0.76), and Sharpe falls cleanly monotonically with size
(0.92 → 0.79 → 0.70 → 0.09). The *direction* — the signal works far better on
large, high-quality names — is credible.

**But the level is not trustworthy.** The top-N ranking uses **current** market
cap, so it floats 2024's winners to the top of 2018 (NVDA ranks #1 in Jan-2018)
and drops delisted names — reintroducing look-ahead/survivorship bias. The tell:
Top-100 out-earns even the 50 cherry-picked survivors. A clean test needs
point-in-time market cap (price × EDGAR shares). Signal density (6–8 trades/yr)
confirms small universes are not too thin.

## Honesty notes
- Two data sources were blocked mid-task: yfinance (curl_cffi can't traverse the
  agent proxy) and FMP (constituent endpoints paywalled). Market cap is read from
  the same `marketCap` field via a `requests`-based path, documented in the module
  header.
- Verification gate met: all four variants ran without errors (exit 0).
- Artifacts: chart `results/universe_size_sweep.png`, writeup
  `results/universe_size_sweep.md`.

## Suggested follow-up
Add a **point-in-time** market-cap ranking (price × EDGAR shares outstanding) for
an unbiased read on the large-cap edge.
