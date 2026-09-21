"""CLEANUP-1 Item 2 - cross-run parent linking. The pull now re-attempts the FLAT pulled set each run, so
a child pulled before its parent links on the run after the parent arrives. Bounded by the flat set (not a
full re-walk). Jira mocked."""
import json
import server


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


def _insert(team, **f):
    item = {"name": f.get("name", "I"), "status": "New", **f}
    with server.db(team) as c:
        server._assign_item_key(c, item); item["id"] = server._insert_project(c, item)
    return item["id"]


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _iss(key, parent=None):
    f = {}
    if parent:
        f["parent"] = {"key": parent}
    return {"key": key, "fields": f}


def _arm(team, monkeypatch, issues):
    _set_cfg(team, "jiraEnabled", True)
    captured = {}
    def fake(jql, fields, cap=2000):
        captured["jql"] = jql
        return list(issues)
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_jira_search_all", fake)
    return captured


def test_cross_run_links_after_parent_arrives(team, monkeypatch):
    # Run 1: only the Story exists, flat, its Jira parent FRAZ-E not yet in Flow.
    story = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-S"])
    _arm(team, monkeypatch, [_iss("FRAZ-S", parent="FRAZ-E")])
    r1 = server._link_pull_parents(team, "admin", scope="flat")
    assert r1["changed"] == 0 and r1["parentNotInFlow"] == 1          # parent not in Flow -> stays flat
    assert server._pid_or_none(_blob(team, story).get("parent")) is None

    # Run 2: the Epic arrives in a later pull. The flat re-attempt now links the Story.
    epic = _insert(team, type="Epic", jiraSource="pull", jiraTickets=["FRAZ-E"])
    _arm(team, monkeypatch, [_iss("FRAZ-S", parent="FRAZ-E"), _iss("FRAZ-E")])
    r2 = server._link_pull_parents(team, "admin", scope="flat")
    assert r2["changed"] == 1 and r2["linkedToPulled"] == 1
    assert _blob(team, story)["parent"] == epic

    # Run 3: idempotent - the Story is no longer flat, so it is not even in the fetched set.
    r3 = server._link_pull_parents(team, "admin", scope="flat")
    assert r3["changed"] == 0


def test_flat_scope_excludes_already_linked(team, monkeypatch):
    epic = _insert(team, type="Epic", jiraSource="pull", jiraTickets=["FRAZ-E"])
    linked = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-S1"], parent=epic)   # already has a parent
    flat = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-S2"])                  # flat
    cap = _arm(team, monkeypatch, [_iss("FRAZ-S2", parent="FRAZ-E"), _iss("FRAZ-E")])
    r = server._link_pull_parents(team, "admin", scope="flat")
    assert r["pulled"] == 2          # only the flat Story + the flat Epic (linked S1 excluded)
    assert "FRAZ-S1" not in cap["jql"]                                # the already-linked item was not fetched
    assert _blob(team, flat)["parent"] == epic
    assert _blob(team, linked)["parent"] == epic                      # untouched


def test_all_scope_still_full_backfill(team, monkeypatch):
    epic = _insert(team, type="Epic", jiraSource="pull", jiraTickets=["FRAZ-E"])
    s1 = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-S1"])
    s2 = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-S2"])
    _arm(team, monkeypatch, [_iss("FRAZ-S1", parent="FRAZ-E"), _iss("FRAZ-S2", parent="FRAZ-E"), _iss("FRAZ-E")])
    r = server._link_pull_parents(team, "admin", scope="all")
    assert r["pulled"] == 3 and r["changed"] == 2                     # scope=all considers every pulled item
    assert _blob(team, s1)["parent"] == epic and _blob(team, s2)["parent"] == epic


def test_flat_scope_abort_on_search_failure(team, monkeypatch):
    story = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-S"])
    _set_cfg(team, "jiraEnabled", True)
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    def boom(jql, fields, cap=2000):
        raise RuntimeError("Jira 503")
    monkeypatch.setattr(server, "_jira_search_all", boom)
    r = server._link_pull_parents(team, "admin", scope="flat")
    assert r.get("aborted") is True and r["changed"] == 0
    assert server._pid_or_none(_blob(team, story).get("parent")) is None


def test_backfill_endpoint_uses_all_scope(client, team, admin_headers, monkeypatch):
    epic = _insert(team, type="Epic", jiraSource="pull", jiraTickets=["FRAZ-E"])
    story = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-S"], parent=epic)
    _arm(team, monkeypatch, [_iss("FRAZ-S", parent="FRAZ-E"), _iss("FRAZ-E")])
    r = client.post("/api/jira/backfill-parents", headers=admin_headers).json()
    assert r.get("scope") == "all" and r["pulled"] == 2               # every pulled item, not just flat
