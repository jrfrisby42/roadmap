"""The /beta shell is additive: it serves the same roadmap.html at /beta and its
subpaths, and never alters the production '/' route. The shell itself is a
route-gated client module — these tests only assert the server contract."""


def test_beta_root_serves_app(client):
    r = client.get("/beta")
    assert r.status_code == 200
    assert "frzBetaStyle" in r.text          # the beta module is present in the served HTML
    assert "<!DOCTYPE html>" in r.text or "<html" in r.text


def test_beta_view_subpaths_serve_app(client):
    for path in ("/beta/gantt", "/beta/kanban", "/beta/list",
                 "/beta/planning", "/beta/dashboard", "/beta/item/123"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "frzBeta" in r.text, path      # same app shell on every /beta route


def test_beta_serves_identical_html_to_root(client):
    # /beta must serve the very same document as '/', not a forked copy.
    assert client.get("/beta").text == client.get("/").text
    assert client.get("/beta/list").text == client.get("/").text


def test_production_root_still_works(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "roadmap" in r.text.lower()
