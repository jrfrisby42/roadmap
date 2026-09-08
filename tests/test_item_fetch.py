"""ITEM-FETCH-2 (Part B of ITEM-FETCH-1) regression guard: a miss in the in-memory set asks the server.

openItem resolved the id against the boot-loaded in-memory `projects` array; the notification poll never
refreshes it, so an item created mid-session read "Item not found" until a manual refresh. The fix: on a
miss, re-fetch /api/all ONCE (via a classic re-sync helper called through _call), re-sync `projects`, and
retry - reporting not-found only if the server also lacks it. Scope-safe by construction (/api/all returns
only readable items). Client-only; no server.py change; no new endpoint.

These are closure-scoped frontend functions with no JS runtime in this harness, so this is a SOURCE-SHAPE
guard: it asserts the fallback + the per-id guard are wired at openItem's miss branch (not the bell handler)
and re-sync via the classic helper. Behavioural cases live in tests/item_fetch_checks.js and the live pass.
Each assertion FAILS when ITEM-FETCH-2 is reverted (demonstrated at build vs HEAD = 6.36.1).
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src() -> str:
    return ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")


def _open_item_body() -> str:
    # openItem's first ~30 lines (through the miss branch); enough to cover the fallback.
    m = re.search(r"function openItem\(id, opts\)\{(.*?)// PANEL-FIX", _src(), re.DOTALL)
    assert m, "openItem() / its miss branch not found"
    return m.group(1)


def test_classic_resync_helper_exists_and_is_projects_only():
    src = _src()
    m = re.search(r"async function _frzReloadAllData\(\)\{(.*?)\n\}", src, re.DOTALL)
    assert m, "classic helper _frzReloadAllData() is missing"
    body = m.group(1)
    assert "API.get('/api/all')" in body, "the re-sync must fetch /api/all via the API wrapper"
    assert "projects = data.projects" in body, "the helper must re-sync the projects array from the fresh payload"
    # projects-only: it must not clobber other in-flight globals
    assert "statuses =" not in body and "users =" not in body, "re-sync should be projects-only"


def test_fallback_is_at_openitem_miss_branch():
    body = _open_item_body()
    assert "_frzReloadAllData" in body, "openItem's miss branch must call the re-sync helper (not report not-found immediately)"
    assert "_call('_frzReloadAllData')" in body, "must invoke the classic helper via _call (beta->classic bridge)"


def test_per_id_refetch_guard():
    body = _open_item_body()
    assert "_itemFetchTried" in body, "a per-id 'already tried' guard must prevent repeated full re-fetches"


def test_honest_loading_and_distinct_load_failure():
    body = _open_item_body()
    assert "showLoading" in body, "an honest loading state must show while the re-fetch is in flight"
    assert ".catch(" in body, "a refetch failure must be caught and reported as a load failure, distinct from not-found"


def test_failed_fetch_clears_guard_so_retryable():
    # ITEM-FETCH-2 follow-up: the guard is set BEFORE the fetch; a failed fetch (the server never
    # successfully asked) must CLEAR it so the same id can be retried - else the second click short-circuits
    # to "Item not found", collapsing load-failure into not-found and stranding a real item behind a refresh.
    # Both failure paths clear it (the non-thenable early-return AND the .catch); success-but-absent keeps it.
    body = _open_item_body()
    assert body.count("delete state._itemFetchTried[id]") >= 2, (
        "both failure paths (non-thenable early-return and .catch) must clear the per-id guard so a failed "
        "attempt is retryable; only the success-but-absent path keeps it"
    )


def test_no_raw_fetch_added():
    # the fix must use API/_call, never raw fetch() in openItem's miss branch
    body = _open_item_body()
    assert "fetch(" not in body, "must not use raw fetch() (misses Authorization/X-Team) - use the API wrapper"


def test_bell_handler_still_only_navigates():
    # the fallback belongs in openItem, not the bell click handler.
    src = _src()
    m = re.search(r"var nid=\+b\.getAttribute\('data-nid'\), it=\+b\.getAttribute\('data-nitem'\);(.*?)\}\);", src, re.DOTALL)
    assert m, "bell click handler not found"
    assert "_frzReloadAllData" not in m.group(1), "the fallback must NOT be in the bell handler (every openItem caller must benefit)"
