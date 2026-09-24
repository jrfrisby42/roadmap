"""PARENT-ROLLUP-2: two defects in the 6.57.0 roll-up.
A: mid-tier parents (an Epic that is itself a child) drew nothing - the render gate excluded them.
B: the roll-up did not recurse, so a Roadmap Item above dateless Epics with dated grandchildren
   derived nothing.

Client-only, both in the Gantt render path. The capacity no-double-count invariant must survive
recursion (more parents draw bars) - re-proven here.
"""
import os

BS = chr(92)
_HTML = None


def _html():
    global _HTML
    if _HTML is None:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "roadmap.html")
        with open(p, encoding="utf-8") as f:
            _HTML = f.read()
    return _HTML


def _fn_body(name):
    h = _html(); i = h.find("function " + name); assert i >= 0, name + " not found"
    b = h.find("{", i); depth, j, instr = 0, b, None
    while j < len(h):
        ch = h[j]
        if instr:
            if ch == BS:
                j += 2; continue
            if ch == instr:
                instr = None
            j += 1; continue
        if ch in ("'", '"', "`"):
            instr = ch; j += 1; continue
        if ch == "/" and j + 1 < len(h) and h[j+1] == "/":
            k = h.find("\n", j); j = k if k >= 0 else len(h); continue
        if ch == "/" and j + 1 < len(h) and h[j+1] == "*":
            k = h.find("*/", j + 2); j = (k + 2) if k >= 0 else len(h); continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return h[i:j + 1]
        j += 1
    raise AssertionError(name + " unclosed")


# ── Defect A: the render gate now draws mid-tier (isChild) parents ────────────────
def test_render_gate_allows_mid_tier_parents():
    h = _html()
    assert "if(_ru){" in h                                   # gate no longer requires !isChild
    assert "if(_ru && !isChild){" not in h                   # old gate is gone
    assert 'class="left-row rollup-row${isChild?' in h        # child-row class added for indentation


# ── Defect B: recursion over transitive descendants ──────────────────────────────
def test_computeRollup_recurses_over_descendants():
    b = _fn_body("_computeRollup")
    assert "_rollupDescendants(p.id)" in b                   # aggregates over the whole subtree, not direct kids
    assert "_kidsByParent[p.id]" in b                        # still gated on having children


def test_descendant_walk_has_cycle_guard():
    b = _fn_body("_rollupDescendants")
    assert "seen" in b and "new Set()" in b
    assert "if(seen.has(c.id)) return;" in b                 # a malformed cycle terminates
    assert "_rollupDescendants(c.id, seen)" in b             # depth-agnostic recursion


def test_defectB_rules_unchanged_over_deeper_set():
    b = _fn_body("_computeRollup")
    # the derived-field rules are unchanged, only the SET they run over is deeper
    assert "c.revised||c.due" in b
    assert "s!=='Duplicate'" in b and "!statusIsOffFlow[s]" in b
    assert "kids.every(c => statusIsReleased[c.status])" in b
    assert "' +'+(pods.length-1)" in b


# ── invariants: own-dated still wins; no write path; capacity unchanged ───────────
def test_own_dated_still_untouched():
    assert "if(p.start && p.due) return null;" in _fn_body("_computeRollup")


def test_no_write_path_after_recursion():
    for fn in ("_computeRollup", "_rollupDescendants"):
        b = _fn_body(fn)
        for w in ("API.", "saveConfig", "putConfig", "p.start =", "p.due =", "p.status =", "p.dev ="):
            assert w not in b, (fn, w)


def test_capacity_functions_unchanged():   # INVARIANT (the load-bearing one)
    b = _fn_body("buildCapacityUsageMap")
    assert "if(p.hidden || !p.start || !p.dev) return;" in b
    ic = _fn_body("itemCapacityOnDay")
    assert "if(!p.start || !p.dev) return 0;" in ic
