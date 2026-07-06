"""EODHD fundamentals adapter — the quality-gate fallback, replacing yfinance.

EDGAR stays the PRIMARY fundamentals source; this is the fallback used when EDGAR
lacks a company's data. It replaces ``PointInTimeFundamentals`` (yfinance), which
cannot complete a TLS handshake through the proxy on Render and there hangs the
backtest until the request times out. EODHD is reached with plain ``requests``
(honours the proxy CA bundle), covers delisted tickers, and — unlike yfinance —
carries a real ``filing_date`` per period, so point-in-time selection uses the
actual public-knowledge date instead of an assumed publication lag.

Same interface as ``PointInTimeFundamentals`` (``get_snapshot`` returning a
``FundamentalSnapshot``) so it drops into ``EdgarFundamentals(fallback=…)`` with
nothing downstream changing.

Requires the EODHD **Fundamentals** feed on the account behind ``EODHD_API_KEY``.
If the key lacks that entitlement the endpoint returns HTTP 403 — this adapter
logs it once and returns ``None`` (fail-closed), so the gate simply skips that
ticker. It never hangs, and coverage turns on automatically once the feed is
enabled (no code change).

Endpoint: GET https://eodhd.com/api/fundamentals/{SYMBOL}.US?api_token=…&fmt=json
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

import requests

from core.data.eodhd import normalize_ticker
from core.data.fundamentals import FundamentalSnapshot

log = logging.getLogger(__name__)

_BASE_URL = "https://eodhd.com/api/fundamentals"
_TIMEOUT = 40
_ENV_KEY = "EODHD_API_KEY"
_DEFAULT_CACHE = Path(__file__).parent.parent.parent / "data" / "cache" / "eodhd_fundamentals"

_forbidden_logged = False


def _num(v) -> float | None:
    """EODHD numbers arrive as strings like '112010000000.00' (or None)."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if f != f else f  # drop NaN
    except (TypeError, ValueError):
        return None


class EODHDFundamentals:
    """Point-in-time fundamentals from EODHD, cached per ticker on disk.

    The on-disk cache stores the PROCESSED annual snapshots (same shape as the
    PointInTimeFundamentals cache) so a prebuilt cache can ship them and a cold
    deploy never makes a live call.
    """

    def __init__(self, cache_dir: Path = _DEFAULT_CACHE):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._mem: dict[str, list[dict] | None] = {}  # per-process, incl. negative

    # ── network ──────────────────────────────────────────────────────────
    def _fetch(self, ticker: str) -> dict | None:
        global _forbidden_logged
        key = os.environ.get(_ENV_KEY, "").strip()
        if not key:
            log.warning("EODHD fundamentals: %s not set; cannot fetch %s", _ENV_KEY, ticker)
            return None
        symbol = normalize_ticker(ticker)
        try:
            resp = requests.get(f"{_BASE_URL}/{symbol}",
                                params={"api_token": key, "fmt": "json"}, timeout=_TIMEOUT)
        except Exception as exc:
            log.warning("EODHD fundamentals: request failed for %s (%s): %r", ticker, symbol, exc)
            return None
        if resp.status_code == 403:
            if not _forbidden_logged:
                _forbidden_logged = True
                log.warning("EODHD fundamentals: HTTP 403 (the account behind %s lacks the "
                            "Fundamentals feed) — gate falls back to fail-closed. Enable the "
                            "feed to activate coverage.", _ENV_KEY)
            return None
        if resp.status_code != 200:
            log.warning("EODHD fundamentals: HTTP %s for %s (%s)", resp.status_code, ticker, symbol)
            return None
        try:
            return resp.json()
        except Exception:
            return None

    # ── processing ───────────────────────────────────────────────────────
    @staticmethod
    def _process(raw: dict) -> list[dict]:
        """Build the annual snapshot list (ascending by period end) from the raw
        EODHD payload: revenue growth, D/E, ROE, net margin per fiscal year.
        """
        fin = (raw or {}).get("Financials", {}) or {}
        income = (fin.get("Income_Statement", {}) or {}).get("yearly", {}) or {}
        balance = (fin.get("Balance_Sheet", {}) or {}).get("yearly", {}) or {}
        if not income:
            return []

        periods = []
        for key, inc in income.items():
            bal = balance.get(key, {}) or {}
            end = inc.get("date") or key
            filed = inc.get("filing_date") or bal.get("filing_date")
            revenue = _num(inc.get("totalRevenue"))
            if revenue is None:
                continue  # no revenue → nothing computable
            periods.append({
                "statement_date": end,
                "filed_date": filed,
                "revenue": revenue,
                "net_income": _num(inc.get("netIncome")),
                # longTermDebt preferred; fall back to the "…Total" variant.
                "lt_debt": _num(bal.get("longTermDebt")) if bal.get("longTermDebt") is not None
                           else _num(bal.get("longTermDebtTotal")),
                "equity": _num(bal.get("totalStockholderEquity")),
            })
        periods.sort(key=lambda p: p["statement_date"])

        snaps: list[dict] = []
        for i, p in enumerate(periods):
            rev, ni, eq, ltd = p["revenue"], p["net_income"], p["equity"], p["lt_debt"]
            prior = periods[i - 1]["revenue"] if i > 0 else None
            growth = ((rev - prior) / abs(prior)) if (prior is not None and prior > 0) else None
            # D/E fail-closed: equity ≤ 0 → unbounded → None; missing LT-debt → None.
            if eq is None or eq <= 0:
                de = None
            elif ltd is None:
                de = None
            else:
                de = ltd / eq
            roe = (ni / eq) if (ni is not None and eq and eq > 0) else None
            net_margin = (ni / rev) if (ni is not None and rev) else None
            snaps.append({
                "statement_date": p["statement_date"],
                "filed_date": p["filed_date"],
                "revenue_growth_yoy": growth,
                "debt_to_equity": de,
                "roe": roe,
                "net_margin": net_margin,
            })
        return snaps

    def _cache_path(self, ticker: str) -> Path:
        safe = "".join(c for c in ticker if c.isalnum() or c in "-_")
        return self.cache_dir / f"{safe}.json"

    def _load_snapshots(self, ticker: str) -> list[dict] | None:
        # Per-process memo (incl. None) so a missing/403 ticker is fetched at most
        # once — the gate asks for the same ticker on many entry dates.
        if ticker in self._mem:
            return self._mem[ticker]
        path = self._cache_path(ticker)
        if path.exists():
            try:
                with open(path) as f:
                    snaps = json.load(f)
                    self._mem[ticker] = snaps
                    return snaps
            except Exception:
                pass
        raw = self._fetch(ticker)
        if raw is None:
            self._mem[ticker] = None
            return None
        snaps = self._process(raw)
        try:
            with open(path, "w") as f:
                json.dump(snaps, f)
        except Exception:
            pass
        self._mem[ticker] = snaps
        return snaps

    # ── public API (matches PointInTimeFundamentals) ─────────────────────
    def get_snapshot(self, ticker: str, as_of_date: date | str) -> FundamentalSnapshot | None:
        if isinstance(as_of_date, str):
            as_of_date = date.fromisoformat(as_of_date)
        try:
            snaps = self._load_snapshots(ticker)
            if not snaps:
                return None
            # Point-in-time: only periods already FILED on/before as_of_date
            # (EODHD's real filing_date — no assumed lag, no look-ahead).
            eligible = [s for s in snaps
                        if s.get("filed_date")
                        and date.fromisoformat(s["filed_date"]) <= as_of_date]
            if not eligible:
                return None
            best = max(eligible, key=lambda s: s["filed_date"])
            return FundamentalSnapshot(
                statement_date=date.fromisoformat(best["statement_date"]),
                revenue_growth_yoy=best["revenue_growth_yoy"],
                debt_to_equity=best["debt_to_equity"],
                roe=best["roe"],
                net_margin=best["net_margin"],
                filed_date=date.fromisoformat(best["filed_date"]) if best.get("filed_date") else None,
            )
        except Exception:
            return None
