"""JIRA-PULL-1 Stage 6 - parent hierarchy. Set `parent` on pulled items, resolving the Jira parent key to
a Flow item id. Same invariant as Stage 5: ONLY jiraSource=='pull' items are modified; a pushed (565)
item may be REFERENCED as a parent but never written.

Covered here (pure + team-DB, Jira mocked): the id normalizer, the key/pulled maps, the cycle guard, and
the link engine (link-to-pulled, link-to-pushed-without-modifying-it, parent-not-in-flow-stays-flat,
requires-exclusion, self/cycle skip, idempotency, no notifications/activities) + the backfill endpoint
(admin gate, abort-on-search-failure). The live proofs (screenshots, the backlog-not-emptied check) run
in the acceptance.
"""
import json
import server


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


def _insert(team, **fields):
    item = {"name": fields.get("name", "Item"), "product": "Fraznet", "type": "Story", "status": "New", **fields}
    with server.db(team) as c:
        server._assign_item_key(c, item)
        item["id"] = server._insert_project(c, item)
    return item["id"]


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _iss(key, parent=None):
    f = {"summary": key, "issuetype": {"name": "Story"}, "status": {"name": "New"}, "project": {"key": "FRAZ"}}
    if parent:
        f["parent"] = {"key": parent}
    return {"key": key, "fields": f}


# ── pure helpers ────────────────────────────────────────────────────────────────────────────────────────
def test_pid_or_none():
    assert server._pid_or_none(5) == 5
    assert server._pid_or_none("7") == 7
    assert server._pid_or_none("") is None
    assert server._pid_or_none(None) is None
    assert server._pid_or_none("x") is None


def test_pull_issue_parent_key():
    assert server._pull_issue_parent_key(_iss("A", parent="B")) == "B"
    assert server._pull_issue_parent_key(_iss("A")) is None


def test_pull_maps(team):
    e = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-E"])
    s = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-S"])
    pushed = _insert(team, jiraTickets=["FRAZ-PE"])            # no jiraSource
    with server.db(team) as c:
        key2id, pulled_ids = server._pull_maps(c)
    assert key2id["FRAZ-E"] == e and key2id["FRAZ-S"] == s and key2id["FRAZ-PE"] == pushed
    assert pulled_ids == {e, s} and pushed not in pulled_ids


def test_cycle_guard(team):
    a = _insert(team, jiraSource="pull", jiraTickets=["A"])
    b = _insert(team, jiraSource="pull", jiraTickets=["B"], parent=a)   # b's parent is a
    with server.db(team) as c:
        # making a's parent = b would close the loop a->b->a
        assert server._pull_would_cycle(c, a, b, {}) is True
        # making a's parent = a is self (caller handles separately); walk from a with no parent set:
        assert server._pull_would_cycle(c, b, a, {}) is False   # b under a: a has no parent -> no cycle


# ── the link engine ────────────────────────────────────────────────────────────────────────────────────
def _pid_none(v):
    return server._pid_or_none(v) is None


def _call_backfill(team, monkeypatch, issues):
    # jira_backfill_parents is a FastAPI route; called directly its `auth` param takes the dict verbatim
    # (Depends is only resolved by the framework). Jira is mocked so no network call happens.
    monkeypatch.setattr(server, "_jira_search_all", lambda *a, **k: list(issues))
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    return server.jira_backfill_parents({"team": team, "username": "admin"})


def test_backfill_links_pulled_and_pushed_but_never_modifies_pushed(team, monkeypatch):
    _set_cfg(team, "jiraEnabled", True)
    epic   = _insert(team, name="Epic",    type="Epic",  jiraSource="pull", jiraTickets=["FRAZ-E"])
    story  = _insert(team, name="Story",   jiraSource="pull", jiraTickets=["FRAZ-S"])      # parent FRAZ-E (pulled)
    pushed = _insert(team, name="PushEpic", type="Epic", jiraTickets=["FRAZ-PE"])          # NO jiraSource (a "565")
    story2 = _insert(team, name="Story2",  jiraSource="pull", jiraTickets=["FRAZ-S2"])     # parent FRAZ-PE (pushed)
    story3 = _insert(team, name="Story3",  jiraSource="pull", jiraTickets=["FRAZ-S3"])     # parent FRAZ-X (not in flow)
    story4 = _insert(team, name="Story4",  jiraSource="pull", jiraTickets=["FRAZ-S4"], requires=epic)  # requires set
    pushed_before = _blob(team, pushed)
    issues = [_iss("FRAZ-E"), _iss("FRAZ-S", "FRAZ-E"), _iss("FRAZ-S2", "FRAZ-PE"),
              _iss("FRAZ-S3", "FRAZ-X"), _iss("FRAZ-S4", "FRAZ-E"), _iss("FRAZ-PE")]
    res = _call_backfill(team, monkeypatch, issues)

    assert _blob(team, story)["parent"] == epic          # linked to a PULLED parent
    assert _blob(team, story2)["parent"] == pushed        # linked to a PUSHED parent (the hierarchy is a fact)
    assert "parent" not in _blob(team, story3) or _pid_none(_blob(team, story3).get("parent"))  # not in flow -> flat
    assert _pid_none(_blob(team, story4).get("parent"))   # requires set -> skipped, stays flat
    assert _blob(team, epic).get("parent") in (None, "")  # Epic has no Jira parent -> flat
    # THE INVARIANT: the pushed parent item was never modified
    assert _blob(team, pushed) == pushed_before
    assert res["linkedToPulled"] == 1 and res["linkedToPushed"] == 1
    assert res["parentNotInFlow"] == 1 and res["skippedRequires"] == 1 and res["noParentKey"] == 1
    assert res["changed"] == 2


def test_backfill_idempotent(team, monkeypatch):
    _set_cfg(team, "jiraEnabled", True)
    epic = _insert(team, type="Epic", jiraSource="pull", jiraTickets=["FRAZ-E"])
    _insert(team, jiraSource="pull", jiraTickets=["FRAZ-S"])
    issues = [_iss("FRAZ-E"), _iss("FRAZ-S", "FRAZ-E")]
    first = _call_backfill(team, monkeypatch, issues)
    assert first["changed"] == 1
    second = _call_backfill(team, monkeypatch, issues)
    assert second["changed"] == 0 and second["alreadyLinked"] == 1


def test_backfill_no_notifications_or_activities(team, monkeypatch):
    _set_cfg(team, "jiraEnabled", True)
    epic = _insert(team, type="Epic", jiraSource="pull", jiraTickets=["FRAZ-E"])
    story = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-S"])
    with server.db(team) as c:
        c.execute("INSERT INTO watchers(item_id,username) VALUES(?,?)", (story, "someone"))
        n0 = c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        a0 = c.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    res = _call_backfill(team, monkeypatch, [_iss("FRAZ-E"), _iss("FRAZ-S", "FRAZ-E")])
    assert res["changed"] == 1
    with server.db(team) as c:
        n1 = c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        a1 = c.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    assert n1 == n0 and a1 == a0


def test_backfill_cycle_is_not_creatable(team, monkeypatch):
    _set_cfg(team, "jiraEnabled", True)
    a = _insert(team, jiraSource="pull", jiraTickets=["A"])
    b = _insert(team, jiraSource="pull", jiraTickets=["B"])
    # A's Jira parent is B and B's Jira parent is A - a cycle. Exactly one link may form, never both.
    res = _call_backfill(team, monkeypatch, [_iss("A", "B"), _iss("B", "A")])
    pa = server._pid_or_none(_blob(team, a).get("parent"))
    pb = server._pid_or_none(_blob(team, b).get("parent"))
    assert not (pa == b and pb == a), "a cycle was created"
    assert res["skippedCycle"] >= 1


def test_backfill_search_failure_aborts_zero_writes(team, monkeypatch):
    _set_cfg(team, "jiraEnabled", True)
    epic = _insert(team, type="Epic", jiraSource="pull", jiraTickets=["FRAZ-E"])
    story = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-S"])
    def boom(*a, **k):
        raise RuntimeError("Jira 503")
    monkeypatch.setattr(server, "_jira_search_all", boom)
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    res = server.jira_backfill_parents({"team": team, "username": "admin"})
    assert res.get("aborted") is True and res["changed"] == 0
    assert server._pid_or_none(_blob(team, story).get("parent")) is None    # untouched


def test_backfill_no_pulled_items_noop(team, monkeypatch):
    _set_cfg(team, "jiraEnabled", True)
    _insert(team, jiraTickets=["FRAZ-X"])   # linked but not pulled
    res = _call_backfill(team, monkeypatch, [_iss("FRAZ-X")])
    assert res["pulled"] == 0 and res["changed"] == 0


# ── endpoint gating ──────────────────────────────────────────────────────────────────────────────────────
def test_backfill_admin_gated(client, team, viewer_headers):
    assert client.post("/api/jira/backfill-parents", headers=viewer_headers).status_code in (401, 403)
