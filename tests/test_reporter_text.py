"""REPORTER-TEXT-1 regression guard: a free-text substring filter on `reporter`.

One server param (`reporter`, a column LIKE substring on the indexed reporter column) + a free-text
`+ Filter` chip (no panel) that round-trips through the URL. As a URL filter param it takes the
FIELDS-CARRY-2 four-site carry/express treatment PLUS the define/offer sites - the guard is written
against the FULL set the first time (FIELDS-CARRY-2 took three deploys because its enumeration grew
across deploys).

SOURCE-SHAPE guards: they prove every registration site carries `reporter` and the server clause is a
column LIKE. They do NOT prove the filter returns the right rows or that the chip paints - that is
tests/reporter_text_checks.js + the live pass. server.py and roadmap.html both change this stage.
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROADMAP = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"


def _html() -> str:
    return ROADMAP.read_text(encoding="utf-8", errors="replace")


def _py() -> str:
    return SERVER.read_text(encoding="utf-8", errors="replace")


def test_server_param_is_column_like_substring():
    src = _py()
    assert re.search(r"reporter: Optional\[str\] = None,", src), "list_items must accept a reporter param"
    # a column LIKE substring on the indexed reporter column - NOT json_extract, NOT eq (exact)
    assert 'where.append("reporter LIKE ?")' in src, "reporter must filter via a column LIKE"
    assert 'params.append(f"%{reporter.strip()}%")' in src, "reporter LIKE must be a %substring% match"
    # must not touch q / the FTS index - the schema stays the exact three columns (no reporter)
    assert "fts5(item_key, name, description)" in src, "the FTS schema must be unchanged (no reporter)"
    assert "fts5(item_key, name, description, reporter)" not in src, "reporter must NOT be added to the FTS index"
    assert "'$.reporter'" not in src, "reporter must be a column filter, not a json_extract blob filter"


def test_chip_defined_freetext_no_panel():
    src = _html()
    m = re.search(r"reporter:\s*\{ label:'Reporter', freetext:true, param:'reporter',(.*?)set:function\(v\)\{ state\.reporterFilter", src, re.DOTALL)
    assert m, "CHIPS.reporter must be a freetext chip with get/set on state.reporterFilter"
    assert "panel:" not in m.group(0), "the reporter chip must have NO panel (free-text)"


def test_all_four_carry_and_express_sites():
    src = _html()
    # 1. syncURL carry set
    assert re.search(r"'blockedReason','reporter'\]\.forEach\(function\(pk\)", src), "syncURL carry set must include reporter"
    # 2. syncURL _selfSer-where-offered
    assert re.search(r"'blockedReason','reporter'\]\.forEach\(function\(x\)\{ if\(CFG\[view\]\.chips", src), "_selfSer must include reporter"
    # 3. _frzCarryQS keys
    assert re.search(r"'blockedReason','reporter'\] : \['project'\]", src), "_frzCarryQS keys must include reporter"
    # 4. _frzViewExpressesSaved appliedP
    assert re.search(r"'blockedReason','reporter'\]\.forEach\(function\(x\)\{ if\(\(CFG\[view\]\.chips", src), "_frzViewExpressesSaved must include reporter"


def test_define_and_offer_sites():
    src = _html()
    assert "var _XCHIP = ['priority','dept','location','resolutionType','blockedReason','reporter']" in src, "_XCHIP must include reporter"
    assert re.search(r"list:\s*\{ chips:\[[^\]]*\], extra:\[[^\]]*'blockedReason','reporter'\]", src), "CFG.list.extra must include reporter"


def test_listquery_actually_sends_reporter():
    # THE site that makes the filter act: _listQuery (the List's own fetch builder) must send `reporter`.
    # reporter is a beta state var (no classic panel), so it is bridged to the classic script via
    # window._frzReporterFilter. Without this the chip shows and the URL carries it but the rendered List
    # does NOT filter - the defect caught in the 6.38.0 live pass.
    src = _html()
    assert "window._frzReporterFilter = function()" in src, "state.reporterFilter must be bridged to the classic script"
    assert "params.set('reporter', _rep)" in src, "_listQuery must send reporter to /api/items"


def test_freetext_apply_menu_and_clear_branches():
    src = _html()
    # apply-from-URL free-text branch (never split)
    assert "if (CHIPS[k].freetext){ CHIPS[k].set(v||''); return; }" in src, "applyURLToFilters needs a free-text set branch"
    # chipMenu dispatches free-text to the text-input popover
    assert "if (CHIPS[key] && CHIPS[key].freetext) return reporterMenu(key, anchor);" in src, "chipMenu must route free-text to reporterMenu"
    assert "function reporterMenu(key, anchor)" in src, "the reporterMenu popover must exist"
    # both clear paths (Clear-all + the chip x) reset the free-text state var
    assert src.count("CHIPS[k].freetext){ CHIPS[k].set(''); }") >= 2, "both clear paths must reset the free-text chip"
