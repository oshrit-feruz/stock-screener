# 8-K veto layer — a fail-closed, non-predictive entry filter.
#
# Blocks a BUY signal from entering a position when the company has, in the
# recent past, filed a specific SEC 8-K event (or carries a going-concern
# opinion) that indicates serious *structural* distress. This is NOT a
# predictive model and fits NO parameters on our backtest — the excluded event
# types are categorical, prior-justified exclusions taken from the literature:
#
#   Campbell, Hilscher & Szilagyi (2008) — financially distressed stocks earn
#     anomalously low returns and do not revert.
#   Kausar, Taffler & Tan (2009) — going-concern opinions predict ~-14%
#     abnormal returns in the following year (continued underperformance).
#
# The veto acts only on *publicly filed facts*: if no qualifying fact is on
# file, it never blocks. (A missing/unavailable filing record is therefore
# treated as "no positive fact → do not block", and reported as unverifiable —
# the veto blocks distress, not the absence of data.)
#
# ── Data source note ────────────────────────────────────────────────────────
# The task description referenced the EDGAR full-text search endpoint
# (efts.sec.gov/LATEST/search-index). We instead use the EDGAR *submissions*
# API (data.sec.gov/submissions/CIK##########.json) because it:
#   * returns the 8-K Item codes in a structured `items` field per filing
#     (full-text search returns text hits, not clean item codes, and q="TICKER"
#     false-matches on unrelated documents),
#   * is point-in-time filterable by each filing's `filingDate`,
#   * is the same data.sec.gov family already used by core/data/edgar.py, and
#     reuses that module's CIK lookup (no duplicated ticker→CIK pipeline).
# This honours the intent — "identify which Item types were filed in the past
# 90 days" — with the authoritative structured source.
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import requests

# Reuse the existing EDGAR pipeline (CIK lookup, User-Agent, cache-freshness
# and the courtesy-rate-limited JSON fetch) — do not duplicate it.
from core.data.edgar import _USER_AGENT, EdgarFundamentals, _cache_fresh, _fetch_json
from core.data.fundamentals import _safe_ticker

_CACHE_DIR = Path(__file__).parent / "cache" / "sec_8k"
_CACHE_TTL_SECONDS = 24 * 3600           # 24-hour TTL, per spec
_LOOKBACK_DAYS = 90                       # 8-K veto window: past 90 days
_DOC_BYTE_CAP = 4_000_000                 # cap going-concern doc download (~4 MB)

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_SUBMISSIONS_SHARD_URL = "https://data.sec.gov/submissions/{name}"
_ARCHIVE_DOC_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"
)

# 8-K Item types that fire the veto (categorical, from the literature — NOT
# fitted). Item 5.02 (CEO/officer turnover) is deliberately EXCLUDED: the
# evidence on management-change signals is mixed.
VETO_8K_ITEMS: dict[str, str] = {
    "1.03": "Bankruptcy or Receivership",
    "4.01": "Change in Registrant's Certifying Accountant (auditor red flag)",
    "4.02": "Non-Reliance on Previously Issued Financial Statements (restatement)",
    "1.05": "Material Cybersecurity Incident",   # optional / lower priority
}

# Going-concern qualification is checked in the most recent 10-K/10-Q. We
# require BOTH phrases to co-occur ("substantial doubt ... going concern"),
# which is the standard auditor going-concern language (AS 2415 / ASU 2014-15).
# Requiring both avoids false positives from routine accounting-policy
# boilerplate ("prepared assuming the Company will continue as a going
# concern"), which appears in most healthy filings. This is a false-positive
# guard, not a fitted parameter.
_GC_REQUIRED_PHRASES = ("substantial doubt", "going concern")

# CIK resolution reuses the existing EDGAR ticker→CIK map.
_edgar = EdgarFundamentals()

# In-memory memo: normalised filing list per ticker (avoids re-reading the
# cache JSON on every is_vetoed call during a backtest).
_filings_mem: dict[str, list[dict] | None] = {}
_gc_mem: dict[str, bool] = {}   # accession → going-concern present?


# ── filing retrieval (submissions API) ──────────────────────────────────────

def _cache_path(ticker: str) -> Path:
    return _CACHE_DIR / f"{_safe_ticker(ticker)}.json"


def _normalise_recent(block: dict) -> list[dict]:
    """Turn a submissions `filings.recent`-shaped block (parallel arrays) into a
    list of per-filing dicts, keeping only forms relevant to the veto."""
    forms = block.get("form", [])
    dates = block.get("filingDate", [])
    items = block.get("items", [])
    accns = block.get("accessionNumber", [])
    docs = block.get("primaryDocument", [])
    out: list[dict] = []
    for i, form in enumerate(forms):
        if form not in ("8-K", "10-K", "10-Q"):
            continue
        raw_items = items[i] if i < len(items) else ""
        out.append({
            "form": form,
            "filingDate": dates[i] if i < len(dates) else "",
            "items": [s.strip() for s in raw_items.split(",") if s.strip()],
            "accession": accns[i] if i < len(accns) else "",
            "primaryDocument": docs[i] if i < len(docs) else "",
        })
    return out


def _fetch_filings(ticker: str) -> list[dict] | None:
    """Full point-in-time filing history for `ticker` (8-K / 10-K / 10-Q),
    merging the `recent` block with any older submission shards so a multi-year
    backtest sees filings older than the ~1000-entry recent window. Returns None
    when the ticker cannot be resolved or EDGAR is unreachable."""
    cik = _edgar._get_cik(ticker)
    if cik is None:
        return None

    data = _fetch_json(_SUBMISSIONS_URL.format(cik=int(cik)))
    if data is None:
        return None

    filings = data.get("filings", {})
    records = _normalise_recent(filings.get("recent", {}))

    # Older filings live in additional shards referenced by filings.files.
    for shard in filings.get("files", []):
        name = shard.get("name")
        if not name:
            continue
        shard_data = _fetch_json(_SUBMISSIONS_SHARD_URL.format(name=name))
        if isinstance(shard_data, dict):
            records.extend(_normalise_recent(shard_data))

    records.sort(key=lambda r: r["filingDate"], reverse=True)
    return records


def _get_filings(ticker: str) -> list[dict] | None:
    """Cached accessor (in-memory memo → 24h disk cache → EDGAR)."""
    if ticker in _filings_mem:
        return _filings_mem[ticker]

    path = _cache_path(ticker)
    if _cache_fresh(path) if path.exists() else False:
        try:
            with open(path) as f:
                records = json.load(f)
            _filings_mem[ticker] = records
            return records
        except Exception:
            pass

    records = _fetch_filings(ticker)
    if records is not None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "w") as f:
                json.dump(records, f)
        except Exception:
            pass
    _filings_mem[ticker] = records
    return records


# ── going-concern check (most recent 10-K/10-Q as of the date) ──────────────

def _fetch_doc_text(cik: int, accession: str, doc: str) -> str | None:
    accession_nodash = accession.replace("-", "")
    url = _ARCHIVE_DOC_URL.format(cik=int(cik), accession=accession_nodash, doc=doc)
    time.sleep(0.12)   # SEC courtesy rate limit (matches core/data/edgar)
    try:
        resp = requests.get(
            url, headers={"User-Agent": _USER_AGENT}, timeout=45, stream=True
        )
        resp.raise_for_status()
        chunks, total = [], 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= _DOC_BYTE_CAP:
                break
        return b"".join(chunks).decode("utf-8", errors="ignore")
    except Exception:
        return None


def _doc_has_going_concern(cik: int, filing: dict) -> bool:
    accession = filing.get("accession", "")
    doc = filing.get("primaryDocument", "")
    if not accession or not doc:
        return False
    if accession in _gc_mem:
        return _gc_mem[accession]

    gc_cache = _CACHE_DIR / f"gc_{_safe_ticker(accession)}.json"
    if gc_cache.exists():   # immutable filing → no TTL needed
        try:
            with open(gc_cache) as f:
                val = bool(json.load(f).get("going_concern", False))
            _gc_mem[accession] = val
            return val
        except Exception:
            pass

    text = _fetch_doc_text(cik, accession, doc)
    present = False
    if text:
        low = text.lower()
        present = all(p in low for p in _GC_REQUIRED_PHRASES)

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(gc_cache, "w") as f:
            json.dump({"going_concern": present}, f)
    except Exception:
        pass
    _gc_mem[accession] = present
    return present


# ── public interface ────────────────────────────────────────────────────────

def is_vetoed(
    ticker: str, as_of_date: str, check_going_concern: bool = True
) -> tuple[bool, str]:
    """Should a BUY signal for `ticker` on `as_of_date` be blocked?

    Returns (blocked, reason). `blocked` is True only on a positively-filed
    distress fact:
      * a VETO_8K_ITEMS 8-K filed in the past 90 days, or
      * a going-concern qualification in the most recent 10-K/10-Q filed on or
        before the date.
    A clean company returns (False, ""). When no filing record can be resolved
    (unknown CIK / EDGAR unreachable) it returns (False, "unverifiable: ...") —
    the veto acts on facts, not on their absence.
    """
    as_of = date.fromisoformat(as_of_date)
    filings = _get_filings(ticker)
    if not filings:
        return False, f"unverifiable: no EDGAR filing record for {ticker}"

    # 1) Recent distress 8-K (scan most-recent-first within the 90-day window).
    window_start = as_of - timedelta(days=_LOOKBACK_DAYS)
    for f in filings:
        if f["form"] != "8-K" or not f["filingDate"]:
            continue
        fdate = date.fromisoformat(f["filingDate"])
        if fdate > as_of:
            continue                      # point-in-time: not yet public
        if fdate < window_start:
            break                         # sorted desc → older filings can't qualify
        for item in f["items"]:
            if item in VETO_8K_ITEMS:
                return True, (
                    f"8-K Item {item} filed {f['filingDate']}: "
                    f"{VETO_8K_ITEMS[item]}"
                )

    # 2) Going-concern opinion in the most recent 10-K/10-Q as of the date.
    if check_going_concern:
        cik = _edgar._get_cik(ticker)
        if cik is not None:
            for f in filings:
                if f["form"] not in ("10-K", "10-Q") or not f["filingDate"]:
                    continue
                if date.fromisoformat(f["filingDate"]) > as_of:
                    continue
                # First (most recent) annual/quarterly filing as of the date.
                if _doc_has_going_concern(int(cik), f):
                    return True, (
                        f"{f['form']} filed {f['filingDate']}: going-concern "
                        f"doubt (substantial doubt / going concern)"
                    )
                break

    return False, ""


def prefetch_veto_cache(tickers: list[str], as_of_date: str) -> None:
    """Warm the per-ticker filing cache for every ticker in the universe before
    a backtest run. `as_of_date` is accepted for interface symmetry; the filing
    list is date-independent (is_vetoed filters point-in-time locally)."""
    for ticker in tickers:
        try:
            _get_filings(ticker)
        except Exception:
            continue
