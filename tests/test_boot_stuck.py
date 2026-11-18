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


def test_service_worker_leaves_page_loads_to_the_browser(client):
    # SW-NAV-OFF-1 (supersedes the BOOT-STUCK-1 retry + saved copy): 2026-11-18 a worker-controlled tab sat on the
    # loading screen with readyState "loading" and ~7% of the page received. The worker must not handle navigations.
    sw = client.get("/sw.js").text
    assert "if (req.mode === 'navigate') return;" in sw
    assert "respondWith" not in sw.split("if (req.mode === 'navigate') return;", 1)[0], "nothing is answered before the bypass"
    assert "caches.match('/')" not in sw and "c.put('/'" not in sw, "no saved copy of the page"
    shell = sw.split("const SHELL = [", 1)[1].split("]", 1)[0]
    assert "'/'" not in shell, "install must not store a copy of the page"
    assert "caches.delete(k)" in sw, "activate drops the old caches that held the saved page"


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
