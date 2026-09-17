"""OVERLAY-DISMISS-1: close overlays on route change (roadmap.html only; client-side).

Dropdowns (closeAllDropdowns - 5 hardcoded filter IDs), popovers (closePop - state.pop) and the mention
selector (_closeMention - frz-mention-menu, body-level, outside both) used to survive navigation and
float over the next page, because none of the three route entries (navigate / routeFromLocation / the
shell popstate) invoked any dismissal. The fix is ONE closer, `_frzCloseOverlays()`, that CALLS all three
mechanisms (it does not unify them) and is invoked at all three route boundaries - in navigate() BEFORE
the /item early-return, or an item deep-link would leave overlays open.

Static source guards (frontend behavior has no pytest harness; the rendered pass runs in the browser).
server.py is untouched.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


def test_closer_calls_all_three_mechanisms():
    """Revert: drop the _closeMention() call and the reported case (mention menu floats) regresses."""
    src = _html()
    closer = src.split("function _frzCloseOverlays(){", 1)[1].split("\n  }", 1)[0]
    assert "closeAllDropdowns()" in closer, "closer must call closeAllDropdowns()"
    assert "closePop()" in closer, "closer must call closePop()"
    assert "_closeMention()" in closer, "closer must dismiss the mention menu"


def test_invoked_in_navigate_before_the_item_early_return():
    """Revert: move the call below the /item branch (or remove it) and an item deep-link leaves overlays open."""
    src = _html()
    nav = src.split("function navigate(url, replace){", 1)[1].split("function lastView(", 1)[0]
    assert "_frzCloseOverlays()" in nav, "navigate() must invoke the closer"
    assert nav.index("_frzCloseOverlays()") < nav.index("openItem("), \
        "the closer must run BEFORE navigate()'s /item early-return (openItem)"


def test_invoked_in_route_from_location():
    src = _html()
    rfl = src.split("function routeFromLocation(initial){", 1)[1][:400]
    assert "_frzCloseOverlays()" in rfl, "routeFromLocation() must invoke the closer near the top"


def test_invoked_in_the_shell_popstate():
    """The shell popstate (function(e){ ...) - distinct from the classic arrow-fn popstate."""
    src = _html()
    ps = src.split("window.addEventListener('popstate', function(e){", 1)[1][:400]
    assert "_frzCloseOverlays()" in ps, "the shell popstate handler must invoke the closer"


def test_closealldropdowns_id_list_unchanged_invariant():
    """INVARIANT (Part 2): the fix must NOT extend closeAllDropdowns's hardcoded filter-ID list."""
    src = _html()
    assert "['productDropBtn','devDropBtn','assigneeDropBtn','typeDropBtn','statusDropBtn']" in src, \
        "the 5-ID filter list must be exactly as before (this fix is about route changes, not registration)"


def test_three_mechanisms_not_unified_invariant():
    """INVARIANT (Part 2): the closer CALLS the three mechanisms; it does not replace/unify them."""
    src = _html()
    assert "function closeAllDropdowns(){" in src
    assert "function closePop(){" in src
    assert "function _closeMention(){" in src


def test_client_only_no_server_symbol():
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "_frzCloseOverlays" not in py, "OVERLAY-DISMISS-1 is client-only; no symbol in server.py"


# ── OVERLAY-DISMISS-2: close overlays at the modal-close boundary ───────────────────────────────────
def test_overlay_dismiss_2_invoked_from_modal_teardown():
    """The closer also runs at the single modal-close funnel (onClose). Revert: remove the call and the
    mention menu survives closing the item modal (the reported bug)."""
    src = _html()
    oc = src.split("function onClose(){", 1)[1].split("\n  }", 1)[0]
    assert "_frzCloseOverlays" in oc, "onClose (the single modal-close funnel) must invoke the closer"


def test_overlay_dismiss_2_window_export():
    """_frzCloseOverlays is beta-scoped; onClose is classic, so it must be exposed on window. Revert: drop
    the export and the classic teardown can't reach the closer."""
    src = _html()
    assert "window._frzCloseOverlays = _frzCloseOverlays" in src, \
        "the closer must be exposed on window for the classic modal teardown to reach it"


def test_overlay_dismiss_1_route_calls_still_intact_invariant():
    """INVARIANT: OVERLAY-DISMISS-1's three route call sites are untouched by this modal-boundary fix."""
    src = _html()
    nav = src.split("function navigate(url, replace){", 1)[1].split("function lastView(", 1)[0]
    assert "_frzCloseOverlays()" in nav and nav.index("_frzCloseOverlays()") < nav.index("openItem(")
    rfl = src.split("function routeFromLocation(initial){", 1)[1][:400]
    assert "_frzCloseOverlays()" in rfl
    ps = src.split("window.addEventListener('popstate', function(e){", 1)[1][:400]
    assert "_frzCloseOverlays()" in ps
