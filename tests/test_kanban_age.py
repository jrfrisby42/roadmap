"""KANBAN-AGE-1: days-in-status (status_since side table kept by triggers) + per-column Kanban limits."""
import json
import server


def _since(team, pid):
    with server.db(team) as c:
        r = c.execute("SELECT status, since, source FROM status_since WHERE pid=?", (pid,)).fetchone()
        return tuple(r) if r else None


def _create(client, h, **kw):
    body = {"name": "Age item", "status": "Planned", **kw}
    r = client.post("/api/projects", json=body, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_insert_records_status(client, admin_headers, team):
    pid = _create(client, admin_headers)
    st, since, src = _since(team, pid)
    assert st == "Planned" and src == "live" and since.endswith("Z")


def test_status_change_moves_since_and_other_edits_do_not(client, admin_headers, team):
    pid = _create(client, admin_headers)
    with server.db(team) as c:   # pin an old timestamp so a change is observable
        c.execute("UPDATE status_since SET since='2020-01-01T00:00:00Z' WHERE pid=?", (pid,))
    # non-status write (direct SQL, like the Jira sync's writers) keeps the timestamp
    with server.db(team) as c:
        p = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()[0])
        p["name"] = "Renamed"
        c.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(p), pid))
    assert _since(team, pid)[1] == "2020-01-01T00:00:00Z"
    # status write moves it
    with server.db(team) as c:
        p["status"] = "In Progress"
        c.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(p), pid))
    st, since, _ = _since(team, pid)
    assert st == "In Progress" and since != "2020-01-01T00:00:00Z"


def test_delete_removes_row(client, admin_headers, team):
    pid = _create(client, admin_headers)
    with server.db(team) as c:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
    assert _since(team, pid) is None


def test_api_all_serves_the_map(client, admin_headers, team):
    pid = _create(client, admin_headers)
    m = client.get("/api/all", headers=admin_headers).json()["statusSince"]
    assert m[str(pid)][0] == "Planned"


def test_backfill_from_audit_and_created(team):
    with server.db(team) as c:
        c.execute("DROP TRIGGER trg_status_since_ins")
        c.execute("DROP TRIGGER trg_status_since_upd")
        c.execute("DELETE FROM status_since")
        c.execute("DELETE FROM schema_meta WHERE key='kanban_age_backfill'")
        c.execute("INSERT INTO projects(id,data) VALUES(901,?)", (json.dumps({"name": "a", "status": "In Progress", "createdAt": "2026-01-01T00:00:00+00:00"}),))
        c.execute("INSERT INTO projects(id,data) VALUES(902,?)", (json.dumps({"name": "b", "status": "Planned", "createdAt": "2026-02-01T00:00:00+00:00"}),))
        c.execute("INSERT INTO projects(id,data) VALUES(903,?)", (json.dumps({"name": "c", "status": "Released"}),))
        server._write_audit_c(c, "update", "x", 901, "a", {"status": ["Planned", "In Progress"]})
        server._write_audit_c(c, "update", "x", 903, "c", {"status": ["Planned", "In Testing"]})   # disagrees -> unknown
    server._initialized_teams.discard(team)   # init is cached per team; force the boot path to run again
    server.init_team_db(team)
    a, b, c3 = _since(team, 901), _since(team, 902), _since(team, 903)
    assert a[0] == "In Progress" and a[2] == "audit" and a[1].endswith("Z")
    assert b == ("Planned", "2026-02-01T00:00:00+00:00", "created")
    assert c3 is None
    # triggers are back after init
    with server.db(team) as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"trg_status_since_ins", "trg_status_since_upd", "trg_status_since_del"} <= names


def _board(wip):
    col = {"name": "Doing", "statuses": ["In Progress"], "dropStatus": "In Progress"}
    if wip is not ...:
        col["wip"] = wip
    return [{"id": "b1", "name": "B", "columns": [col]}]


def test_board_limit_round_trips(client, admin_headers):
    r = client.put("/api/boards", json={"boards": _board(3)}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["boards"][0]["columns"][0]["wip"] == 3


def test_board_limit_blank_is_dropped(client, admin_headers):
    r = client.put("/api/boards", json={"boards": _board("")}, headers=admin_headers)
    assert r.status_code == 200 and "wip" not in r.json()["boards"][0]["columns"][0]


def test_board_limit_rejects_bad_values(client, admin_headers):
    for bad in (-1, 1000, 2.5, "3", True):
        r = client.put("/api/boards", json={"boards": _board(bad)}, headers=admin_headers)
        assert r.status_code == 422, (bad, r.text)


# ── QUICK-JUMP-1 source guards (the palette is frontend-only; verified headlessly when built) ──
import pathlib
_HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8")


def test_quick_jump_is_wired_to_ctrl_k():
    assert "function openQuickJump(){" in _HTML
    assert "(e.key||'').toLowerCase()!=='k'" in _HTML and "e.preventDefault(); e.stopPropagation(); openQuickJump();" in _HTML


def test_quick_jump_searches_keys_numbers_jira_and_views():
    body = _HTML.split("function _qjItems(q){", 1)[1].split("\n  }\n", 1)[0]
    assert "_frzJiraKeyFromQuery(q)" in body and "'-'+ql" in body
    assert "_frzOrderedViews()" in _HTML.split("function _qjCommands(){", 1)[1].split("\n  }\n", 1)[0]


def test_board_cards_carry_the_age_chip_and_limit_count():
    assert "+_frzAgeChip(p)" in _HTML and "frz-colcount" in _HTML and "data-colwip" in _HTML
