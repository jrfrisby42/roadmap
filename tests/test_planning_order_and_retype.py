"""PLAN-TABS-1 (Planning tabs: Releases, Sprints, Plan; /planning = Releases), SHIP-TEAM-OFF-1 (Shipped has
no Team picker of its own; the filter bar narrows it) and CHECKLIST-RETYPE-1 (changing an item to a Type with a
checklist template adds the steps when it has none - how IT's portal tickets, which come in as Task, get their
Onboarding/Offboarding steps)."""
import json
import pathlib

import server

HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def test_planning_tabs_are_releases_sprints_plan_and_releases_is_default():
    i_rel, i_spr, i_plan = HTML.index('data-plantab="releases"'), HTML.index('data-plantab="sprints"'), HTML.index('data-plantab="plan"')
    assert i_rel < i_spr < i_plan
    assert "planTab:'releases'," in HTML
    assert HTML.count("var pm=path.match(/^\\/planning\\/(sprints|releases|plan)/); state.planTab = pm ? pm[1] : 'releases';") == 3
    assert "navigate('/planning'+(t==='releases'?'':'/'+t), false);" in HTML
    assert "state.planTab!=='releases') base = '/planning/'+state.planTab;" in HTML


def test_shipped_has_no_team_picker():
    assert "data-shipteam" not in HTML and "_ship.team" not in HTML
    assert "function _shipInTeam(d){ return _shipPool(d); }" in HTML


def _setup(team):
    with server.db(team) as c:
        c.execute("INSERT OR REPLACE INTO config(key,value) VALUES('types',?)", (json.dumps([
            {"name": "Task"}, {"name": "Onboarding", "checklist": ["Laptop", {"text": "Parking", "optional": True}]}]),))


def _create(client, h, **kw):
    r = client.post("/api/projects", json={"name": "New hire Sam", "type": "Task", "status": "New", **kw}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_retype_to_a_templated_type_adds_the_steps(client, team, admin_headers):
    _setup(team)
    it = _create(client, admin_headers)
    assert not it.get("checklist")
    it["type"] = "Onboarding"
    r = client.put(f"/api/projects/{it['id']}", json=it, headers=admin_headers)
    assert r.status_code == 200, r.text
    cl = r.json()["checklist"]                                   # returned, so the page shows it without a reload
    assert [s["text"] for s in cl] == ["Laptop", "Parking"] and cl[1]["optional"] is True and not any(s["done"] for s in cl)


def test_retype_keeps_existing_steps_and_same_type_save_adds_nothing(client, team, admin_headers):
    _setup(team)
    it = _create(client, admin_headers, checklist=["My own step"])
    it["type"] = "Onboarding"
    out = client.put(f"/api/projects/{it['id']}", json=it, headers=admin_headers).json()
    assert [s["text"] for s in out["checklist"]] == ["My own step"]
    plain = _create(client, admin_headers, type="Onboarding")      # created as Onboarding (steps sent, then removed)
    client.put(f"/api/items/{plain['id']}/checklist", json={"steps": []}, headers=admin_headers)
    plain = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"] if p["id"] == plain["id"])
    plain["name"] = "renamed"                                     # a save that does not change the Type
    assert not client.put(f"/api/projects/{plain['id']}", json=plain, headers=admin_headers).json().get("checklist")
