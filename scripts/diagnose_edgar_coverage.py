#!/usr/bin/env python3
"""EDGAR coverage diagnostic for the point-in-time S&P 500 universe.

For each year-end 2009..2024 it takes the PIT S&P 500 membership and measures,
across those members, how much EDGAR actually covers — because the top-100
ranking depends on EDGAR shares outstanding:

  cik_ok    — ticker maps to a CIK in SEC company_tickers.json at all.
              (SEC lists only CURRENTLY-active tickers, so delisted/acquired
              names fail here → this column exposes the survivorship gap in the
              ranking step, independent of PIT membership being correct.)
  shares_ok — get_shares_outstanding() returns a value as of that date
              (captures the XBRL-depth issue: sparse before ~2010).
  gate_ok   — get_snapshot() yields a usable quality-gate verdict (rev growth,
              D/E, net margin all present).

No EODHD, no prices — pure EDGAR (free). Writes a markdown table.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.edgar import EdgarFundamentals
from core.signals.recovery_score import passes_quality_gate
from data.sp500_universe import get_universe

_OUT = Path(__file__).parent.parent / "validation" / "edgar_coverage.md"
YEARS = range(2009, 2025)


def main() -> None:
    edgar = EdgarFundamentals(fallback=None)  # pure EDGAR; no EODHD fallback

    # Per-year member lists + counters.
    dates = {y: f"{y}-12-31" for y in YEARS}
    members_by_year = {y: get_universe(dates[y]) for y in YEARS}
    counts = {y: {"n": len(members_by_year[y]), "cik": 0, "sh": 0, "gate": 0} for y in YEARS}

    # Invert the loop: fetch each unique ticker's companyfacts ONCE, evaluate it
    # for every year it is a member, then evict it from the in-memory memo so
    # only one multi-MB companyfacts JSON is resident at a time (avoids OOM).
    membership_years: dict[str, list[int]] = {}
    for y in YEARS:
        for t in members_by_year[y]:
            membership_years.setdefault(t, []).append(y)

    total = len(membership_years)
    for i, (t, yrs) in enumerate(sorted(membership_years.items()), 1):
        has_cik = edgar._get_cik(t) is not None
        for y in yrs:
            if not has_cik:
                continue
            counts[y]["cik"] += 1
            d = dates[y]
            if edgar.get_shares_outstanding(t, d) is not None:
                counts[y]["sh"] += 1
            snap = edgar.get_snapshot(t, d)
            if snap is not None and passes_quality_gate(snap) is not None:
                counts[y]["gate"] += 1
        # Free this ticker's companyfacts from the memo.
        edgar._facts_mem.pop(t, None)
        if i % 100 == 0:
            print(f"  ...{i}/{total} tickers processed")

    rows = []
    print(f"\n{'year':<6}{'members':>8}{'cik_ok':>8}{'shares':>8}{'gate':>7}  (cik% / sh% / gate%)")
    for y in YEARS:
        c = counts[y]
        n = c["n"]
        rows.append((y, n, c["cik"], c["sh"], c["gate"]))
        print(f"{y:<6}{n:>8}{c['cik']:>8}{c['sh']:>8}{c['gate']:>7}  "
              f"({c['cik']/n:.0%} / {c['sh']/n:.0%} / {c['gate']/n:.0%})")

    lines = ["# EDGAR coverage of the PIT S&P 500 membership\n",
             "How much of each year's point-in-time membership the top-100 ranking "
             "can actually see. `cik_ok` = maps to a SEC CIK at all (delisted names "
             "fail → survivorship gap in the ranking step). `shares_ok` = has "
             "shares-outstanding as of the date (XBRL depth). `gate_ok` = has a "
             "usable fundamental quality-gate verdict.\n",
             "| Year | Members | CIK ok | Shares ok | Gate ok | Shares % | Gate % |",
             "|---|--:|--:|--:|--:|--:|--:|"]
    for y, n, c, s, g in rows:
        lines.append(f"| {y} | {n} | {c} | {s} | {g} | {s/n:.0%} | {g/n:.0%} |")
    _OUT.write_text("\n".join(lines) + "\n")
    print(f"\nsaved -> {_OUT}")


if __name__ == "__main__":
    main()
