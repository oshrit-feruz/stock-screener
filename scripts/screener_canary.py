#!/usr/bin/env python3
"""Probe the live /api/screener and fail loudly if it is not serving what a user
would need to see today.

This is the consumer-side half of the pipeline's alerting. The producer half is
in product/screener/daily_screener.py (a degraded scan refuses to publish and
turns the daily run red). This script watches what the app actually serves —
so it also catches a broken deploy, a Render outage, a universe fingerprint
mismatch, or a daily run that simply never happened.

Run by .github/workflows/screener-canary.yml on weekdays after the daily run has
had time to publish. On failure the workflow opens a GitHub issue; on recovery
it closes it. GitHub emails about issues, which is the channel.

The freshness bar is deliberately STRICTER than the service's own: /api/screener
serves a result up to four trading days old before answering 503, so a canary
that only checked the status code would fire days after the pipeline died. On a
trading day the canary requires today's result.

    SCREENER_URL=https://stock-screener-7lvr.onrender.com python scripts/screener_canary.py
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from product.market_calendar import is_trading_day  # noqa: E402

DEFAULT_URL = "https://stock-screener-7lvr.onrender.com"
# Render's free tier spins the service down when idle and takes up to a minute
# to wake it; the first request of the day is the one that pays for it.
_TIMEOUT_SECONDS = 90
_ATTEMPTS = 2
_REPORT = Path(os.environ.get("CANARY_REPORT", "canary_report.md"))


def check(status: int, payload: Any, today: date, trading_day: bool) -> Optional[str]:
    """The failure reason, or None if the endpoint is serving what it should.

    Pure — takes the response pieces and the calendar answer — so the rules can
    be tested without a network. Order matters: each check assumes the earlier
    ones passed, so the reason names the FIRST thing wrong, not a consequence
    of it.
    """
    if status != 200:
        detail = ""
        if isinstance(payload, dict) and payload.get("detail"):
            detail = f" — {str(payload['detail'])[:300]}"
        return f"HTTP {status}{detail}"
    if not isinstance(payload, dict):
        return "200 with a non-object body"
    ranking = payload.get("full_ranking")
    if not isinstance(ranking, list) or not ranking:
        return "200 with an empty ranking — a scan that scored nothing was served as a quiet day"
    computed_on = payload.get("computed_on")
    if trading_day and computed_on != today.isoformat():
        return (
            f"stale on a trading day: computed_on={computed_on!r}, expected {today.isoformat()}. "
            f"The daily run did not publish today's result (or the service could not fetch it)."
        )
    return None


def _fetch(url: str) -> tuple[int, Any, str]:
    """(status, parsed body or None, raw text excerpt). Retries once on transport errors."""
    last: Optional[Exception] = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            resp = requests.get(url, timeout=_TIMEOUT_SECONDS)
            try:
                body = resp.json()
            except ValueError:
                body = None
            return resp.status_code, body, resp.text[:500]
        except requests.RequestException as exc:
            last = exc
            if attempt < _ATTEMPTS:
                time.sleep(15)
    return 0, None, f"transport error after {_ATTEMPTS} attempts: {last!r}"


def _write_report(url: str, reason: str, status: int, excerpt: str, today: date,
                  trading_day: bool) -> None:
    """A Markdown body for the GitHub issue, written to a file so the workflow
    can pass it to `gh issue create --body-file` without shell-quoting a
    multi-line string."""
    day_kind = "a trading day" if trading_day else "not a trading day"
    _REPORT.write_text(
        f"**`/api/screener` is not serving usable results.**\n\n"
        f"- **When:** {today.isoformat()} ({day_kind}), checked at "
        f"{time.strftime('%H:%M UTC', time.gmtime())}\n"
        f"- **Probe:** `{url}`\n"
        f"- **Reason:** {reason}\n"
        f"- **HTTP status:** {status or 'no response'}\n\n"
        f"<details><summary>Response excerpt</summary>\n\n```\n{excerpt}\n```\n</details>\n\n"
        f"**Where to look:** the [daily screener runs]"
        f"(../actions/workflows/daily-screener.yml) — a red run means the producer "
        f"refused to publish (the log names why); a green run with this issue open "
        f"means the result was published but the service is not serving it.\n\n"
        f"This issue is managed by the `screener-canary` workflow: it comments on "
        f"each failing check and closes the issue automatically on recovery.\n"
    )


def main() -> int:
    base = os.environ.get("SCREENER_URL", DEFAULT_URL).strip().rstrip("/")
    url = f"{base}/api/screener"
    today = date.today()
    trading_day = is_trading_day(today)

    status, payload, excerpt = _fetch(url)
    reason = check(status, payload, today, trading_day)

    if reason is None:
        computed_on = payload.get("computed_on") if isinstance(payload, dict) else None
        n = len(payload["full_ranking"])
        print(f"OK — {url} served {n} rows computed_on={computed_on} "
              f"({'trading day' if trading_day else 'market closed'})")
        return 0

    print(f"CANARY FAILED — {reason}", file=sys.stderr)
    _write_report(url, reason, status, excerpt, today, trading_day)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"reason={reason.splitlines()[0][:200]}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
