"""RESOLVED-BY-1: finished items on the Teams in `resolvedByTeams` (e.g. Support) get a Resolved by tag
(Support / Development) from their Jira ticket; a person's pick always wins. Classifier rules, the person
endpoint, the sync/backfill fill (Jira stubbed), Shipped, config, and client source guards."""
import json
import pathlib
from datetime import date, timedelta

import server

HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def _seed(team, tagged=("Support",)):
    users = [{"username": "admin", "role": "admin", "builtin": True},
             {"username": "jacob.smith", "role": "editor", "firstName": "Jake", "lastName": "Smith", "ownerFilter": "Support"},
             {"username": "henry.doan", "role": "editor", "firstName": "Henry", "lastName": "Doan", "ownerFilter": "Wasatch"},
             {"username": "dan.hill", "role": "admin", "firstName": "Dan", "lastName": "Hill"}]
    with server.db(team) as c:
        for k, v in (("users", users), ("developers", ["Wasatch", "Weto", "Support"]),
                     ("statuses", ["New", "In Progress", "Released"]), ("statusIsTerminal", {"Released": True}),
                     ("jiraAssigneeMap", {"acc-henry": "henry.doan", "acc-jake": "jacob.smith"}),
                     ("resolvedByTeams", list(tagged))):
            c.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (k, json.dumps(v)))


def _item(team, **kw):
    p = {"name": "x", "type": "Support", "status": "Released", "dev": "Support",
         "releaseDate": (date.today() - timedelta(days=2)).isoformat()}
    p.update(kw)
    with server.db(team) as c:
        return server._insert_project(c, p)


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _issue(path=(), assignees=(), current=None, comments=()):
    hist = [{"created": "2026-01-0%dT00:00:00" % (i + 1), "author": {"displayName": "x"},
             "items": [{"field": "status", "toString": s}]} for i, s in enumerate(path)]
    hist += [{"created": "2026-02-0%dT00:00:00" % (i + 1), "author": {"displayName": "x"},
              "items": [{"field": "assignee", "to": a[0], "toString": a[1]}]} for i, a in enumerate(assignees)]
    return {"id": "1", "fields": {"assignee": current, "status": {"name": "Done"},
                                  "comment": {"comments": [{"author": {"accountId": a, "displayName": n}} for a, n in comments]}},
            "changelog": {"histories": hist}}


def _code(n=1):
    return {"commit": {"overall": {"count": n}}, "pullrequest": {"overall": {"count": 0}}}


# ── classifier ───────────────────────────────────────────────────────────────
def test_linked_code_is_development_with_the_developers_team(team):
    _seed(team)
    ctx = server._rb_context(team)
    r = server._rb_classify(_issue(assignees=[("acc-henry", "Henry Doan")]), _code(2), ctx)
    assert r["by"] == "dev" and r["sure"] and r["team"] == "Wasatch" and r["who"] == "Henry Doan"
    assert r["why"] == "Code linked in Jira: 2 commits."


def test_code_review_is_development(team):
    _seed(team)
    r = server._rb_classify(_issue(path=["In Dev", "Code Review", "Done"]), {}, server._rb_context(team))
    assert r["by"] == "dev" and r["sure"] and r["why"] == "Went through Code Review in Jira."


def test_no_developer_is_support(team):
    _seed(team)
    # Jake (Support Team) and Dan (no Team, QA) are not developers
    iss = _issue(path=["Other Work Done"], assignees=[("acc-jake", "Jake Smith")], current={"accountId": "acc-jake", "displayName": "Jake Smith"},
                 comments=[("x", "Dan Hill")])
    r = server._rb_classify(iss, {}, server._rb_context(team))
    assert r == {"by": "support", "team": "", "who": "", "sure": True, "why": "No developer worked it in Jira."}


def test_developer_without_code_is_dev_but_unsure(team):
    _seed(team)
    r = server._rb_classify(_issue(path=["In Dev", "Other Work Done"], assignees=[("acc-henry", "Henry Doan")]), {}, server._rb_context(team))
    assert r["by"] == "dev" and r["sure"] is False and r["team"] == "Wasatch" and r["why"].startswith("Not sure:")


def test_non_flow_assignee_counts_and_team_is_inferred_from_their_jira_work(team):
    _seed(team)
    for _ in range(3):   # Tyler's other Jira tickets sit on Weto
        _item(team, dev="Weto", type="Story", jiraTickets=["FRAZ-1"], jiraCache={"FRAZ-1": {"assignee": "Tyler Cheney"}})
    r = server._rb_classify(_issue(assignees=[("acc-t", "Tyler Cheney")]), _code(), server._rb_context(team))
    assert r["by"] == "dev" and r["team"] == "Weto" and r["who"] == "Tyler Cheney"


def test_non_flow_commenter_alone_does_not_count(team):
    _seed(team)
    r = server._rb_classify(_issue(path=["Other Work Done"], comments=[("c1", "Some Customer")]), {}, server._rb_context(team))
    assert r["by"] == "support" and r["sure"]


# ── person endpoint ──────────────────────────────────────────────────────────
def test_person_pick_saves_without_touching_updated_ts(client, team, editor_headers, viewer_headers):
    _seed(team)
    pid = _item(team, resolvedBy={"by": "dev", "team": "Wasatch", "who": "Henry Doan", "sure": False, "src": "jira", "why": "w", "at": "2026-01-01T00:00:00+00:00"})
    with server.db(team) as c:
        ts0 = c.execute("SELECT updated_ts FROM projects WHERE id=?", (pid,)).fetchone()["updated_ts"]
    assert client.put(f"/api/items/{pid}/resolved-by", json={"by": "support"}, headers=viewer_headers).status_code == 403
    assert client.put(f"/api/items/{pid}/resolved-by", json={"by": "maybe"}, headers=editor_headers).status_code == 422
    r = client.put(f"/api/items/{pid}/resolved-by", json={"by": "support"}, headers=editor_headers)
    assert r.status_code == 200, r.text
    rb = _blob(team, pid)["resolvedBy"]
    assert rb["by"] == "support" and rb["src"] == "person" and rb["sure"] and rb["setBy"] == "editor1"
    assert rb["team"] == "Wasatch"                                   # kept, shown again if flipped back to dev
    with server.db(team) as c:
        assert c.execute("SELECT updated_ts FROM projects WHERE id=?", (pid,)).fetchone()["updated_ts"] == ts0


def test_person_pick_refused_on_an_untracked_team(client, team, editor_headers):
    _seed(team)
    pid = _item(team, dev="Wasatch")
    assert client.put(f"/api/items/{pid}/resolved-by", json={"by": "dev"}, headers=editor_headers).status_code == 422


def test_full_item_put_cannot_change_it(client, team, admin_headers):
    _seed(team)
    pid = _item(team, resolvedBy={"by": "support", "src": "person", "sure": True})
    it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"] if p["id"] == pid)
    it["resolvedBy"] = {"by": "dev", "src": "person"}
    client.put(f"/api/projects/{pid}", json=it, headers=admin_headers)
    assert _blob(team, pid)["resolvedBy"]["by"] == "support"


def test_recurrence_drops_it():
    assert "resolvedBy" in server.SERVER_OWNED_FIELDS and "resolvedBy" in server.RECURRENCE_SKIP_KEYS


# ── fill (sync + backfill) ───────────────────────────────────────────────────
def _stub_jira(monkeypatch, issues):
    monkeypatch.setattr(server, "jira_configured", lambda: True)

    def fake(method, path, payload=None, timeout=10):
        if path.startswith("/rest/api/3/issue/"):
            key = path.split("/rest/api/3/issue/")[1].split("?")[0]
            if key not in issues:
                raise server.HTTPException(404, "nope")
            return {**issues[key][0], "id": key}
        if path.startswith("/rest/dev-status/"):
            key = path.split("issueId=")[1]
            return {"summary": issues[key][1]}
        raise AssertionError(path)
    monkeypatch.setattr(server, "_jira_req", fake)


def test_fill_tags_from_jira_and_never_overrides_a_person(team, monkeypatch):
    _seed(team)
    a = _item(team, jiraTickets=["FRAZ-1"])
    b = _item(team, jiraTickets=["FRAZ-2"])
    cc = _item(team)                                                       # no Jira ticket
    d = _item(team, jiraTickets=["FRAZ-1"], resolvedBy={"by": "support", "src": "person", "sure": True})
    e = _item(team, status="In Progress", jiraTickets=["FRAZ-1"])         # not finished
    f = _item(team, dev="Wasatch", jiraTickets=["FRAZ-1"])                 # untracked Team
    _stub_jira(monkeypatch, {"FRAZ-1": (_issue(assignees=[("acc-henry", "Henry Doan")]), _code()),
                             "FRAZ-2": (_issue(path=["Other Work Done"]), {})})
    dry = server._resolved_by_fill(team, "t", dry=True)
    assert dry["candidates"] == 3 and dry["dev"] == 1 and dry["support"] == 2 and dry["noJira"] == 1
    assert "resolvedBy" not in _blob(team, a)                              # dry run writes nothing
    out = server._resolved_by_fill(team, "t")
    assert out["written"] == 3
    assert _blob(team, a)["resolvedBy"]["by"] == "dev" and _blob(team, a)["resolvedBy"]["team"] == "Wasatch"
    assert _blob(team, b)["resolvedBy"]["by"] == "support"
    assert _blob(team, cc)["resolvedBy"]["why"] == "No Jira ticket."
    assert _blob(team, d)["resolvedBy"]["src"] == "person"
    assert "resolvedBy" not in _blob(team, e) and "resolvedBy" not in _blob(team, f)
    # a second run right away changes nothing (re-checks wait about a day)
    again = server._resolved_by_fill(team, "t")
    assert again["candidates"] == 0 and again["written"] == 0
    with server.db(team) as c:
        assert c.execute("SELECT COUNT(*) n FROM audit_log WHERE action='resolved-by:jira'").fetchone()["n"] == 1


def test_fill_respects_the_window_limit_and_off_switch(team, monkeypatch):
    _seed(team)
    old = _item(team, releaseDate=(date.today() - timedelta(days=400)).isoformat())
    new = _item(team)
    out = server._resolved_by_fill(team, "t", days=365)
    assert out["written"] == 1 and "resolvedBy" not in _blob(team, old) and "resolvedBy" in _blob(team, new)
    _seed(team, tagged=())
    assert server._resolved_by_fill(team, "t")["candidates"] == 0


def test_fill_skips_jira_errors_for_the_next_run(team, monkeypatch):
    _seed(team)
    pid = _item(team, jiraTickets=["FRAZ-404"])
    _stub_jira(monkeypatch, {})
    out = server._resolved_by_fill(team, "t")
    assert out["jiraErrors"] == 1 and "resolvedBy" not in _blob(team, pid)


# ── Shipped + config ─────────────────────────────────────────────────────────
def test_shipped_carries_the_tag_only_on_tracked_teams(team):
    _seed(team)
    rb = {"by": "dev", "team": "Wasatch", "who": "Henry Doan", "sure": True, "src": "jira", "why": "w", "at": "x"}
    a = _item(team, resolvedBy=rb)
    b = _item(team, dev="Wasatch", resolvedBy=rb)
    m = server._shipped_model(team)
    by = {x["id"]: x for x in m["items"]}
    assert m["rbTeams"] == ["Support"]
    assert by[a]["rb"]["by"] == "dev" and by[a]["rb"]["team"] == "Wasatch"
    assert by[b]["rb"] is None


def test_config_key_is_admin_only_and_returned(client, team, admin_headers, editor_headers):
    assert client.put("/api/config/resolvedByTeams", json=["Support"], headers=editor_headers).status_code == 403
    assert client.put("/api/config/resolvedByTeams", json=["Support"], headers=admin_headers).status_code == 200
    assert client.get("/api/all", headers=admin_headers).json()["resolvedByTeams"] == ["Support"]


def test_client_surfaces():
    assert "_rbRowHtml(p),   // RESOLVED-BY-1" in HTML and "_rbWire(txtEl, p);" in HTML           # item page, Notes & Resolution
    assert "toggleResolvedByTeam(${i}, this.checked)" in HTML                                     # Admin > Teams
    assert "API.put('/api/items/'+id+'/resolved-by'" in HTML and "data-shiprb" in HTML and "_rbAlsoFixed(d)" in HTML
    assert ":not(.rep-btn):not(.rb-opt) {" in HTML                                               # dark-mode button rule skips it


def test_sync_only_tags_recent_work():
    import inspect
    src = inspect.getsource(server.run_jira_sync)
    assert "_resolved_by_fill(team, _JIRA_SYNC_ACTOR, days=_RB_SYNC_DAYS, limit=_RB_SYNC_LIMIT)" in src
    assert server._RB_SYNC_DAYS <= 30
