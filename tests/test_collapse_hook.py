"""COLLAPSE-HOOK-1 regression guard: the rail-collapse toggle is drivable for verification.

The toggle (#frzCollapse / #frzTopCollapse / "[") is not drivable by tooling and setCollapsed is
closure-scoped, so the collapsed-rail cases went unverified across many stages. This exposes two thin
globals (like setTheme) that route through the EXISTING setCollapsed - no second code path, no UI change.

SOURCE-SHAPE guards: they prove the hooks exist and route correctly. They do NOT prove the hook produces
the same DOM state as a real click - that is tests/collapse_hook_checks.js + the live pass (the equivalence
check). Reverting deletes the globals (they do not exist at HEAD), which is the trivial revert demo and is
NOT sufficient on its own. server.py is untouched by this stage.
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src() -> str:
    return ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")


def test_both_globals_defined_unconditionally():
    src = _src()
    assert "window._frzSetCollapsed = function(on){" in src, "_frzSetCollapsed must be defined on window"
    assert "window._frzIsCollapsed  = function(){" in src or "window._frzIsCollapsed = function(){" in src, \
        "_frzIsCollapsed must be defined on window"
    # not behind a debug flag - defined at the same top level as setCollapsed/toggleCollapse
    m = re.search(r"function toggleCollapse\(\)\{[^}]*\}\s*(.*?)window\._frzSetCollapsed", src, re.DOTALL)
    assert m and "if(" not in m.group(1).replace("//", ""), "the hooks must be defined unconditionally, not behind a flag"


def test_setter_routes_through_setCollapsed_not_a_second_path():
    src = _src()
    m = re.search(r"window\._frzSetCollapsed = function\(on\)\{(.*?)\};", src)
    assert m, "_frzSetCollapsed body not found"
    body = m.group(1)
    # must call setCollapsed with silent=false (matches the button/toggleCollapse, persists)
    assert re.search(r"setCollapsed\(!!on, false\)", body), "setter must call setCollapsed(on, false) - route through the real fn, persist"
    # must NOT reimplement: no class/grid/brand/persistence work of its own
    for forbidden in ("classList", "railKey", "grid-template", "frz-rail-hidden", "is-collapsed", "lset("):
        assert forbidden not in body, f"the setter must not do its own {forbidden} work (no second code path)"


def test_reader_reads_internal_boolean_not_dom():
    src = _src()
    m = re.search(r"window\._frzIsCollapsed  ?= function\(\)\{(.*?)\};", src)
    assert m, "_frzIsCollapsed body not found"
    body = m.group(1)
    assert "state.collapsed" in body, "_frzIsCollapsed must read the internal boolean state.collapsed"
    assert "classList" not in body and "frz-rail-hidden" not in body and "is-collapsed" not in body, \
        "_frzIsCollapsed must NOT sniff a DOM class (that would be circular)"


def test_verification_comment_present():
    # the 1.5 comment must state: verification-only, persists preference, keep routing through setCollapsed
    src = _src()
    m = re.search(r"COLLAPSE-HOOK-1:(.*?)window\._frzSetCollapsed", src, re.DOTALL)
    assert m, "COLLAPSE-HOOK-1 comment block not found before the hooks"
    c = m.group(1)
    assert "verification" in c.lower(), "comment must say it exists for verification"
    assert "persist" in c.lower(), "comment must warn it persists the collapse preference"
    assert "setCollapsed" in c and "never grow its own" in c.lower(), "comment must require routing through setCollapsed"
