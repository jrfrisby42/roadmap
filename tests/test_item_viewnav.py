"""ITEM-VIEWNAV-1 guards: a Views pill + a clickable Space crumb on the item page (roadmap.html only).

The item page (flag-on, desktop) has no view switcher. This stage adds a fixed-width "Views" pill to the
item-page utility cluster (#frzBadges, beside Watch/Flag) whose dropdown lists the same gated views the tab
strip uses and navigates through the SAME carry path, plus makes the breadcrumb's Space segment link to the
List scoped to that Space.

The load-bearing correctness point (found during the build): the existing carry (_frzCarryQS) reads the URL,
which is FILTER-LESS on an item page, so reusing it would silently drop filters. Fix (option A): one shared
key-list constant _FRZ_CARRY_KEYS with TWO readers - _frzCarryQS (URL) for the tab strip, _frzCarryQSFromState
(in-memory state) for the item-page controls. The shared constant is the anti-drift mechanism (not a guard):
these tests assert BOTH readers use the constant, so they cannot diverge.

Source-shape guards; visual/behaviour was screenshot- and query-verified live (both themes). server.py is
untouched (empty diff) - asserted here too. These fail when the stage is reverted.
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


# ── the shared carry-key constant + two readers (the anti-drift design) ──────────────────────────────────
def test_viewnav_shared_carry_constant_one_list_two_readers():
    src = _html()
    # the ONE key list is a named constant
    assert re.search(r"var _FRZ_CARRY_KEYS = \['project','owner','type','status','assignee','sprint','priority','dept','location','resolutionType','blockedReason','reporter'\];", src), \
        "the cross-view carry keys must live in a single named constant _FRZ_CARRY_KEYS"
    # _frzCarryQS (URL reader) reads the constant, not a hardcoded list
    m = re.search(r"function _frzCarryQS\(includeChips\)\{.*?\n  \}", src, re.DOTALL)
    assert m, "_frzCarryQS not found"
    q = m.group(0)
    assert "includeChips ? _FRZ_CARRY_KEYS : ['project']" in q, "_frzCarryQS must read the shared constant"
    assert "new URLSearchParams(location.search)" in q, "_frzCarryQS must still read the URL (tab-strip path unchanged)"
    # _frzCarryQSFromState (state reader) reads the SAME constant
    m2 = re.search(r"function _frzCarryQSFromState\(includeChips\)\{.*?\n  \}", src, re.DOTALL)
    assert m2, "_frzCarryQSFromState not found"
    s = m2.group(0)
    assert "includeChips ? _FRZ_CARRY_KEYS : ['project']" in s, "the state carry must read the SAME shared constant (anti-drift)"
    assert "state.project" in s and "CHIPS[ck]" in s and "state.sprintFilter" in s, \
        "the state carry must source values from in-memory state (project, chips, sprint), mirroring syncURL"


def test_viewnav_carryqs_not_folded_into_one_function():
    # COLDLOAD-2: a resting saved filter lives in memory while the URL deliberately does not express it, so
    # _frzCarryQS must NOT fall back to state (that would resurrect a resting filter as applied). Keep them
    # separate; the reasoning must be recorded so nobody unifies them later.
    src = _html()
    m = re.search(r"function _frzCarryQS\(includeChips\)\{.*?\n  \}", src, re.DOTALL)
    assert m and "state.project" not in m.group(0) and "CHIPS" not in m.group(0), \
        "_frzCarryQS must remain URL-only (no state fallback) - COLDLOAD-2 protection"
    assert "resurrect a resting" in src, "the do-not-unify reasoning (COLDLOAD-2) must be documented in a comment"


# ── the pill: rendered on flag-on, in the utility cluster ────────────────────────────────────────────────
def test_viewnav_pill_rendered_flag_on_only():
    src = _html()
    # RENDERED conditionally on _frzIA() (flag-on AND not phone), not CSS-hidden -> genuinely absent under flag-off
    assert re.search(r"if\(_frzIA\(\)\)\{\s*\n\s*badges \+= '<button class=\"frz-watch frz-views-pill\" id=\"frzViewsPill\"", src), \
        "the pill must be built only inside if(_frzIA()) - render gate, not a CSS hide"
    assert '<span>Views</span>' in src, "the pill label is the fixed text 'Views'"
    assert 'aria-haspopup="menu"' in src, "the pill declares a menu popup"
    # sits in the utility cluster (#frzBadges) alongside Watch/Flag, reusing .frz-watch
    assert 'id="frzViewsPill"' in src and 'frz-watch frz-views-pill' in src, "the pill reuses .frz-watch (Watch/Flag sibling)"


def test_viewnav_dropdown_source_mark_and_nav():
    src = _html()
    m = re.search(r"function _frzViewsMenu\(anchor\)\{.*?\n  \}", src, re.DOTALL)
    assert m, "_frzViewsMenu not found"
    d = m.group(0)
    assert "_frzOrderedViews().forEach" in d, "the dropdown lists the SAME gated source the tab strip uses (_frzOrderedViews)"
    assert "_frzItemArrivalView===v" in d, "the arrived-from view is marked from _frzItemArrivalView"
    assert "_frzNavViewFromItem(v)" in d, "selecting a view navigates via the item-page (state-based) carry"
    assert "openPop(anchor" in d, "the dropdown uses the shared openPop (shared dismissal: closePop on next open + _popAway)"
    assert "ev.key==='Escape'" in d, "Escape closes the dropdown"


def test_viewnav_navview_from_item_uses_state_carry():
    src = _html()
    m = re.search(r"function _frzNavViewFromItem\(v\)\{.*?\n  \}", src, re.DOTALL)
    assert m, "_frzNavViewFromItem not found"
    n = m.group(0)
    assert "_frzCarryQSFromState(true)" in n, "the item-page nav must carry from state, not the (filter-less) item URL"
    assert "navigate('/'+v+qs, false)" in n, "it navigates through the shared navigate()"
    # the tab strip's own _frzNavView stays URL-based and unchanged
    assert re.search(r"function _frzNavView\(v\)\{\s*//.*?\n\s*var qs=_frzCarryQS\(true\);", src), \
        "the tab strip's _frzNavView (URL carry) must be unchanged"


# ── the arrival signal (D4): in-app marks the view, cold marks nothing ───────────────────────────────────
def test_viewnav_arrival_signal():
    src = _html()
    assert "var _frzItemArrivalView = null;" in src, "the arrival view is an in-memory (non-persisted) module var"
    # openItem captures it: genuine view for an in-app arrival, null when arrivalUnknown
    assert "_frzItemArrivalView = (opts && opts.arrivalUnknown) ? null : (state.view || null);" in src, \
        "openItem must mark state.view on a genuine arrival and NOTHING when arrivalUnknown"
    # both cold-load / deep-link routeFromLocation branches pass arrivalUnknown:true
    assert src.count("arrivalUnknown:true") >= 2, "the cold deep-link / restore branches must pass arrivalUnknown:true"


# ── the clickable Space crumb ────────────────────────────────────────────────────────────────────────────
def test_viewnav_space_crumb_clickable_guarded():
    src = _html()
    # the Space segment becomes an interactive span WITHOUT restructuring the crumb; guarded on a real Space
    assert "var _spaceLink = (_spName && _spName!=='__all__');" in src, \
        "the Space link is guarded on a present, non-__all__ Space"
    assert 'id="frzCrumbSpace"' in src and 'class="frz-crumb-space"' in src, "the Space segment is a targetable span"
    assert "setProject(p.product); _frzNavViewFromItem('list');" in src, \
        "the Space crumb scopes the List (setProject) and carries the live chips via the state carry"
    # the deliberate divergence from COMPOSER-REFINE-1 Stage 1.4 is recorded so nobody 'harmonises' them
    assert "DELIBERATE DIVERGENCE from COMPOSER-REFINE-1 Stage 1.4" in src, \
        "the two Space affordances (crumb link vs composer label) must be documented as intentionally different"


def test_viewnav_server_untouched():
    # server.py must have no diff for this stage (roadmap.html only). A cheap invariant: the stage adds no
    # server symbol. (The empty-diff is also asserted at the git level in the deliverable.)
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "_frzViewsMenu" not in py and "_FRZ_CARRY_KEYS" not in py and "frzViewsPill" not in py, \
        "ITEM-VIEWNAV-1 is client-only; no symbol should appear in server.py"
