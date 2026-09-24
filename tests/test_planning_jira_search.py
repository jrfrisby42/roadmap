"""PLANNING-JIRA-SEARCH-1: a pasted Jira key filters the planning backlog and the add-to-release
picker to the matching item. Filter only, never jump; no key detection; List server-FTS untouched.

Client-only, additive one-line haystack extensions (same shape as JIRA-LOOKUP-1's itemMatchesSearch
parity). Source-assertions against roadmap.html.
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


# ── backlog search now includes jiraTickets (filter only) ─────────────────────────
def test_backlog_search_matches_jira_key():
    b = _fn_body("_blSearchMatch")
    assert "(p.jiraTickets||[]).join(' ')" in b
    assert "p.itemKey" in b and "p.name" in b            # ordinary fields kept
    assert "if(!_blSearch) return true;" in b            # empty query -> no filtering (unchanged)


def test_backlog_search_is_filter_not_jump():
    b = _fn_body("_blSearchMatch")
    for jump in ("openItem", "navigate", "frzGlobalSearch", "_frzSearchJump", "_frzJiraKeyFromQuery"):
        assert jump not in b, jump                        # no jump, no key detection


# ── release-pane (add-to-release) search now includes jiraTickets ────────────────
def test_release_pane_search_matches_jira_key():
    h = _html()
    assert ("String(p.itemKey||'').toLowerCase().indexOf(q)>=0 || "
            "(p.jiraTickets||[]).join(' ').toLowerCase().indexOf(q)>=0") in h


# ── invariants ───────────────────────────────────────────────────────────────────
def test_backlog_structural_filters_untouched():   # INVARIANT
    b = _fn_body("backlogItems")
    for f in ("archived", "hidden", "inScope", "isPlannable", "inNonCompletedSprint"):
        assert f in b, f


def test_top_bar_jump_unchanged():   # INVARIANT (this prompt must not touch it)
    b = _fn_body("frzGlobalSearch")
    assert "_frzSearchJump(res.jump)" in b
    assert "_frzJiraKeyFromQuery(q)" in b


def test_list_fts_shelved_untouched():   # INVARIANT (server FTS path not touched)
    # _fts_match still strips to alnum + prefix (unchanged); no jiraTickets indexed into FTS
    assert "def _fts_match" in _srv()
    assert "jiraTickets" not in _srv_fn("_fts_sync")


def _srv():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")
    with open(p, encoding="utf-8") as f:
        return f.read()


def _srv_fn(name):
    s = _srv(); i = s.find("def " + name)
    assert i >= 0, name
    nxt = s.find("\ndef ", i + 1)
    return s[i:(nxt if nxt >= 0 else len(s))]
