"""JIRA-COL-1 regression guard: the optional, per-Space-gated Jira List column.

Adds a Jira-ticket column to the List (and, via _paintHome -> _listRowHtml, to My Home
Assigned/Watching/Recent - no My Home code change). It is default-hidden (_optin7), gated OUT of the
order ENTIRELY (order/header/picker/render/export) unless Jira sync is enabled AND the current Space
scope has a project mapping, and its first key links via the per-issue cached URL (it.jiraCache[key].url -
the same source renderJiraIssueCard uses; NO hardcoded host, plain text when uncached).

SOURCE-SHAPE guards: they prove the pieces exist and are wired, not that the column renders right (that's
tests/jira_col_checks.js + the live pass). Each fails when JIRA-COL-1 is reverted (demonstrated vs HEAD =
6.36.3). server.py is intentionally untouched by this stage (list_items already returns the full blob).
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src() -> str:
    return ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")


def test_col_def_registered():
    src = _src()
    # a 'jira' entry in _LIST_COL_DEF with sort:null (no server sort) rendering via _listJira
    assert re.search(r"jira:\s*\{\s*label:'Jira',\s*sort:null,", src), "jira column missing from _LIST_COL_DEF"
    assert re.search(r'data-col="jira"[^>]*>\$\{_listJira\(it\)\}', src), "jira cell must render via _listJira"


def test_default_order_includes_jira():
    m = re.search(r"const _LIST_DEFAULT_ORDER = \[([^\]]*)\]", _src())
    assert m and "'jira'" in m.group(1), "_LIST_DEFAULT_ORDER must include 'jira'"


def test_cell_links_via_cache_no_hardcoded_host():
    src = _src()
    m = re.search(r"function _listJira\(it\)\{(.*?)\n\}", src, re.DOTALL)
    assert m, "_listJira renderer not found"
    body = m.group(1)
    # links the first key via the per-issue cached URL; +N and title from the key list
    assert "it.jiraTickets" in body and "jiraCache" in body, "_listJira must read jiraTickets + jiraCache"
    assert "cache[first] && cache[first].url" in body, "_listJira must link via the cached per-issue url"
    # no hardcoded Jira host anywhere in the renderer
    assert "atlassian.net" not in body and "https://" not in body, "no hardcoded Jira host in the cell"


def test_a4_plain_case_has_pending_sync_title():
    # A4: the uncached (plain-text) first key carries a title explaining the link is pending a sync, so two
    # adjacent rows differing in clickability are explained. The linked <a> case must NOT carry that title.
    src = _src()
    m = re.search(r"function _listJira\(it\)\{(.*?)\n\}", src, re.DOTALL)
    assert m, "_listJira not found"
    body = m.group(1)
    assert re.search(r"<span title=\"Jira link available after this issue syncs\" style=\"font-family:monospace\">'\+esc\(first\)", body), \
        "plain (uncached) first key must carry the pending-sync title"
    # the linked branch is a bare <a> (no per-element title); the full-key-list title stays on the outer span
    assert "ks.join(', ')" in body, "outer title must still list every key (incl. behind +N)"


def test_gate_helper_and_filter():
    src = _src()
    # the gate: sync enabled + scope mapping (specific Space maps, or All Spaces => any maps)
    m = re.search(r"function _frzJiraColAvailable\(\)\{(.*?)\n\}", src, re.DOTALL)
    assert m, "_frzJiraColAvailable not found"
    g = m.group(1)
    assert "_jiraSyncConfigData" in g and "cfg.enabled" in g, "gate must require jiraSyncConfig.enabled"
    assert "_jiraProjectMappingData" in g, "gate must consult the project mapping"
    assert "_frzListScope" in g, "gate must read the current Space scope"
    # applied in _listColOrderAll so the column is absent ENTIRELY (order/header/picker/render), not just hidden
    assert re.search(r"if\(!_frzJiraColAvailable\(\)\) saved = saved\.filter\(id => id!=='jira'\);", src), \
        "_listColOrderAll must drop 'jira' when the gate is off"


def test_scope_exposed_to_classic():
    # the beta IIFE must expose state.project as window._frzListScope so the classic gate can read it
    assert re.search(r"window\._frzListScope = function\(\)\{[^}]*state\.project \|\| '__all__'", _src()), \
        "window._frzListScope must expose state.project (default '__all__')"


def test_optin7_default_hidden():
    # jira seeds into the HIDDEN set once via _optin7 (default hidden; opt-in survives via the separate key)
    assert re.search(r"_optin7'\)\)\{ set\.add\('jira'\);", _src()), "jira must default-hide via _optin7"


def test_export_registered_and_gated():
    src = _src()
    assert re.search(r"jira:\{label:'Jira', get:function\(it\)\{ return \(Array\.isArray\(it\.jiraTickets\)", src), \
        "jira missing from _exportColDefs"
    # present in the 'all' order AND gated out of the full export when unavailable
    m = re.search(r"var allCols=\[([^\]]*)\]", src)
    assert m and "'jira'" in m.group(1), "'jira' missing from the export 'all' order"
    assert re.search(r"if\(!_frzJiraColAvailable\(\)\) allCols=allCols\.filter\(function\(k\)\{ return k!=='jira'", src), \
        "the full export must drop 'jira' when the gate is off"
