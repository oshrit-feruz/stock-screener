# Stock Screener — Stage 1: Data Layer

Point-in-time data foundation for a stock screening and advisory product.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run smoke test

```bash
python scripts/verify_data_layer.py
```

## Run tests

```bash
pytest tests/ -v
```

## Known limitations

### yfinance historical depth (~4 fiscal years)

yfinance returns approximately the 4 most recent fiscal years of annual statements.
Running in mid-2026, this covers roughly FY2022–FY2025 for most US companies.

**Consequence:** `revenue_growth_yoy` for the oldest year in the window will always be
`None` because the prior-year baseline is not available.  For snapshots dated
**2022-12-31 and later**, the prior year is within the window and all fields populate
correctly.  Snapshots dated **2021-12-31 or earlier** will have `revenue_growth_yoy = None`
even though the other fields (D/E, ROE, net margin) may be present.

**Validation window:** use `2022-12-31` as the earliest as-of date.

### yfinance field names (v1.x vs legacy)

yfinance ≥ 1.0 changed row labels from spaced strings (`"Total Revenue"`) to camelCase
(`"TotalRevenue"`).  The data layer tries both variants in order, so it is compatible
with all recent yfinance releases.

### Cache invalidation

The fundamentals cache is keyed per ticker (`data/cache/fundamentals/{ticker}.json`).
If you upgrade yfinance or suspect stale data, delete the relevant `.json` files and
re-run.
