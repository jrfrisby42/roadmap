"""CAPACITY-HISTORY-1 - the two DISPLAY capacity functions drop the terminal filter so finished work
shows as historical load; the CONFLICT engine keeps its filter (no past alerts). Client-only; asserted
against roadmap.html function bodies (the _html grep style; no node for a live JS run)."""
import os

_HTML = None


def _html():
    global _HTML
    if _HTML is None:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "roadmap.html")
        with open(p, encoding="utf-8") as f:
            _HTML = f.read()
    return _HTML


def _fn_body(name):
    """Return the source of `function <name>(...) { ... }` by brace-matching."""
    h = _html()
    i = h.find("function " + name)
    assert i >= 0, name + " not found"
    b = h.find("{", i)
    depth, j, instr = 0, b, None
    while j < len(h):
        ch = h[j]
        if instr:
            if ch == "\\":
                j += 2; continue
            if ch == instr:
                instr = None
            j += 1; continue
        if ch in ("'", '"', "`"):
            instr = ch; j += 1; continue
        if ch == "/" and j + 1 < len(h) and h[j + 1] == "/":
            k = h.find("\n", j); j = k if k >= 0 else len(h); continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return h[i:j + 1]
        j += 1
    raise AssertionError(name + " unclosed")


# ── the two DISPLAY functions: terminal filter GONE, the other filters KEPT ─────────────────────────────
def test_buildCapacityUsageMap_drops_terminal_keeps_others():
    body = _fn_body("buildCapacityUsageMap")
    assert "if(isTerminalStatus(p.status)) return;" not in body      # terminal filter removed (history lights up)
    assert "CAPACITY-HISTORY-1" in body                              # the why-comment is present at the site
    for keep in ("statusIgnoreConflicts[p.status]", "productIgnoreConflicts[p.product]", "isScheduledType(p.type", "p.hidden"):
        assert keep in body, "must keep filter: " + keep


def test_capacityHealthClass_drops_terminal_keeps_others():
    body = _fn_body("capacityHealthClass")
    assert "if(isTerminalStatus(p.status)) return;" not in body      # cell colour now matches the bars
    assert "CAPACITY-HISTORY-1" in body
    for keep in ("statusIgnoreConflicts[p.status]", "productIgnoreConflicts[p.product]", "isScheduledType(p.type"):
        assert keep in body, "must keep filter: " + keep


# ── the CONFLICT engine still excludes terminal (invariant - no past alerts) ────────────────────────────
def test_computeConflictingIds_still_excludes_terminal():
    body = _fn_body("computeConflictingIds")
    assert "if(isTerminalStatus(p.status)) return;" in body          # invariant: conflicts stay today-forward


def test_client_only_no_server_symbol():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")
    with open(p, encoding="utf-8") as f:
        srv = f.read()
    assert "buildCapacityUsageMap" not in srv and "capacityHealthClass" not in srv  # capacity display is client-only
