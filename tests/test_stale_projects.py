"""STALE-PROJECTS-1: a row the List drew from fresher /api/items data must not dead-click.

The List (and My Home) render rows from a server /api/items fetch, but the click-to-open handlers resolve
the id against the in-memory `projects` array, which is loaded once at boot and not refreshed by the
notification poll. An item created mid-session (portal, a colleague, a Jira spawn) therefore renders a
clickable row that resolves to nothing. `openItem` already re-fetched-on-miss (ITEM-FETCH-2); the List
detail panel `frzOpenPanel` did NOT - it silently `frzClosePanel(); return`'d. The fix extracts the
re-fetch-on-miss into a shared `_frzResolveOrReload(id, onResolved)` that BOTH open-paths call.

Static source guards (frontend; the rendered repro runs in the browser). server.py is untouched.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


def test_shared_resolver_exists_with_refetch_and_guards():
    src = _html()
    fn = src.split("function _frzResolveOrReload(id, onResolved){", 1)[1].split("\n  }", 1)[0]
    assert "onResolved()" in fn, "resolver calls the continuation once the item is present"
    assert "_frzReloadAllData" in fn, "resolver re-fetches /api/all + re-syncs projects on a miss"
    assert "state._itemFetchTried" in fn, "resolver is loop-guarded (one re-fetch per id)"
    assert "showToast" in fn, "a genuine miss / load failure is surfaced, never silent"


def test_openitem_miss_uses_the_shared_resolver():
    """openItem's inline ITEM-FETCH-2 block is now the shared resolver (behavior preserved)."""
    src = _html()
    oi = src.split("function openItem(id, opts){", 1)[1].split("\n  }", 1)[0]
    assert "_frzResolveOrReload(id, function(){ openItem(id, opts); })" in oi, \
        "openItem must delegate its miss to the shared resolver"


def test_frzopenpanel_miss_uses_the_shared_resolver_not_a_silent_return():
    """THE fix. Revert: restore `if(!p){ frzClosePanel(); return; }` and the desktop List panel dead-clicks
    a fresh row again."""
    src = _html()
    fp = src.split("function frzOpenPanel(id, opts){", 1)[1].split("\n  }", 1)[0]
    assert "_frzResolveOrReload(id, function(){ frzOpenPanel(id, opts); })" in fp, \
        "frzOpenPanel must re-fetch + retry on a miss, not silently close"
    assert "if(!p){ frzClosePanel(); return; }" not in fp, "the old silent no-op must be gone"


def test_my_home_open_path_uses_openitem_invariant():
    """INVARIANT: My Home rows (also rendered from /api/items) open via openItem, which routes through the
    shared resolver - so they were never the silent path and stay covered."""
    src = _html()
    assert "document.getElementById('frzHomeBody').addEventListener('click'" in src
    home = src.split("getElementById('frzHomeBody').addEventListener('click'", 1)[1][:300]
    assert "openItem(id)" in home, "My Home opens via openItem (shared resolver)"


def test_client_only_no_server_symbol():
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "_frzResolveOrReload" not in py, "STALE-PROJECTS-1 is client-only; no symbol in server.py"
