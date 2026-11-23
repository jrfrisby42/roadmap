"""RB-FILTER-1 (J.R. 2026-11-23: "if the item is a support item but has been worked on by the devs, can we have
that item show up if the filter for support OR the dev team that worked on it is selected").

On Planning > Releases a finished Support item Resolved by a dev Team matches that dev Team in the Team chip:
the release list + an opened release include it, and Shipped lists it in the "Also fixed for Support" group
(now for any picked dev Teams, not only a single one). Picking Support keeps it in the main list, never twice.
The Plan backlog and Sprints are unchanged. Source guards; behaviour checked by running the functions in node.
"""
import pathlib
import re

HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def _fn(name):
    m = re.search(r"function " + re.escape(name) + r"\(.*?\n  \}", HTML, re.DOTALL)
    assert m, name
    return m.group(0)


def test_dev_team_pick_credits_the_resolving_team_only():
    assert "function _rbCredits(rb, f){ return !!(f.dev && rb && rb.by==='dev' && rb.team && f.dev.indexOf(rb.team)>=0); }" in HTML
    body = _fn("_planFilterPassRb")
    assert "if(_planFilterPass(p, f, team)) return true;" in body
    assert "g.dev=null;" in body and "return _planFilterPass(p, g, team);" in body   # every other chip still applies


def test_release_list_and_opened_release_use_it():
    assert "_planFilterPassRb(p, rf, null, p.resolvedBy)" in _fn("renderReleasesTab")
    assert "_planFilterPassRb(p, rf, null, p.resolvedBy)" in _fn("fillReleaseDetail")


def test_shipped_also_fixed_group_works_for_any_picked_dev_teams():
    body = _fn("_rbAlsoFixed")
    assert "if(!f.dev) return { items:[] };" in body
    assert "!inPool[x.id] && _rbMatch(x) && _planFilterPassRb(" in body          # never shown twice
    assert "_shipFocusTeam()" not in body                                          # no longer only one Team


def test_backlog_and_sprints_are_unchanged():
    assert "return _planFilterPass(p, f);" in _fn("backlogItems")
    assert HTML.count("_planFilterPassRb(") == 4                                    # def + 2 release + Shipped
