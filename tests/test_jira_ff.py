"""Feature-flag pull on release transition (item #5 refactor).

Jira is blanked in the test env, so these monkeypatch `jira_configured` and
`_fetch_jira_feature_flags` to drive the FF-pull path without any network call.
"""
import json

import server


def _create(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()["id"]


def _get_item(client, headers, pid):
    allr = client.get("/api/all", headers=headers).json()
    return next(p for p in allr["projects"] if p["id"] == pid)


def test_ff_pulled_on_release_transition(client, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_fetch_jira_feature_flags", lambda ticket: {"flag-A", "flag-B"})
    client.put("/api/config/statusIsReleased", json={"Released": True}, headers=admin_headers)

    pid = _create(client, admin_headers, status="Planned",
                  jiraTickets=["PROJ-1"], featureFlags=["manual-1"])

    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Released",
                         "jiraTickets": ["PROJ-1"], "featureFlags": ["manual-1"]},
                   headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body["jiraFeatureFlags"]) == {"flag-A", "flag-B"}
    # featureFlags = union of manual + Jira flags.
    assert set(body["featureFlags"]) == {"manual-1", "flag-A", "flag-B"}

    stored = _get_item(client, admin_headers, pid)
    assert set(stored["jiraFeatureFlags"]) == {"flag-A", "flag-B"}


def test_ff_skipped_when_not_release(client, admin_headers, monkeypatch):
    # Status change that is NOT to the released status -> no FF pull.
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    called = {"n": 0}
    monkeypatch.setattr(server, "_fetch_jira_feature_flags",
                        lambda t: called.__setitem__("n", called["n"] + 1) or {"x"})
    client.put("/api/config/statusIsReleased", json={"Released": True}, headers=admin_headers)

    pid = _create(client, admin_headers, status="Planned", jiraTickets=["PROJ-1"])
    client.put(f"/api/projects/{pid}",
               json={"name": "Item", "status": "In Progress", "jiraTickets": ["PROJ-1"]},
               headers=admin_headers)
    assert called["n"] == 0


def test_ff_write_does_not_clobber_concurrent_edit(client, admin_headers, team, monkeypatch):
    """The FF write must re-read + merge, not overwrite with the stale request body.

    Simulates a concurrent edit landing *during* the Jira network walk; the edit
    must survive while the feature flags are still applied.
    """
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    client.put("/api/config/statusIsReleased", json={"Released": True}, headers=admin_headers)
    pid = _create(client, admin_headers, status="Planned", jiraTickets=["PROJ-1"], name="Original")

    def fake_fetch(ticket):
        # A different request renames the item while we're "talking to Jira".
        with server.db(team) as c:
            cur = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
            cur["name"] = "Concurrently Renamed"
            c.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(cur), pid))
        return {"flag-A"}

    monkeypatch.setattr(server, "_fetch_jira_feature_flags", fake_fetch)

    client.put(f"/api/projects/{pid}",
               json={"name": "Original", "status": "Released", "jiraTickets": ["PROJ-1"]},
               headers=admin_headers)

    item = _get_item(client, admin_headers, pid)
    assert item["name"] == "Concurrently Renamed"      # concurrent edit preserved (race fixed)
    assert item["jiraFeatureFlags"] == ["flag-A"]      # FF still applied
