"""SPRINTS-FILTER-1: Planning > Sprints follows the filter bar, like Releases (RELEASES-FILTER-1).

sprintStats(sp, f) narrows a sprint to the items matching the shared Planning filter check
(_planFilterPass); a completed sprint's snapshot rows are tested against the item as it is now.
Sprints with no matching items are hidden with a note; an opened sprint says "Showing N of M".
Display only: completing a sprint still uses every item (sprintItems). Source-shape guards;
behaviour was verified headlessly. These fail when the stage is reverted.
"""
import pathlib
import re

HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _fn(name):
    src = HTML.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"function " + re.escape(name) + r"\(.*?\n  \}", src, re.DOTALL)
    assert m, name + " not found"
    return m.group(0)


def test_sprint_stats_take_the_filters_for_live_and_snapshot_sprints():
    b = _fn("sprintStats")
    assert b.startswith("function sprintStats(sp, f)")
    assert b.count("_planFilterPass(p, f)") == 2          # snapshot rows + live items
    assert "carry:(on ? it.length-doneS.length : (sp.carryOver||0))" in b


def test_sprints_tab_filters_rows_and_detail():
    b = _fn("renderSprintsTab")
    assert "sprintStats(sp, sf)" in b and "stBy[sp.id].total>0" in b
    assert "hidden by the filters" in b
    assert "sprintStats(sp, _planFilters())" in b and "(filters on)" in b
    assert "st.carry" in b and "(sp.carryOver||0)+'</div>'" not in b


def test_completing_a_sprint_still_uses_every_item():
    assert "var allItems=sprintItems(sp.id);" in _fn("doCompleteSprint")


def test_plan_tab_sprint_pane_follows_the_filters():
    # SPRINT-PANE-FILTER-1: the Plan tab's sprint pane narrows its cards + counts like the backlog beside it.
    b = _fn("sprintPaneHTML")
    assert "allItems = sprintItems(sprint.id)" in b
    assert "_planFilterPass(p, pf)" in b and "(filters on)" in b
    assert "No items in this sprint match the filters." in b
