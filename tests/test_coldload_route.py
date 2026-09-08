"""COLDLOAD-2 regression guard: a deep-linked cold load must NOT be hijacked to the default filter's view.

Root cause (COLDLOAD-1 Part A): an async race. `_frzSyncSavedFromServer`'s post-hydration default-apply
awaits one API call and typically wins over `routeFromLocation` (behind three sequential loads); it fired
`_frzApplyDefaultFilter(state.view)` against the STALE boot-default view, rebuilding the URL and discarding
the requested route and its non-filter params (item/panel).

The fix records the requested route once at mount (`state.bootReq`) and gates BOTH default-apply callers on
`_frzBootSpecifiesDest()`, with the hydration path applying against `_frzBootReqView()` - never `state.view`.

WHY THIS IS A SOURCE-SHAPE GUARD, stated plainly (per COLDLOAD-2 2.4): `_frzBootSpecifiesDest`,
`_frzApplyDefaultFilter`, `routeFromLocation` and `_frzSyncSavedFromServer` are closure-scoped frontend JS.
There is no JS runtime in this harness (no node on PATH, no committed jsdom), and `state` is not readable from
a browser console, so the DECISION cannot be exercised in an automated test. This guard asserts the decision is
WIRED IN - both callers consult the gate, and the hydration path does not read state.view - which is what a
straight revert of COLDLOAD-2 removes. The behavioural decision/landing assertions and both-ordering coverage
live in tests/coldload_checks.js (a live console snippet for Claude's post-deploy pass); case 7 (no-default
account) is blocked on a fixture that does not exist (see that file and the COLDLOAD-2 report).

Each assertion below FAILS when COLDLOAD-2 is reverted (demonstrated at build time against HEAD=6.35.5).
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src() -> str:
    return ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")


def test_requested_route_captured_at_mount():
    # state.bootReq is recorded once, from location, before either default-apply path can fire.
    assert re.search(r"state\.bootReq\s*=\s*\{\s*path:", _src()), (
        "state.bootReq is not captured at mount - the recorded requested route is the value both "
        "default-apply callers need; without it the hydration path falls back to the stale state.view."
    )


def _specifies_dest_body() -> str:
    m = re.search(r"function _frzBootSpecifiesDest\(\)\s*\{(.*?)\n  \}", _src(), re.DOTALL)
    assert m, "function _frzBootSpecifiesDest() is missing"
    return m.group(1)


def test_specifies_dest_is_account_independent():
    # COLDLOAD-2 Addendum A: specified-ness must be PATH PRESENCE, not a comparison against lastView().
    # The old predicate did `vm[1] !== (lastView()||'gantt')`, so a user whose lastView equalled the
    # requested view got the default (hijack). The fix removes lastView() from the decision entirely.
    body = _specifies_dest_body()
    # strip full-line // comments (the fix's comment legitimately mentions lastView to explain its removal)
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("//"))
    assert "lastView" not in code, (
        "specified-ness CODE still references lastView() - it is account-dependent: a user whose lastView "
        "equals the requested view is hijacked to the default filter's view. Decide by path presence."
    )
    assert "req.path" in code, "the predicate must decide on request path presence (req.path)"
    assert "panel" in body and "release" in body, "a bare path with a destination param (panel/release) must still count as specified"


def test_bootreqview_still_uses_lastview():
    # lastView() is the CORRECT source for WHERE a bare landing goes - it must survive in _frzBootReqView,
    # only removed from the specified-ness DECISION. Guards against over-removal.
    m = re.search(r"function _frzBootReqView\(\)\s*\{(.*?)\n  \}", _src(), re.DOTALL)
    assert m, "function _frzBootReqView() is missing"
    assert "lastView" in m.group(1), "_frzBootReqView must still resolve a bare landing via lastView()||'gantt'"


def test_router_default_apply_is_gated_by_specifies_dest():
    # The routeFromLocation default-apply guard must consult _frzBootSpecifiesDest alongside _frzHasFilterParams.
    src = _src()
    m = re.search(r"if \(initial && !_frzHasFilterParams\(location\.search\)[^\n]*\)\{", src)
    assert m, "could not locate the routeFromLocation default-apply guard"
    assert "!_frzBootSpecifiesDest()" in m.group(0), (
        "the router's default-apply is not gated on _frzBootSpecifiesDest() - a specified destination "
        "(e.g. /reports, /list?panel=) would still be overridden by the default filter."
    )


def test_hydration_path_uses_recorded_view_not_state_view():
    # The post-hydration default-apply must apply against _frzBootReqView(), never state.view.
    src = _src()
    # the racing line `var _v=state.view;` inside the hydration block must be gone
    assert not re.search(r"\(Date\.now\(\)-_t0\)\s*<\s*3000[^\n]*\n\s*var _v=state\.view;", src), (
        "the hydration default-apply still reads state.view - the stale boot default that caused the "
        "cold-load hijack. It must apply against the recorded requested route (_frzBootReqView())."
    )
    assert "function _frzBootReqView(" in src, "_frzBootReqView() helper is missing"
    # and the hydration block must be gated on _frzBootSpecifiesDest too
    m = re.search(r"!state\.onItem && !_frzBootSpecifiesDest\(\)\)\{\s*\n\s*var _v=_frzBootReqView\(\);", src)
    assert m, "the hydration default-apply is not gated on _frzBootSpecifiesDest()/does not use _frzBootReqView()"
