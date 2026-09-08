"""The mutating endpoints must refuse an unauthenticated caller.

/api/positions/open, /api/positions/close and POST /api/portfolio rewrite the
tracked book. They were reachable by anyone on the internet, so these tests pin
both halves of the fix: writes require the token, and reads are untouched —
the PWA and the shift-app client depend on the read endpoints and must keep
working exactly as before.

Driven through the ASGI app directly: starlette's TestClient needs httpx, which
this environment does not carry.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TOKEN = "admin-token-for-tests"

# One representative body per endpoint; the guard runs before validation, so
# these only need to be well-formed enough to reach it.
_WRITES = [
    ("/api/positions/open", {"ticker": "AAPL", "entry_price": 100.0, "entry_date": "2026-01-02"}),
    ("/api/positions/close", {"ticker": "AAPL"}),
    ("/api/portfolio", {"holdings": []}),
]


@pytest.fixture(scope="module")
def app():
    """The API with ADMIN_TOKEN configured, as a deployed service has."""
    import product.api.main as main
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ADMIN_TOKEN", _TOKEN)
        mp.delenv("RENDER", raising=False)   # local: cookie must not be Secure-only
        importlib.reload(main)
        yield main.app
    importlib.reload(main)


def _call(app, path: str, method: str = "GET", body: dict | None = None,
          cookie: str | None = None, header_token: str | None = None) -> dict:
    """Drive one request through the ASGI app; returns status, body and headers."""
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    raw = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    if header_token:
        headers.append((b"x-admin-token", header_token.encode()))
    scope = {
        "type": "http", "method": method, "path": path, "raw_path": path.encode(),
        "query_string": query.encode(), "headers": headers, "client": ("1.2.3.4", 1),
        "server": ("testserver", 80), "scheme": "http", "root_path": "", "app": app,
    }
    out: dict = {"body": b"", "headers": {}}
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            for key, value in message["headers"]:
                out["headers"].setdefault(key.decode().lower(), value.decode())
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out


@pytest.mark.parametrize("path,body", _WRITES)
def test_writes_are_refused_without_a_token(app, path, body):
    """403, and the message says how to authenticate rather than just failing."""
    r = _call(app, path, "POST", body)
    assert r["status"] == 403
    assert "/api/auth" in r["body"].decode()


@pytest.mark.parametrize("path,body", _WRITES)
def test_writes_are_refused_with_a_wrong_token(app, path, body):
    assert _call(app, path, "POST", body, cookie="admin_session=wrong")["status"] == 403
    assert _call(app, path, "POST", body, header_token="wrong")["status"] == 403


@pytest.mark.parametrize("path,body", _WRITES)
def test_the_guard_lets_an_authenticated_write_through(app, path, body, tmp_path,
                                                       monkeypatch):
    """Past the guard the endpoint runs its own logic — it may still reject the
    body (a close with no such position, say). Only 403 would mean the token was
    not accepted, and that is what this asserts against.

    The storage paths are redirected into tmp_path first: these calls really do
    write, and data/positions/*.json and data/portfolio/portfolio.json are
    tracked in the repo — an authenticated write against the real paths would
    leave the working tree dirty and, run in CI, could be committed.
    """
    import product.api.main as main
    import product.exit.exit_tracker as tracker
    monkeypatch.setattr(main, "_OPEN_FILE", tmp_path / "open_positions.json")
    monkeypatch.setattr(main, "_CLOSED_FILE", tmp_path / "closed_positions.json")
    monkeypatch.setattr(main, "_PORTFOLIO_FILE", tmp_path / "portfolio.json")
    # /api/positions/open writes through ExitTracker, which carries its own
    # path constants — redirecting main's alone still hits the real file.
    monkeypatch.setattr(tracker, "_OPEN_FILE", tmp_path / "t_open.json")
    monkeypatch.setattr(tracker, "_CLOSED_FILE", tmp_path / "t_closed.json")

    for kwargs in ({"cookie": f"admin_session={_TOKEN}"}, {"header_token": _TOKEN}):
        assert _call(app, path, "POST", body, **kwargs)["status"] != 403


def test_auth_endpoint_exchanges_the_token_for_a_cookie(app):
    r = _call(app, "/api/auth", "POST", {"token": _TOKEN})
    assert r["status"] == 200
    cookie = r["headers"].get("set-cookie", "")
    assert "admin_session=" in cookie
    assert "HttpOnly" in cookie, "the session cookie must not be readable from JS"
    assert "strict" in cookie.lower(), \
        "SameSite=Strict is what keeps a cross-site page from POSTing as the user"


def test_auth_endpoint_rejects_a_wrong_token(app):
    r = _call(app, "/api/auth", "POST", {"token": "nope"})
    assert r["status"] == 403
    assert "set-cookie" not in r["headers"]


# The token must never travel in a URL. This one opens and closes positions, so
# a query string carrying it would sit in browser history, in anything copied
# and pasted, and in the platform access log — where reading it back out is
# enough to trade the book.

def test_the_token_is_not_accepted_in_the_query_string(app):
    """The old ?k= contract must be gone, not merely undocumented: a GET that
    carries the right token still must not hand back a session."""
    r = _call(app, f"/api/auth?k={_TOKEN}")
    assert "set-cookie" not in r["headers"], \
        "a token in the URL must not be exchanged for a session"


def test_the_sign_in_page_is_served_without_a_token(app):
    """GET is the form. It has to work unauthenticated — it is how a browser
    gets authenticated in the first place — so it must carry no credential and
    must not be cached."""
    r = _call(app, "/api/auth")
    assert r["status"] == 200
    body = r["body"].decode()
    assert "<form" in body
    assert "set-cookie" not in r["headers"]
    assert _TOKEN not in body, "the page must never contain the token itself"
    assert "no-store" in r["headers"].get("cache-control", "")


def test_the_sign_in_page_posts_the_token_in_a_body(app):
    """Pins the mechanism, not just the absence of ?k=: the page's own script
    has to send the token as a POST body for the form to work at all."""
    body = _call(app, "/api/auth")["body"].decode()
    assert "method: 'POST'" in body
    assert "JSON.stringify({ token:" in body


def test_a_get_with_no_token_configured_still_shows_the_form(monkeypatch):
    """503 belongs on the exchange, not on the form — a deploy missing
    ADMIN_TOKEN should say so when the token is submitted, not 503 a page that
    would otherwise explain nothing."""
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    import product.api.main as main
    importlib.reload(main)
    try:
        assert _call(main.app, "/api/auth")["status"] == 200
        r = _call(main.app, "/api/auth", "POST", {"token": _TOKEN})
        assert r["status"] == 503
        assert "set-cookie" not in r["headers"]
    finally:
        monkeypatch.undo()
        importlib.reload(main)


@pytest.mark.parametrize("path,body", _WRITES)
def test_writes_fail_closed_when_no_token_is_configured(monkeypatch, path, body):
    """A deploy that forgets ADMIN_TOKEN must stop writes, not leave them open.
    503 rather than 403: nothing the caller does can help."""
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    import product.api.main as main
    importlib.reload(main)
    try:
        assert _call(main.app, path, "POST", body)["status"] == 503
        assert _call(main.app, path, "POST", body,
                     cookie=f"admin_session={_TOKEN}")["status"] == 503
    finally:
        # Undo before the reload: the token is read into module state at import,
        # so restoring the env alone would leave writes disabled for later tests.
        monkeypatch.undo()
        importlib.reload(main)


@pytest.mark.parametrize("path", [
    "/api/health",
    "/api/positions",
    "/api/portfolio",          # GET stays open; only the POST is guarded
    "/api/alerts",
])
def test_reads_are_untouched(app, path):
    """The PWA and the shift-app client read these without authenticating."""
    assert _call(app, path)["status"] == 200
