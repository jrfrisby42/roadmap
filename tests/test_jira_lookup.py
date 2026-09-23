"""JIRA-LOOKUP-1: find the Flow item from a Jira key (Option B - client-side resolution over the
in-memory projects[], no schema/backfill). Jump on a single non-hidden match, list otherwise; ungated
(the project mapping supplies the prefixes); browse-URL + case/whitespace tolerant; the in-memory
Gantt/Kanban filter gains parity so a pasted key filters there too.

Client-only. Source-assertions against roadmap.html via the _fn_body walker.
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


def test_prefixes_from_mapping_not_hardcoded():
    b = _fn_body("_frzJiraPrefixes")
    assert "window._jiraProjectMappingData" in b       # derived from the Org mapping VALUES
    assert "toUpperCase()" in b
    assert "FRAZ" not in b                             # no hardcoded prefix


def test_key_detection_url_and_bare_and_gated_prefix():
    b = _fn_body("_frzJiraKeyFromQuery")
    assert "browse" in b                                # pasted Jira browse URL
    assert "trim()" in b                                # whitespace tolerant
    assert "toUpperCase()" in b                         # case tolerant
    assert "_frzJiraPrefixes()" in b                    # bare key gated to a configured prefix
    # bare-key regex is anchored to the WHOLE query so an embedded key never hijacks text search
    assert r"^([A-Za-z][A-Za-z0-9_]*)-(" in b and "$/" in b


def test_resolve_primary_rule():
    b = _fn_body("_frzResolveJira")
    assert "_val('projects'" in b                       # resolves over the in-memory projects[]
    assert "p.jiraTickets" in b
    assert "!p.hidden" in b                             # hidden synced-children set aside
    assert "vis.length===1" in b and "jump" in b        # single non-hidden -> jump
    assert "list" in b                                  # else -> list


def test_global_search_uses_lookup_then_falls_through():
    b = _fn_body("frzGlobalSearch")
    assert "_frzJiraKeyFromQuery(q)" in b
    assert "_frzResolveJira(" in b
    assert "_frzSearchJump(res.jump)" in b              # jump reuses the existing jump path
    assert "/api/items?q=" in b                          # falls through to text search when nothing linked


def test_inmemory_filter_parity():
    b = _fn_body("itemMatchesSearch")
    assert "p.jiraTickets" in b                          # 2.3 parity: key filters Gantt/Kanban too
    # ordinary-text fields still present (unchanged behavior)
    assert "p.itemKey" in b and "p.name" in b and "p.description" in b


def test_read_only_no_new_endpoint():
    # Option B first pass is client-only: no server route added for the lookup
    assert "by-jira" not in _html()


def test_search_header_css_present():
    assert ".frz-beta .frz-search-hd {" in _html()
