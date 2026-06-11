"""Phase 1 (JIRA-REPLACEMENT.md) step 1a: items mirror their JSON blob into
indexed columns on every write, and a boot-time backfill indexes legacy rows.
Nothing reads these columns yet — this just locks in the dual-write + backfill."""
import json

import server


def _cols(team, pid):
    with server.db(team) as c:
        row = c.execute(
            "SELECT item_key, type, status, parent_id, product, owner, assignee, "
            "reporter, priority, story_points, sprint_id, archived, updated_ts "
            "FROM projects WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def test_create_populates_index_columns(client, team, admin_headers):
    body = {"name": "Item", "type": "Feature", "status": "Planned",
            "product": "Fraznet", "dev": "PodA", "priority": "High", "parent": None}
    pid = client.post("/api/projects", json=body, headers=admin_headers).json()["id"]
    cols = _cols(team, pid)
    assert cols["type"] == "Feature"
    assert cols["status"] == "Planned"
    assert cols["product"] == "Fraznet"
    assert cols["owner"] == "PodA"          # mirrored from blob field `dev`
    assert cols["priority"] == "High"
    assert cols["archived"] == 0
    assert cols["updated_ts"]                # set on write


def test_update_refreshes_columns_and_timestamp(client, team, admin_headers):
    pid = client.post("/api/projects",
                      json={"name": "Item", "status": "Planned", "dev": "PodA"},
                      headers=admin_headers).json()["id"]
    ts1 = _cols(team, pid)["updated_ts"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "In Progress", "dev": "PodB",
                         "parent": pid + 1000},
                   headers=admin_headers)
    assert r.status_code == 200
    cols = _cols(team, pid)
    assert cols["status"] == "In Progress"
    assert cols["owner"] == "PodB"
    assert cols["parent_id"] == pid + 1000
    assert cols["updated_ts"] != ts1        # timestamp advanced


def test_parent_and_points_coercion(client, team, admin_headers):
    pid = client.post("/api/projects",
                      json={"name": "X", "status": "Planned", "parent": "",
                            "storyPoints": "5"},
                      headers=admin_headers).json()["id"]
    cols = _cols(team, pid)
    assert cols["parent_id"] is None        # "" -> NULL, not 0
    assert cols["story_points"] == 5.0      # string coerced to float


def test_backfill_indexes_legacy_rows(team):
    # Simulate a legacy row written straight to the blob with NULL columns,
    # then run the same backfill init_team_db does and confirm it indexes it.
    legacy = {"name": "Legacy", "type": "Task", "status": "TBD", "dev": "PodC"}
    with server.db(team) as c:
        cur = c.execute("INSERT INTO projects(data) VALUES(?)", (json.dumps(legacy),))
        pid = cur.lastrowid
        c.execute("UPDATE projects SET updated_ts=NULL, status=NULL, owner=NULL WHERE id=?", (pid,))
    assert _cols(team, pid)["status"] is None   # genuinely un-indexed

    with server.db(team) as c:
        for r in c.execute("SELECT id, data FROM projects WHERE updated_ts IS NULL").fetchall():
            server._reindex_project(c, r["id"], json.loads(r["data"]))

    cols = _cols(team, pid)
    assert cols["status"] == "TBD"
    assert cols["owner"] == "PodC"
    assert cols["type"] == "Task"
    assert cols["updated_ts"]
