"""ASSIGN-NOTIFY-1: a bulk edit that sets Assignee notifies each new assignee, like a single edit does.

Up to 3 items -> one "assigned" notification each (with the item); 4+ -> ONE summary (no item) so a big
reassignment is one bell row / one Slack DM. Unchanged assignees, self-assignment and unknown usernames
notify nobody. New assignees watch the items, as with a single edit.
"""
import server


def _mk(client, headers, name, assignee=""):
    return client.post("/api/projects", json={"name": name, "status": "Planned", "assignee": assignee},
                       headers=headers).json()["id"]


def _users(client, admin_headers):
    client.put("/api/config/users", json=[{"username": "admin", "role": "admin"},
                                          {"username": "ann", "role": "editor"},
                                          {"username": "bob", "role": "editor"}], headers=admin_headers)


def _assigned(team, who):
    with server.db(team) as c:
        return [dict(r) for r in c.execute(
            "SELECT item_id, message FROM notifications WHERE type='assigned' AND username=? ORDER BY id", (who,)).fetchall()]


def _bulk(client, headers, ids, patch):
    r = client.post("/api/items/bulk", json={"ids": ids, "patch": patch}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_small_bulk_assign_notifies_per_item(client, team, admin_headers):
    _users(client, admin_headers)
    ids = [_mk(client, admin_headers, f"Item {i}") for i in range(3)]
    _bulk(client, admin_headers, ids, {"assignee": "ann"})
    rows = _assigned(team, "ann")
    assert [r["item_id"] for r in rows] == ids
    assert all("assigned you Item" in r["message"] for r in rows)
    with server.db(team) as c:
        assert c.execute("SELECT COUNT(*) FROM watchers WHERE username='ann'").fetchone()[0] == 3


def test_large_bulk_assign_sends_one_summary(client, team, admin_headers):
    _users(client, admin_headers)
    ids = [_mk(client, admin_headers, f"Item {i}") for i in range(7)]
    _bulk(client, admin_headers, ids, {"assignee": "bob"})
    [row] = _assigned(team, "bob")
    assert row["item_id"] is None and "assigned you 7 items" in row["message"] and "and 2 more" in row["message"]


def test_unchanged_self_and_unknown_assignees_notify_nobody(client, team, admin_headers):
    _users(client, admin_headers)
    a = _mk(client, admin_headers, "Already Ann's", "ann")
    before = len(_assigned(team, "ann"))
    _bulk(client, admin_headers, [a], {"assignee": "ann"})            # no change
    assert len(_assigned(team, "ann")) == before
    b = _mk(client, admin_headers, "Mine")
    _bulk(client, admin_headers, [b], {"assignee": "admin"})          # self-assignment
    assert _assigned(team, "admin") == []
    _bulk(client, admin_headers, [b], {"assignee": "Not A User"})     # unknown -> skipped, no error
    assert _assigned(team, "Not A User") == []


def test_bulk_without_assignee_field_notifies_nobody(client, team, admin_headers):
    _users(client, admin_headers)
    a = _mk(client, admin_headers, "X", "ann")
    before = len(_assigned(team, "ann"))
    _bulk(client, admin_headers, [a], {"priority": 2})
    assert len(_assigned(team, "ann")) == before
