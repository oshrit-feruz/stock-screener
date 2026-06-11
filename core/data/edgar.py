import json
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from core.data.fundamentals import FundamentalSnapshot, PointInTimeFundamentals, _safe_ticker

_DEFAULT_EDGAR_CACHE = Path(__file__).parent.parent.parent / "data" / "cache" / "edgar"
_PUBLICATION_LAG_DAYS = 90
_CACHE_TTL_SECONDS = 7 * 86400

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL   = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_USER_AGENT  = "StockAdvisor research@stockadvisor.com"

_REVENUE_CONCEPTS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
]
_NET_INCOME_CONCEPTS = ["NetIncomeLoss"]
_EQUITY_CONCEPTS = [
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
]
_LT_DEBT_CONCEPTS = ["LongTermDebt", "LongTermDebtNoncurrent"]


def _cache_fresh(path: Path) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < _CACHE_TTL_SECONDS


def _fetch_json(url: str) -> dict | None:
    time.sleep(0.12)
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _annual_entries(facts: dict, concept: str, cutoff: date | None = None) -> list[dict]:
    """Return 10-K / FY entries, deduplicated by period-end date (latest filed per end wins).

    Dedup key is the `end` date (fiscal period end) rather than `fy`.  The `fy`
    field is used inconsistently across companies: income-statement items often set
    fy = the period year, while balance-sheet items may set fy = the filing year —
    causing multiple period-end dates to share the same fy value.

    When cutoff is supplied, entries filed after the cutoff are excluded BEFORE
    deduplication so that a later comparative filing cannot displace the original.
    """
    try:
        raw = facts["facts"]["us-gaap"][concept]["units"]["USD"]
    except (KeyError, TypeError):
        return []

    by_end: dict[str, dict] = {}
    for entry in raw:
        if entry.get("form") == "10-K" and entry.get("fp") == "FY":
            end = entry.get("end")
            if not end:
                continue
            if cutoff is not None and date.fromisoformat(entry["filed"]) > cutoff:
                continue
            if end not in by_end or entry["filed"] > by_end[end]["filed"]:
                by_end[end] = entry

    return sorted(by_end.values(), key=lambda e: e["end"], reverse=True)


def _best_entry(entries: list[dict], cutoff: date) -> dict | None:
    """Most recent entry whose filed date is on or before cutoff."""
    for entry in entries:
        if date.fromisoformat(entry["filed"]) <= cutoff:
            return entry
    return None


def _first_concept(facts: dict, concepts: list[str], cutoff: date) -> tuple[str, dict] | tuple[None, None]:
    """Return (concept, entry) for the most recent eligible entry across all concepts.

    Companies change XBRL tags over time (e.g. AAPL switched from Revenues to
    RevenueFromContractWithCustomerExcludingAssessedTax in FY2019). Returning the
    most recent entry — not the first concept with any entry — ensures we always
    use the current-period filing instead of a stale prior-concept entry.
    Passes cutoff to _annual_entries so dedup uses only filings available by cutoff.
    """
    best_concept: str | None = None
    best_entry_val: dict | None = None
    for concept in concepts:
        entries = _annual_entries(facts, concept, cutoff=cutoff)
        entry = _best_entry(entries, cutoff)
        if entry is None:
            continue
        if best_entry_val is None or entry["end"] > best_entry_val["end"]:
            best_concept = concept
            best_entry_val = entry
    return best_concept, best_entry_val


class EdgarFundamentals:
    def __init__(
        self,
        cache_dir: Path = _DEFAULT_EDGAR_CACHE,
        fallback: PointInTimeFundamentals | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._fallback = fallback

    # ── CIK lookup ────────────────────────────────────────────────────────

    def _tickers_cache_path(self) -> Path:
        return self.cache_dir / "tickers.json"

    def _load_tickers_map(self) -> dict[str, int]:
        path = self._tickers_cache_path()
        if _cache_fresh(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass

        raw = _fetch_json(_TICKERS_URL)
        if raw is None:
            return {}

        mapping = {v["ticker"].upper(): v["cik_str"] for v in raw.values()}
        try:
            with open(path, "w") as f:
                json.dump(mapping, f)
        except Exception:
            pass
        return mapping

    def _get_cik(self, ticker: str) -> int | None:
        return self._load_tickers_map().get(ticker.upper())

    # ── companyfacts ──────────────────────────────────────────────────────

    def _facts_cache_path(self, ticker: str) -> Path:
        return self.cache_dir / f"{_safe_ticker(ticker)}.json"

    def _get_facts(self, ticker: str) -> dict | None:
        path = self._facts_cache_path(ticker)
        if _cache_fresh(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass

        cik = self._get_cik(ticker)
        if cik is None:
            return None

        data = _fetch_json(_FACTS_URL.format(cik=int(cik)))
        if data is None:
            return None

        try:
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception:
            pass
        return data

    # ── public interface ──────────────────────────────────────────────────

    def get_snapshot(self, ticker: str, as_of_date: date | str) -> FundamentalSnapshot | None:
        if isinstance(as_of_date, str):
            as_of_date = date.fromisoformat(as_of_date)

        cutoff = as_of_date - timedelta(days=_PUBLICATION_LAG_DAYS)

        try:
            facts = self._get_facts(ticker)
            if not facts:
                return self._do_fallback(ticker, as_of_date)

            rev_concept, rev_entry = _first_concept(facts, _REVENUE_CONCEPTS, cutoff)
            if rev_entry is None:
                return self._do_fallback(ticker, as_of_date)

            # Revenue growth: current FY vs entry whose period-end is ~1 year earlier
            all_rev = _annual_entries(facts, rev_concept, cutoff=cutoff)
            current_end = date.fromisoformat(rev_entry["end"])
            prior_rev_entry = next(
                (e for e in all_rev
                 if 300 <= (current_end - date.fromisoformat(e["end"])).days <= 400),
                None,
            )
            revenue_growth = None
            if prior_rev_entry is not None and prior_rev_entry["val"] != 0:
                revenue_growth = (
                    (rev_entry["val"] - prior_rev_entry["val"]) / abs(prior_rev_entry["val"])
                )

            _, ni_entry  = _first_concept(facts, _NET_INCOME_CONCEPTS, cutoff)
            _, eq_entry  = _first_concept(facts, _EQUITY_CONCEPTS, cutoff)
            _, ltd_entry = _first_concept(facts, _LT_DEBT_CONCEPTS, cutoff)

            revenue    = rev_entry["val"]
            net_income = ni_entry["val"]  if ni_entry  else None
            equity     = eq_entry["val"]  if eq_entry  else None
            lt_debt    = ltd_entry["val"] if ltd_entry else None

            # D/E: negative/zero equity means book value wiped out → unbounded → None (fail-closed)
            if equity is None or equity <= 0:
                de_ratio = None
            elif lt_debt is None:
                de_ratio = None   # LT-debt concept not found in EDGAR → unknown → fail-closed
            else:
                de_ratio = float(lt_debt / equity)

            return FundamentalSnapshot(
                statement_date     = date.fromisoformat(rev_entry["end"]),
                revenue_growth_yoy = revenue_growth,
                debt_to_equity     = de_ratio,
                roe                = (net_income / equity) if (equity and equity > 0 and net_income is not None) else None,
                net_margin         = (net_income / revenue) if (revenue and net_income is not None) else None,
                filed_date         = date.fromisoformat(rev_entry["filed"]),
            )
        except Exception:
            return self._do_fallback(ticker, as_of_date)

    def _do_fallback(self, ticker: str, as_of_date: date) -> FundamentalSnapshot | None:
        if self._fallback is not None:
            return self._fallback.get_snapshot(ticker, as_of_date)
        return None

    def get_all_snapshots(self, ticker: str, years: list[int]) -> list[FundamentalSnapshot]:
        return [
            s for year in years
            if (s := self.get_snapshot(ticker, date(year, 12, 31))) is not None
        ]
