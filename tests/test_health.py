"""Basic liveness: the app boots, serves the SPA, and reports its version."""


def test_root_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Frazil" in r.text


def test_version_endpoint(client):
    import server
    r = client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert body["server"] == server.APP_VERSION
