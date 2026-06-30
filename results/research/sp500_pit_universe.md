# Point-in-time S&P 500 universe — survivorship-bias correction

Replaces the hardcoded 50-ticker `VALIDATION_UNIVERSE` in the portfolio backtest
with the **actual S&P 500 membership on each date**, resolved per month.

## Module: `data/sp500_universe.py`

- Source: `github.com/fja05680/sp500` historical-components CSV (each row = full
  index membership on a date, back to 1996). Downloaded once and cached under
  `data/cache/sp500_universe/`, refreshed after 7 days.
- `get_universe(date) -> list[str]`: members on `date`, using the most recent
  snapshot on or before it.
- `validate_universe(date)`: prints member count + a 10-ticker sample.
- Deliberately small surface so the FMP paid API can be dropped in later behind
  the same interface (the FMP key available during development is on a tier that
  402s on the constituent endpoints, hence the free source — marked temporary in
  a header comment).

### Validation checks (all pass)

| Date | Members | TSLA | META | Expected |
|---|--:|:--:|:--:|---|
| 2010-01-04 | 499 | ❌ absent | ❌ absent | ~500, no TSLA (joined 2020), no META (IPO 2012) |
| 2018-01-02 | 505 | ❌ absent | — | ~500, no TSLA |
| 2020-12-21 | 505 | ✅ present | — | the day TSLA joined → present |

## Integration (`run_portfolio_sim.py`)

- The universe is resolved on the **first trading day of each month** and reused
  all month (`build_monthly_universe`), not recomputed daily.
- `load_all_data` now takes the union of monthly members; EDGAR fundamentals are
  fetched only for tickers that actually produce a raw price BUY (avoids hundreds
  of needless companyfacts downloads).
- Each signal entry is gated by that month's membership, so a name can only be
  bought while it was genuinely in the index.

## Result — the bias was large

2018–2024, $100k start. **Same signal, same sizing — only the universe changed.**

| Variant | Old (50 survivors) | New (point-in-time S&P 500) |
|---|--:|--:|
| V1 (10%/max10) | $293,354 (+16.6% CAGR) | **$88,830 (−1.7%)** |
| V2 (20%/max5)  | $398,905 (+21.9%) | $96,350 (−0.5%) |
| V3 (5%/max20)  | $184,512 (+9.1%) | $122,784 (+3.0%) |
| SPY buy & hold | $245,098 (+13.7%) | $245,098 (+13.7%) |

Universe coverage: 654 distinct tickers over the window; 553 had usable price
history; 312 produced ≥1 BUY crossing (4,763 crossings pre-suppression) — vs ~50
tickers and a few hundred crossings before.

**Interpretation.** The old 50-ticker list was composed of mega-caps that
survived to 2024, so every "dip" the signal bought eventually recovered. On the
real point-in-time index — which includes the names that dipped and *didn't*
recover (and were later removed) — V1's CAGR falls from +16.6% to **−1.7%**, and
all three variants now **underperform SPY**. Most of the previously-reported edge
was survivorship bias.

### Known limitation

101 of 654 union tickers (delisted/renamed names like ATVI, ANTM, XLNX) have no
usable price history from the free Yahoo source and are skipped. These are
disproportionately the *worst* outcomes (companies removed from the index after
falling), so the corrected numbers above, while far lower than the survivor-only
backtest, are still **optimistic** — the true survivorship-free result would be
somewhat worse again. A paid data source with delisted history (or FMP's
constituent + price endpoints) would close this gap.

Reproduce: `python scripts/run_portfolio_sim.py`
(price cache built via `python scripts/_fetch_price_cache.py 2016-01-01 2024-12-31 --tickers <union_file>`).
