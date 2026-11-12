"""SHIPPED-1: the Releases tab's Shipped section (GET /api/shipped) counts with the exec deck's own rules."""
import json
import pathlib
from datetime import date

import server

TODAY = date(2026, 11, 12)


def _cfg(team):
    with server.db(team) as c:
        c.execute("INSERT OR REPLACE INTO config(key,value) VALUES('statusIsTerminal',?)", (json.dumps({"Released": True}),))


def _put(team, pid, **p):
    with server.db(team) as c:
        c.execute("INSERT INTO projects(id,data) VALUES(?,?)", (pid, json.dumps(p)))


def _seed(team):
    _cfg(team)
    _put(team, 1, name="Portal", type="Roadmap Item", status="In Progress", dev="Wasatch")
    _put(team, 2, name="Login page", type="Story", status="Released", parent=1, releaseDate="2026-11-03", itemKey="FRZ-2", storyPoints=3)
    _put(team, 3, name="Fix crash", type="Bug", status="Released", completedAt="2026-11-05T16:00:00+00:00", dev="Uinta")   # no Jira date: Flow's
    _put(team, 4, name="Old thing", type="Story", status="Released", parent=1, releaseDate="2026-10-20")
    _put(team, 5, name="Dupe", type="Story", status="Duplicate", releaseDate="2026-11-04")           # reserved terminal: never counted
    _put(team, 6, name="Hidden copy", type="Story", status="Released", hidden=True, releaseDate="2026-11-04")
    _put(team, 7, name="Archived", type="Story", status="Released", archived=True, releaseDate="2026-11-04")
    _put(team, 8, name="Not done", type="Story", status="In Progress", parent=1)


def test_month_grouped_by_project_with_deck_rules(team):
    _seed(team)
    m = server._shipped_model(team, None, TODAY)
    assert m["month"] == "2026-11"
    got = {x["id"]: x for x in m["items"]}
    assert set(got) == {2, 3}
    assert got[2]["project"] == "Portal" and got[2]["projectId"] == 1 and got[2]["team"] == "Wasatch"   # team inherited from the project
    assert got[3]["project"] == "" and got[3]["shipped"] == "2026-11-05"
    assert m["points"] == 3.0
    assert [x["id"] for x in m["items"]] == [3, 2]   # newest first


def test_other_month_and_month_list(team):
    _seed(team)
    m = server._shipped_model(team, "2026-10", TODAY)
    assert [x["id"] for x in m["items"]] == [4]
    assert m["months"][:2] == [{"ym": "2026-11", "count": 2}, {"ym": "2026-10", "count": 1}]
    assert len(m["months"]) == 2   # no empty tail before the Org's first shipped month


def test_counts_match_the_exec_deck(team):
    _seed(team)
    deck = server._exec_model(team, TODAY)["delivered"]["perMonth"]
    ship = {x["ym"]: x["count"] for x in server._shipped_model(team, None, TODAY)["months"]}
    assert all(ship.get(d.strftime("%Y-%m"), 0) == n for d, n in deck)


def test_endpoint_roles_and_validation(client, admin_headers, team):
    _seed(team)
    r = client.get("/api/shipped?month=2026-10", headers=admin_headers)
    assert r.status_code == 200 and [x["id"] for x in r.json()["items"]] == [4]
    assert client.get("/api/shipped?month=nov", headers=admin_headers).status_code == 422
    for role, code in (("viewer", 200), ("contributor", 403)):
        tok = server.create_token(team, "u_" + role, role)
        h = {"Authorization": "Bearer " + tok, "X-Team": team}
        assert client.get("/api/shipped", headers=h).status_code == code, role


_HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8")


def test_releases_tab_leads_with_shipped():
    body = _HTML.split("function renderReleasesTab(host){", 1)[1].split("\n  }\n", 1)[0]
    assert 'id="frzShip"' in body and "_shipLoad(document.getElementById('frzShip'));" in body
    assert body.index('id="frzShip"') < body.index('id="frzNewRelease"')
    assert "API.get('/api/shipped'" in _HTML
