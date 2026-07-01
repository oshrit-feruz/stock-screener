# 8-K veto validation

```

============================================================================================
8-K VETO VALIDATION — best config (Top-100 PIT, thr0.60, flat 10%, money market, T+1)
2018-2024, $100k, 252d hold — research-only
============================================================================================

Veto layer is LIVE: 13 veto-eligible distress 8-Ks exist in the
  universe over the window, across 12 tickers (these are what the veto scans).
  Sample distress 8-Ks on file (date, ticker, items):
    2017-11-06  KHC    items=['4.02']
    2018-07-26  APD    items=['4.01']
    2018-12-19  DD     items=['4.01']
    2019-05-06  KHC    items=['4.02']
    2020-02-26  CCI    items=['4.02']
    2020-03-03  CDNS   items=['4.01']
    2020-06-22  GE     items=['4.01']
    2022-04-15  TMUS   items=['4.01']
    2022-06-24  CCL    items=['4.01']
    2024-01-19  MSFT   items=['1.05']
    2024-02-13  PRU    items=['1.05']
    2024-02-22  UNH    items=['1.05']
    ... and 1 more

Signals blocked by the veto: 0 (baseline trades: 41, with-veto trades: 41)

BLOCKED SIGNALS (signal date, ticker, composite, counterfactual 252d ret, reason)
--------------------------------------------------------------------------------------------
  (none — no in-universe signal coincided with a distress filing in its 90-day window)

--------------------------------------------------------------------------------------------
  Metric                    BEFORE (no veto)        AFTER (veto)
--------------------------------------------------------------------------------------------
  Final value                       $289,564            $289,564
  CAGR                                +16.4%              +16.4%
  Sharpe                                0.81                0.81
  Max drawdown                        -31.3%              -31.3%
  # trades                                41                  41
  Win rate                             68.3%               68.3%
  Avg trade return                    +32.5%                    

============================================================================================
KEY QUESTION — did blocked signals have a WORSE-than-average win rate?
============================================================================================
  No signals were blocked on this universe/window, so there is nothing to
  compare. The veto is a correctness safeguard that (correctly) did not fire:
  the Top-100 large-cap universe rarely files bankruptcy / restatement / auditor-
  change 8-Ks, and delisted-after-distress names drop out of EDGAR's current
  ticker map (reported as unverifiable above), so they are never force-blocked.

NOTE: research-only. Not wired into product/ until validated.
```
