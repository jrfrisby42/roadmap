"""HOME-HEAD-1 regression guard: My Home's header is built from the same source as its cells.

Defect: My Home's desktop header was a fixed 10-cell static list that did not track the dynamic
_listRowHtml columns, so once optional columns were un-hidden the labels MISLABELED the cells
(Reporter under "Pts", Jira under "Sprint", Space/Created unlabeled). Fix: render My Home's header
via the SAME _renderListHeader builder the List uses (labels from _LIST_COL_DEF), with a blank hidden
lead cell and plain non-sortable ths, snapshotting the same _listColsRenderCache the rows read.

SOURCE-SHAPE guards: they prove the header is derived from _LIST_COL_DEF via the reused builder, NOT
that the labels line up per column (that is tests/home_head_checks.js + the live pass). Each fails when
HOME-HEAD-1 is reverted (demonstrated vs HEAD = 6.37.0). server.py is untouched by this stage.
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src() -> str:
    return ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")


def test_builder_parameterized_not_forked():
    src = _src()
    # ONE header builder, now parameterized (hr, leadTh, thPlain) - reuse, not a second function
    assert re.search(r"function _renderListHeader\(hr, leadTh, thPlain\)\{", src), \
        "_renderListHeader must be parameterized for reuse"
    assert src.count("function _renderListHeader") == 1, "there must be exactly one header builder (no fork)"
    # labels still come from _LIST_COL_DEF via _listTh - no parallel label list
    assert re.search(r"_listColsRenderCache\.map\(id => _listTh\(id, thPlain \? \{plain:true\} : null\)\)", src), \
        "header cells must be built from _listTh (labels from _LIST_COL_DEF)"


def test_default_call_unchanged_for_list():
    # the List still calls it with no args; defaults must reproduce the prior behavior
    src = _src()
    assert "hr = hr || document.getElementById('listHeadRow')" in src, "hr must default to the List head row"
    assert "id=\"listSelectAll\"" in src, "the default lead cell must still be the List's select-all checkbox"


def test_listth_plain_mode():
    src = _src()
    m = re.search(r"function _listTh\(id, opts\)\{(.*?)\n\}", src, re.DOTALL)
    assert m, "_listTh must accept opts"
    body = m.group(1)
    assert "const plain = !!(opts && opts.plain);" in body, "plain mode flag missing"
    # plain => never sortable, never resizable, always data-col
    assert "!plain && c.sort" in body, "plain must suppress the sortable th branch"
    assert "(id==='name'||id==='description') && !plain" in body, "plain must suppress resizing"
    assert "(resizable||plain)?` data-col=" in body, "plain ths must always carry data-col (for identity match)"


def test_my_home_header_row_and_paint_call():
    src = _src()
    assert 'id="frzHomeHeadRow"' in src, "My Home header <tr> must be identifiable"
    # _paintHome renders the dynamic header with a blank hidden lead cell + plain ths, before the rows
    assert re.search(r"var hr=document\.getElementById\('frzHomeHeadRow'\);\s*\n\s*if\(hr\) _call\('_renderListHeader', hr, '<th class=\"frz-mw-cb\"></th>', true\);", src), \
        "_paintHome must render the My Home header via the reused builder (blank lead + plain)"


def test_mobile_media_query_untouched():
    # the ≤640px headerless rule must remain, INSIDE a max-width:640px media block
    src = _src()
    assert ".frz-beta #listView thead, .frz-beta .frz-mw-table thead { display:none; }" in src, \
        "the mobile headerless rule must stay verbatim"
    # the display:none rule must sit after a max-width:640px media open and be the mobile one
    idx = src.index(".frz-beta .frz-mw-table thead { display:none; }")
    assert "max-width:640px" in src[:idx], "the thead display:none rule must remain mobile-scoped"


def test_todos_still_returns_before_painthome():
    # To-dos must still short-circuit before _paintHome (untouched renderer)
    assert re.search(r"if\(tab==='todos'\)\{ _frzRenderTodos\(\); return; \}", _src()), \
        "To-dos must return before _paintHome (renderer untouched)"
