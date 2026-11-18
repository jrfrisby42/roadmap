"""RELEASE-FROM-SHIPPED-1 (make a release from a month of Shipped work; the Releases list is the history),
GANTT-OW-CREDIT-1 (dev Teams get Other work credit for Support items they resolved) and GANTT-SPRINT-DATES-1
(a story with no dates lends its sprint's dates to its project's bar)."""
import json
import pathlib

import server

HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def _items(team, n):
    ids = []
    with server.db(team) as c:
        for i in range(n):
            ids.append(server._insert_project(c, {"name": f"x{i}", "type": "Story", "status": "New"}))
        c.execute("INSERT OR REPLACE INTO config(key,value) VALUES('releases',?)",
                  (json.dumps([{"id": "rel1", "name": "26.11.30", "state": "Released"}]),))
    return ids


def test_bulk_links_a_release_and_returns_new_modified_times(client, team, admin_headers):
    ids = _items(team, 3)
    r = client.post("/api/items/bulk", json={"ids": ids, "patch": {"release": "rel1"}}, headers=admin_headers)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["updated"] == 3 and set(out["updatedTs"]) == {str(i) for i in ids}
    with server.db(team) as c:
        for i in ids:
            row = c.execute("SELECT data, updated_ts FROM projects WHERE id=?", (i,)).fetchone()
            assert json.loads(row["data"])["release"] == "rel1" and out["updatedTs"][str(i)] == row["updated_ts"]
    assert client.post("/api/items/bulk", json={"ids": ids[:1], "patch": {"release": ""}}, headers=admin_headers).status_code == 200


def test_bulk_release_must_exist(client, team, admin_headers, viewer_headers):
    ids = _items(team, 1)
    assert client.post("/api/items/bulk", json={"ids": ids, "patch": {"release": "nope"}}, headers=admin_headers).status_code == 422
    assert client.post("/api/items/bulk", json={"ids": ids, "patch": {"release": 5}}, headers=admin_headers).status_code == 422
    assert client.post("/api/items/bulk", json={"ids": ids, "patch": {"release": "rel1"}}, headers=viewer_headers).status_code == 403


def test_create_release_from_shipped():
    assert "data-shiprel" in HTML and "function _relPickGo(sec, d, btn)" in HTML
    assert "API.post('/api/items/bulk', { ids:ids, patch:{ release:rel.id } })" in HTML
    assert "rel={ id:'rel'+Date.now(), name:name, targetDate:end, releasedDate:end, state:'Released' };" in HTML
    assert "r.notes=_frzGenReleaseNotes(rel.id)" in HTML                             # notes generated on create
    assert "return e.slice(2,4)+'.'+e.slice(5,7)+'.'+e.slice(8,10);" in HTML           # 26.11.30 names
    assert "if(ts[p.id]) p.updated_ts=ts[p.id];" in HTML                               # no false conflict after linking


def test_releases_list_is_history_only():
    body = HTML.split("function renderReleasesTab(host){", 1)[1].split("\n  }\n", 1)[0]
    for gone in ("data-relship", "data-relreopen", "frzNewRelease", "+ New release"):
        assert gone not in body
    assert "+ Add items</button>" not in HTML.split("function fillReleaseDetail(id, det){", 1)[1].split("\n  }\n", 1)[0]


def test_gantt_credits_dev_teams_and_backstops_with_sprints():
    assert "const rb = p.resolvedBy, ct = (rb && rb.by === 'dev' && rb.team) || '';" in HTML
    assert '<i class="ow-sup" style="height:${sPct}%"></i>' in HTML
    assert "const _sb = c => (c.revised || ((c.start || c.due) && !c.startAuto)) ? null : _sprintSpanOf(c);" in HTML   # + GANTT-SPRINT-GUESS-1
    assert "function _sprintSpanOf(c){" in HTML


def test_shipped_collapses_and_remembers_per_browser():
    # SHIP-COLLAPSE-1: expanded by default; folding it leaves one line so the Releases list is in reach.
    assert "function _shipCollapsed(){ try { return localStorage.getItem(_SHIP_COL_KEY)==='1'; } catch(e){ return false; } }" in HTML
    assert "if(_shipCollapsed()){" in HTML and "_shipTog(false)+" in HTML


def test_release_list_dates_by_target():
    # REL-TARGET-DATE-1: "Released <date>" is the target (month end), not the day "Mark released" was clicked.
    body = HTML.split("function renderReleasesTab(host){", 1)[1].split("\n  }\n", 1)[0]
    assert "(shownState==='Released' ? 'Released ' : 'Target ')+fmtD(r.targetDate||r.releasedDate)" in body
    assert "(b.targetDate||b.releasedDate||'').localeCompare(a.targetDate||a.releasedDate||'')" in body


def test_release_state_follows_its_items():
    # REL-STATE-1: all items finished -> shows Released; adding Shipped work to an existing release releases it.
    body = HTML.split("function renderReleasesTab(host){", 1)[1].split("\n  }\n", 1)[0]
    assert "var shownState=(r.state!=='Released' && full.total>0 && full.done===full.total) ? 'Released' : r.state;" in body
    assert "if(r && r.state!=='Released'){ r.state='Released'; r.releasedDate=r.targetDate||end; dirty=true; }" in HTML


def test_shipped_rows_mark_items_already_in_a_release():
    # SHIP-REL-MARK-1: a badge with the release name, not a tail on the grey meta text
    html = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8", errors="replace")
    assert "var relMark = inRel ? '<span class=\"frz-ship-relmark\" title=\"In release '+escA(inRel.name)+'\">✓ '+esc(inRel.name)+'</span>' : '';" in html
    assert "if(inRel) meta += ' · '+inRel.name;" not in html
    assert ".frz-beta .frz-ship-relmark {" in html and "background:var(--frz-accent-soft)" in html
