"""The release-cache fetch must not depend on api.github.com.

The live service booted cold on an HTTP 403 from the per-tag API lookup (the
unauthenticated endpoint is rate-limited per source IP, and a hosting
provider's egress IP is shared), while the asset itself was downloadable from
the release's direct URL the whole time. These tests pin the order: direct URL
first, trying GitHub's ".N" renames of a duplicate upload; the API only as a
fallback.
"""
from __future__ import annotations

import io
import json
import tarfile

import pytest

from scripts import fetch_release_cache as frc


def _tarball() -> bytes:
    """A minimal seed archive: manifest.json plus one price pickle."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        members = (("manifest.json", b'{"prices": 1}'), ("prices/AAPL_2009-01-01.pkl", b"x"))
        for name, payload in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class _Resp:
    """The slice of a requests.Response the fetcher touches."""

    def __init__(self, status: int, body: bytes = b"", payload=None):
        self.status_code = status
        self.raw = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))}
        self._payload = payload

    def json(self):
        """The API payload, when this stands in for the release lookup."""
        return self._payload

    def close(self):
        """Nothing to release."""

    def raise_for_status(self):
        """Mirror requests: raise on a 4xx/5xx."""
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def seed_dir(tmp_path, monkeypatch):
    """Point the fetcher's seed directory at a temp path, with no token."""
    monkeypatch.setattr(frc, "_SEED", tmp_path / "seed_cache")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return tmp_path / "seed_cache"


def test_candidates_try_githubs_duplicate_renames():
    """The configured name first, then GitHub's .1/.2/.3 renames; other
    suffixes get no variants."""
    assert frc._asset_candidates("seed.tar.gz") == [
        "seed.tar.gz", "seed.1.tar.gz", "seed.2.tar.gz", "seed.3.tar.gz",
    ]
    assert frc._asset_candidates("seed.zip") == ["seed.zip"]


def test_direct_url_is_used_and_the_api_is_never_called(seed_dir, monkeypatch):
    """The asset lives under its ".1" rename; the exact name 404s. The fetch
    must find it on the direct URL and never touch api.github.com."""
    calls: list[str] = []

    def fake_get(url, **kw):
        calls.append(url)
        assert "api.github.com" not in url
        if url.endswith("/seed_cache_2010_2026.1.tar.gz"):
            return _Resp(200, _tarball())
        return _Resp(404)

    monkeypatch.setattr(frc.requests, "get", fake_get)
    assert frc.fetch_and_extract() is True
    assert (seed_dir / "manifest.json").exists()
    assert (seed_dir / "prices" / "AAPL_2009-01-01.pkl").read_bytes() == b"x"
    assert calls[0].endswith("/releases/download/cache-v1/seed_cache_2010_2026.tar.gz")


def test_api_is_only_the_fallback(seed_dir, monkeypatch):
    """Every direct candidate misses; the API lists the asset and its download
    works. Extraction is identical."""
    asset_url = "https://api.github.com/repos/oshrit-feruz/stock-screener/releases/assets/1"

    def fake_get(url, **kw):
        if "/releases/download/" in url:
            return _Resp(404)
        if url.endswith("/releases/tags/cache-v1"):
            return _Resp(200, payload={"assets": [{"name": "seed_cache_2010_2026.7.tar.gz",
                                                    "url": asset_url, "size": 1}]})
        if url == asset_url:
            return _Resp(200, _tarball())
        raise AssertionError(url)

    monkeypatch.setattr(frc.requests, "get", fake_get)
    assert frc.fetch_and_extract() is True
    assert (seed_dir / "manifest.json").exists()


def test_api_rate_limit_fails_open(seed_dir, monkeypatch):
    """Direct misses and the API answers 403: no exception, no seed, False."""
    def fake_get(url, **kw):
        return _Resp(403 if "api.github.com" in url else 404)

    monkeypatch.setattr(frc.requests, "get", fake_get)
    assert frc.fetch_and_extract() is False
    assert not (seed_dir / "manifest.json").exists()


def test_present_seed_skips_every_request(seed_dir, monkeypatch):
    """A seed with a manifest is final: the fetch makes no request at all."""
    seed_dir.mkdir(parents=True)
    (seed_dir / "manifest.json").write_text(json.dumps({}))

    def fake_get(url, **kw):
        raise AssertionError("no request expected when the seed is present")

    monkeypatch.setattr(frc.requests, "get", fake_get)
    assert frc.fetch_and_extract() is True


def test_truncated_archive_installs_nothing(seed_dir, monkeypatch):
    """A download that dies after manifest.json must not leave a "present"
    seed behind — that manifest would make every later fetch skip while the
    price cache stayed empty. Extraction stages, validates, then swaps."""
    whole = _tarball()
    truncated = whole[: len(whole) // 2]

    def fake_get(url, **kw):
        hit = url.endswith("/seed_cache_2010_2026.1.tar.gz")
        return _Resp(200, truncated) if hit else _Resp(404)

    monkeypatch.setattr(frc.requests, "get", fake_get)
    assert frc.fetch_and_extract() is False
    assert not (seed_dir / "manifest.json").exists()
    assert not (seed_dir.parent / (seed_dir.name + ".partial")).exists()


def test_archive_without_manifest_installs_nothing(seed_dir, monkeypatch):
    """Complete but not a seed (no manifest.json): rejected, nothing installed."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("prices/AAPL_2009-01-01.pkl")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))

    def fake_get(url, **kw):
        return _Resp(200, buf.getvalue()) if "/releases/download/" in url else _Resp(403)

    monkeypatch.setattr(frc.requests, "get", fake_get)
    assert frc.fetch_and_extract() is False
    assert not seed_dir.exists()
