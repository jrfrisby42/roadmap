"""INTAKE-ASSIGNEE-1: per-Type default assignee for portal tickets.

`intakeTypeAssignees` ({type: username}, admin-edited in Admin -> intake settings) sets the assignee on a
new portal ticket of that Type, before the insert. A ticket born assigned bells admins + that assignee
(AC-AUDIENCE-1) instead of every editor. The username is honoured only while it is still a user on the
team; otherwise the ticket stays unassigned and the admins + editors fallback applies.
"""
import server


def _set(client, admin_headers, key, value):
    r = client.put(f"/api/config/{key}", json=value, headers=admin_headers)
    assert r.status_code == 200, (key, r.text)


def _setup(client, admin_headers, team, mapping, notify=True):
    _set(client, admin_headers, "statuses", ["New", "In Progress", "Released"])
    _set(client, admin_headers, "statusIsDefault", {"New": True})
    _set(client, admin_headers, "types", [{"name": "Bug"}, {"name": "Request"}])
    _set(client, admin_headers, "products", [{"name": "Fraznet"}])
    _set(client, admin_headers, "intakeEnabled", True)
    _set(client, admin_headers, "intakeNotifyTeam", notify)
    _set(client, admin_headers, "intakeTypeAssignees", mapping)
    # Users last: replacing the list does not affect the minted admin token used above.
    _set(client, admin_headers, "users", [
        {"username": "admin", "role": "admin"},
        {"username": "boss", "role": "admin"},
        {"username": "dev1", "role": "editor"},
        {"username": "dev2", "role": "editor"},
        {"username": "looker", "role": "viewer"},
    ])


def _submit(client, team, title, ttype):
    server._rate.clear()
    r = client.post(f"/api/intake/{team}", json={
        "title": title, "description": "it broke", "email": "r@example.com", "name": "Pat",
        "type": ttype, "product": "Fraznet"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _blob(team, pid):
    import json
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _intake_bell(team, pid):
    with server.db(team) as c:
        return {r["username"] for r in c.execute(
            "SELECT username FROM notifications WHERE type='intake' AND item_id=?", (pid,)).fetchall()}


def test_config_key_is_registered_and_defaults_empty(client, admin_headers):
    assert "intakeTypeAssignees" in server.VALID_KEYS
    assert client.get("/api/all", headers=admin_headers).json()["intakeTypeAssignees"] == {}


def test_ticket_of_a_mapped_type_is_born_assigned(client, team, admin_headers):
    _setup(client, admin_headers, team, {"Bug": "dev2"})
    pid = _submit(client, team, "Login fails", "Bug")
    assert _blob(team, pid).get("assignee") == "dev2"


def test_assigned_ticket_bells_admins_and_the_assignee_only(client, team, admin_headers):
    _setup(client, admin_headers, team, {"Bug": "dev2"})
    pid = _submit(client, team, "Login fails", "Bug")
    assert _intake_bell(team, pid) == {"admin", "boss", "dev2"}      # dev1 is not belled


def test_unmapped_type_stays_unassigned_and_bells_admins_and_editors(client, team, admin_headers):
    _setup(client, admin_headers, team, {"Bug": "dev2"})
    pid = _submit(client, team, "Need a report", "Request")
    assert not _blob(team, pid).get("assignee")
    assert _intake_bell(team, pid) == {"admin", "boss", "dev1", "dev2"}


def test_mapping_to_a_removed_user_is_ignored(client, team, admin_headers):
    _setup(client, admin_headers, team, {"Bug": "gone.user"})
    pid = _submit(client, team, "Login fails", "Bug")
    assert not _blob(team, pid).get("assignee")
    assert _intake_bell(team, pid) == {"admin", "boss", "dev1", "dev2"}
