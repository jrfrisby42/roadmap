"""RELEASES-FILTER-1: Planning > Releases follows the filter bar (and Plan picks up the Assignee chip).

The Releases tab read items straight from memory and ignored every chip. Now one check
(_planFilterPass) is shared by the Plan backlog, the Shipped section and the release list, so a
Team / Status / Type / Assignee / Priority / Department pick or a rail Space narrows all of them.
Mark released, release notes and feature flags still use every item in the release.
Source-shape guards; behaviour was verified headlessly. These fail when the stage is reverted.
"""
import pathlib
import re

HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src():
    return HTML.read_text(encoding="utf-8", errors="replace")


def _fn(src, name):
    m = re.search(r"function " + re.escape(name) + r"\(.*?\n  \}", src, re.DOTALL)
    assert m, name + " not found"
    return m.group(0)


def test_one_shared_check_covers_every_planning_chip():
    body = _fn(_src(), "_planFilterPass")
    for needle in ("inScope(p)", "f.dev", "f.type", "f.st", "f.asg", "priorityMatches(p, f.prio)", "departmentMatches(p, f.dept)"):
        assert needle in body, needle


def test_plan_backlog_uses_the_shared_check():
    assert "return _planFilterPass(p, f);" in _fn(_src(), "backlogItems")


def test_shipped_follows_the_filters_on_the_effective_team():
    src = _src()
    assert "_planFilterPass(p, f, x.team||'')" in _fn(src, "_shipPool")
    assert "function _shipInTeam(d){ return _shipPool(d); }" in src     # SHIP-TEAM-OFF-1: no Team picker; the filter bar narrows Shipped
    assert "_shipInTeam(d).filter(_rbMatch)" in src                      # RESOLVED-BY-1: _shipVisible = pool + the Resolved by filter
    assert "var vis=_shipVisible(d);" in _fn(src, "_shipPaint")


def test_release_list_and_detail_follow_the_filters():
    src = _src()
    tab = _fn(src, "renderReleasesTab")
    assert "_planFilterPassRb(p, rf, null, p.resolvedBy)" in tab and "prBy[r.id].total>0" in tab
    det = _fn(src, "fillReleaseDetail")
    assert "shownItems.map(" in det and "(filters on)" in det
    # flags + notes + Mark released keep the whole release
    assert "items.map(function(p){ return Array.isArray(p.featureFlags)" in det
    assert "var pr=releaseProgress(r);" in _fn(src, "markReleased")
