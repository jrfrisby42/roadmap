"""FIX-409-HDR-1: the Jira link-issue duplicate rejection is a 409 whose DICT detail names the
holding item by its KEY (not the numeric id), so the client surfaces the real reason instead of the
generic concurrency message. (The client-side three-branch _check handling is frontend JS with no
pytest harness in this repo; verified by trace + live check, per project convention.)"""
import server


def test_link_issue_duplicate_returns_dict_message_with_item_key(client, team, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "jira_configured", lambda: True)   # else the endpoint 503s before the dup check
    holder = client.post("/api/projects", json={"name": "Holder", "status": "Planned"},
                         headers=admin_headers).json()
    hid, hkey = holder["id"], holder["itemKey"]
    # jiraTickets is not a create-body field; set it on the holder's blob so the dup scan finds it.
    with server.db(team) as c:
        blob = server._get_item_blob(c, hid); blob["jiraTickets"] = ["FRAZ-1"]; server._save_project(c, hid, blob)
    other = client.post("/api/projects", json={"name": "Other", "status": "Planned"},
                        headers=admin_headers).json()["id"]

    r = client.post("/api/jira/link-issue",
                    json={"ticket": "FRAZ-1", "item_id": other, "item_name": "Other"}, headers=admin_headers)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and "message" in detail        # canonical dict shape -> client's d.message wins
    assert "FRAZ-1" in detail["message"] and hkey in detail["message"]
    assert f"#{hid}" not in detail["message"]                      # names the KEY, not the numeric id
