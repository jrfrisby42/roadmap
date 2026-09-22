"""FLAG-BLOCKED-2 - the Flag modal offers "Blocked" only when a Blocked status is configured
(getBlockedStatus() truthy); otherwise flagging Blocked would silently no-op. Client-only; asserted
against roadmap.html (the _html grep style; no node for a live JS run)."""
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
    h = _html(); i = h.find("function " + name); assert i >= 0, name + " not found"
    b = h.find("{", i); depth, j, instr = 0, b, None
    while j < len(h):
        ch = h[j]
        if instr:
            if ch == "\\": j += 2; continue
            if ch == instr: instr = None
            j += 1; continue
        if ch in ("'", '"', "`"): instr = ch; j += 1; continue
        if ch == "/" and j + 1 < len(h) and h[j + 1] == "/":
            k = h.find("\n", j); j = k if k >= 0 else len(h); continue
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0: return h[i:j + 1]
        j += 1
    raise AssertionError(name + " unclosed")


def test_getBlockedStatus_returns_empty_when_unset():
    # the whole gate depends on this being falsy (not a throw / default) when no Blocked status is set
    assert "const getBlockedStatus  = () => statuses.find(s => statusIsBlocked[s])  || '';" in _html()


def test_openFlagIssue_filters_blocked_on_config():
    body = _fn_body("openFlagIssue")
    assert "option[value=\"Blocked\"]" in body               # targets the Blocked option in the one modal
    assert "getBlockedStatus()" in body
    assert "_blockedOpt.hidden" in body and "_blockedOpt.disabled" in body   # absent AND unselectable when unset
    assert "FLAG-BLOCKED-2" in body


def test_flag_type_markup_unchanged():
    h = _html()
    for opt in ('<option value="At Risk">', '<option value="Blocked">', '<option value="Needs Decision">', '<option value="Reminder">'):
        assert opt in h                                      # the static option set is untouched (filtered at runtime)


def test_submitFlagIssue_blocked_binding_untouched():
    body = _fn_body("submitFlagIssue")
    # invariant: the status-setting path, prior-status stash and the already-blocked guard are all intact
    assert "p.preBlockStatus = prev;" in body
    assert "p.status !== blockedStatus" in body
    assert "getBlockedStatus()" in body
