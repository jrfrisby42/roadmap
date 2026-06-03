"""PWA surface: manifest, service worker, and icons are served correctly and
without auth (browsers fetch these anonymously during install)."""
import server


def test_manifest_served(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    body = r.json()
    assert body["name"] == "Frazil Roadmap"
    assert body["display"] == "standalone"
    assert body["start_url"] == "/"
    # At least one 512 icon, and a maskable variant for adaptive launchers.
    assert any(i["sizes"] == "512x512" for i in body["icons"])
    assert any(i.get("purpose") == "maskable" for i in body["icons"])


def test_service_worker_served(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers.get("service-worker-allowed") == "/"
    # Cache name must be versioned (busts the shell on release) and /api stays
    # network-only.
    assert f"frazil-shell-{server.APP_VERSION}" in r.text
    assert "/api/" in r.text


def test_icons_served_as_png():
    """Icons must be generated (non-placeholder) and served as real PNG bytes."""
    from fastapi.testclient import TestClient
    c = TestClient(server.app)
    for path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
        r = c.get(path)
        assert r.status_code == 200, f"{path} not served — run tools/gen_pwa_icons.py"
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic number


def test_pwa_assets_need_no_auth(client):
    """Install-time fetches are anonymous — these must not require a token."""
    for path in ("/manifest.webmanifest", "/sw.js", "/icon-192.png"):
        assert client.get(path).status_code == 200
