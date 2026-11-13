"""BOOT-STUCK-1: switching Organizations could leave "Loading data..." up forever.

J.R. caught it in DevTools (2026-11-13): the reload of `/` failed inside the service worker with
net::ERR_CACHE_MISS, and the worker fell back to its saved copy of the page. That copy was saved from ANY
navigation (including /audit and cut-off downloads), and the loading screen is in the page from the start, so
a bad copy never clears it. Fix: retry the navigation once with a fresh request; keep only a complete, marked
app page as the offline copy; and offer a Reload on the loading screen after 15s.
"""
import pathlib

import server

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def test_app_pages_are_marked_and_server_pages_are_not(client):
    for path in ("/", "/gantt", "/item/5", "/admin/types"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers.get("x-flow-shell") == "1", path
        assert r.text.rstrip().endswith("</html>"), "the worker only keeps a copy that ends in </html>"
    assert client.get("/report").headers.get("x-flow-shell") is None
    assert client.get("/sw.js").headers.get("x-flow-shell") is None


def test_service_worker_retries_and_keeps_only_a_complete_app_page(client):
    sw = client.get("/sw.js").text
    nav = sw.split("req.mode === 'navigate'", 1)[1].split("// Our own static assets", 1)[0]
    assert nav.index("await fetch(req)") < nav.index("cache: 'reload'") < nav.index("caches.match('/')"), \
        "network, then one fresh retry, then the saved copy"
    assert "res.headers.get('X-Flow-Shell') === '1'" in nav and "res.ok" in nav
    assert "endsWith('</html>')" in nav, "a cut-off page must never become the offline copy"
    assert "Response.error()" in nav
    shell = sw.split("const SHELL = [", 1)[1].split("]", 1)[0]
    assert "'/'" not in shell, "install must not store an unchecked copy of the page"


def test_loading_screen_offers_reload_after_15s():
    ov = HTML.split('<div id="loadingOverlay"', 1)[1]
    head, rest = ov.split("</div>\n<script>", 1)
    assert 'id="loadingStuck"' in head and 'id="loadingReload"' in head
    watch = rest.split("</script>", 1)[0]
    assert "window._frzLoadWatch = function(on)" in watch and "15000" in watch
    assert "window._frzLoadWatch(true);" in watch, "armed at page load, before any app script runs"
    assert "unregister()" in watch and "location.reload()" in watch
    assert "—" not in watch and "&mdash;" not in head


def test_show_loading_rearms_the_watch():
    line = [l for l in HTML.splitlines() if l.startswith("function showLoading(on)")][0]
    assert "_frzLoadWatch(on)" in line
