"""CORS hardening: cross-origin access is granted ONLY for an explicit allowlist,
never via a '*' + credentials fallback."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server


def _client(origins):
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    server._configure_cors(app, origins)
    return TestClient(app)


def test_allows_configured_origin():
    c = _client(["https://roadmap.frazil.app"])
    r = c.get("/ping", headers={"Origin": "https://roadmap.frazil.app"})
    assert r.headers.get("access-control-allow-origin") == "https://roadmap.frazil.app"
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_denies_unlisted_origin():
    c = _client(["https://roadmap.frazil.app"])
    r = c.get("/ping", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_no_allowlist_disables_cors_entirely():
    # The hardening: with no allowlist, NO origin is reflected and credentials
    # are never granted (previously this fell back to '*' + credentials).
    c = _client([])
    r = c.get("/ping", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers
    assert r.json() == {"ok": True}  # same-origin / non-CORS behaviour intact
