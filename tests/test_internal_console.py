"""The internal console must be unreachable without its token.

Two things are being pinned here, and the second is the one that bites: the
token check itself, and the fact that the page does NOT live under the public
StaticFiles mount. `product/web/` is served wholesale at `/`, so a console kept
there would be fetchable by path whatever the route decided — the guard would
look right in code review and protect nothing.

Driven through the ASGI app directly: starlette's TestClient needs httpx, which
this environment does not carry.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TOKEN = "test-token-value"


@pytest.fixture(scope="module")
def app():
    """The API with a console token configured, as a deployed service has.

    MonkeyPatch.context() rather than the monkeypatch fixture: that one is
    function-scoped and this has to hold for the module, since the token is
    read into module state at import and every test here shares one reload.
    """
    import product.api.main as main
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("INTERNAL_CONSOLE_TOKEN", _TOKEN)
        mp.delenv("RENDER", raising=False)  # local: cookie must not be Secure-only
        importlib.reload(main)
        yield main.app
    # The env is restored on exit; reload so the module agrees with it again.
    importlib.reload(main)


def _get(app, path: str, cookie: str | None = None) -> dict:
    """GET `path` through the ASGI app; returns status, body size and headers."""
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    headers = [(b"cookie", cookie.encode())] if cookie else []
    scope = {
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": query.encode(), "headers": headers, "client": ("1.2.3.4", 1),
        "server": ("testserver", 80), "scheme": "http", "root_path": "", "app": app,
    }
    out: dict = {"size": 0, "headers": {}}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            for key, value in message["headers"]:
                out["headers"].setdefault(key.decode().lower(), value.decode())
        elif message["type"] == "http.response.body":
            out["size"] += len(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return out


@pytest.mark.parametrize("path,cookie", [
    ("/internal/", None),                               # nothing supplied
    ("/internal/?k=wrong-token", None),                 # wrong token
    ("/internal/", "internal_console=wrong-token"),     # wrong cookie
])
def test_console_is_404_without_the_token(app, path, cookie):
    """404 rather than 401/403: an unauthenticated caller should not learn that
    this path exists or that a token would open it."""
    assert _get(app, path, cookie)["status"] == 404


def test_the_token_is_exchanged_for_a_cookie_and_redirected_away(app):
    """?k= hands back a cookie and REDIRECTS rather than serving the page.

    Serving it directly would leave ?k=<token> as the document URL: in the
    address bar, in history, in whatever gets copied and pasted, and — since a
    browser sends the full URL as Referer on same-origin requests — in the
    access log on every one of the console's 60-second polls.
    """
    r = _get(app, f"/internal/?k={_TOKEN}")
    assert r["status"] == 303
    assert r["headers"].get("location") == "/internal"
    cookie = r["headers"].get("set-cookie", "")
    assert "internal_console=" in cookie
    assert "HttpOnly" in cookie, "the token cookie must not be readable from JS"
    assert "no-store" in r["headers"].get("cache-control", ""), \
        "a response handed out against a token must not sit in a shared cache"
    assert r["headers"].get("referrer-policy") == "no-referrer"


def test_the_redirect_target_serves_the_page_with_that_cookie(app):
    """The exchange is only useful if the cookie it sets opens the page."""
    r = _get(app, "/internal", f"internal_console={_TOKEN}")
    assert r["status"] == 200
    assert r["size"] > 1000, "expected the console HTML, not an empty body"
    assert "no-store" in r["headers"].get("cache-control", "")
    assert r["headers"].get("referrer-policy") == "no-referrer"


def test_a_wrong_token_is_not_redirected_either(app):
    """404 before the cookie is set — a redirect for a bad token would confirm
    the path exists, which is the thing the 404 is there to hide."""
    assert _get(app, "/internal/?k=wrong-token")["status"] == 404


def test_the_cookie_alone_opens_the_console(app):
    assert _get(app, "/internal/", f"internal_console={_TOKEN}")["status"] == 200


def test_console_is_absent_when_no_token_is_configured(monkeypatch):
    """Fail CLOSED. A deploy that forgets INTERNAL_CONSOLE_TOKEN must expose
    nothing — the opposite default would publish the console silently."""
    monkeypatch.delenv("INTERNAL_CONSOLE_TOKEN", raising=False)
    import product.api.main as main
    importlib.reload(main)
    try:
        assert _get(main.app, f"/internal/?k={_TOKEN}")["status"] == 404
        assert _get(main.app, "/internal/")["status"] == 404
    finally:
        # Undo first so the reload picks the token back up; the module caches
        # it at import, so restoring the env alone would leave the app gated
        # off for every test that runs after this one.
        monkeypatch.undo()
        importlib.reload(main)


@pytest.mark.parametrize("path", [
    "/internal/index.html",             # the path it had while under web/
    "/internal_console/index.html",     # its directory name, if it were served
])
def test_the_console_is_not_reachable_as_a_static_file(app, path):
    """The whole point of keeping it outside product/web/: the mount at "/"
    serves that tree unconditionally, so a file left there would bypass the
    token check entirely."""
    assert _get(app, path)["status"] == 404


def test_the_gate_does_not_touch_the_public_surface(app):
    """The client PWA and the API it depends on must be unaffected."""
    assert _get(app, "/index.html")["status"] == 200
    assert _get(app, "/api/health")["status"] == 200


# ── research endpoints ────────────────────────────────────────────────────────
# The console's research panel reads the committed study reports. Those are the
# numbers behind the policy, so they sit behind the same token as the page that
# shows them — and the page's own fetches are same-origin, so the cookie it was
# handed carries them without any change to how they are written.

@pytest.mark.parametrize("path", ["/api/research", "/api/research/threshold_sweep"])
def test_research_is_404_without_the_token(app, path):
    """404 rather than 401/403, matching the console page itself."""
    assert _get(app, path)["status"] == 404
    assert _get(app, path, "internal_console=wrong-token")["status"] == 404


@pytest.mark.parametrize("path", ["/api/research", "/api/research/threshold_sweep"])
def test_the_console_cookie_opens_the_research_endpoints(app, path):
    r = _get(app, path, f"internal_console={_TOKEN}")
    assert r["status"] == 200
    assert r["size"] > 100


def test_an_unknown_report_is_404_even_with_the_token(app):
    assert _get(app, "/api/research/no_such_report",
                f"internal_console={_TOKEN}")["status"] == 404


def test_research_fails_closed_when_no_token_is_configured(monkeypatch):
    """A deploy that forgets INTERNAL_CONSOLE_TOKEN must not publish the
    studies, the same way it must not publish the console."""
    monkeypatch.delenv("INTERNAL_CONSOLE_TOKEN", raising=False)
    import product.api.main as main
    importlib.reload(main)
    try:
        assert _get(main.app, "/api/research")["status"] == 404
        assert _get(main.app, "/api/research",
                    f"internal_console={_TOKEN}")["status"] == 404
    finally:
        monkeypatch.undo()
        importlib.reload(main)


def test_gating_research_leaves_the_public_api_alone(app):
    """Only the research routes moved behind the token."""
    assert _get(app, "/api/health")["status"] == 200
    assert _get(app, "/api/positions")["status"] == 200
