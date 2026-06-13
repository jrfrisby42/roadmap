"""Feature A — Custom Kanban Boards. The /api/boards endpoints are additive and
shared (any logged-in user). These assert the server contract + validation."""


def _board(**kw):
    b = {"id": "b1", "name": "Wasatch Board", "position": 0,
         "columns": [{"name": "Done", "statuses": ["Released", "TBD"], "dropStatus": "Released"}]}
    b.update(kw)
    return b


def test_boards_default_empty(client, team, admin_headers):
    r = client.get("/api/boards", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["boards"] == []


def test_boards_round_trip(client, team, admin_headers):
    payload = {"boards": [_board()]}
    r = client.put("/api/boards", json=payload, headers=admin_headers)
    assert r.status_code == 200
    got = client.get("/api/boards", headers=admin_headers).json()["boards"]
    assert len(got) == 1 and got[0]["name"] == "Wasatch Board"
    assert got[0]["columns"][0]["statuses"] == ["Released", "TBD"]


def test_boards_editable_by_any_authed_user(client, team, editor_headers, viewer_headers):
    # spec: create/edit available to all users for now
    assert client.put("/api/boards", json={"boards": [_board()]}, headers=editor_headers).status_code == 200
    assert client.get("/api/boards", headers=viewer_headers).status_code == 200


def test_board_status_in_two_columns_rejected(client, team, admin_headers):
    bad = _board(columns=[
        {"name": "A", "statuses": ["Released"], "dropStatus": "Released"},
        {"name": "B", "statuses": ["Released"], "dropStatus": "Released"},  # dup status
    ])
    r = client.put("/api/boards", json={"boards": [bad]}, headers=admin_headers)
    assert r.status_code == 422
    assert "more than one column" in r.json()["detail"]


def test_board_dropstatus_must_be_in_statuses(client, team, admin_headers):
    bad = _board(columns=[{"name": "A", "statuses": ["Released"], "dropStatus": "TBD"}])
    assert client.put("/api/boards", json={"boards": [bad]}, headers=admin_headers).status_code == 422


def test_board_requires_columns_and_statuses(client, team, admin_headers):
    assert client.put("/api/boards", json={"boards": [_board(columns=[])]},
                      headers=admin_headers).status_code == 422
    empty_col = _board(columns=[{"name": "A", "statuses": [], "dropStatus": ""}])
    assert client.put("/api/boards", json={"boards": [empty_col]},
                      headers=admin_headers).status_code == 422


def test_board_requires_name_and_id(client, team, admin_headers):
    assert client.put("/api/boards", json={"boards": [_board(name="")]},
                      headers=admin_headers).status_code == 422


def test_board_column_order_round_trips(client, team, admin_headers):
    # 1c: reordering columns and saving must persist the new order.
    cols = [{"name": n, "statuses": [s], "dropStatus": s}
            for n, s in [("To Do", "New"), ("Doing", "In Progress"), ("Done", "Released")]]
    b = {"id": "bo", "name": "Flow", "position": 0, "columns": cols}
    client.put("/api/boards", json={"boards": [b]}, headers=admin_headers)
    got = client.get("/api/boards", headers=admin_headers).json()["boards"][0]
    assert [c["name"] for c in got["columns"]] == ["To Do", "Doing", "Done"]
    # reorder (move Done to front) and re-save
    b["columns"] = [cols[2], cols[0], cols[1]]
    client.put("/api/boards", json={"boards": [b]}, headers=admin_headers)
    got2 = client.get("/api/boards", headers=admin_headers).json()["boards"][0]
    assert [c["name"] for c in got2["columns"]] == ["Done", "To Do", "Doing"]
