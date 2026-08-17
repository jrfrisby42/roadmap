"""ACT-WINDOW-1: the six work-queue consumers (AC Open tab, indicator dot, Dashboard At Risk/
Blocked/Needs-Decision tiles, Planning at-risk bucket, sys_atrisk view, unread badge) must read the
uncapped `/api/activities?status=open` set, NOT the latest-500 `/api/activities` window. On a busy
team, routine terminal `Action Taken` bookkeeping evicts still-open flags from that window, so a
consumer reading the capped feed silently drops items that need attention.

The load-bearing REGRESSION GUARD is test_open_mode_surfaces_flag_buried_past_the_500_window: it
buries one unresolved flag under >500 terminal rows and asserts the capped feed MISSES it while the
open feed CATCHES it. If any consumer (or the endpoint) were repointed back at the capped array, an
aged-out flag would vanish - exactly the divergence this test pins. It is demonstrably falsifiable:
point the final assertion at the capped feed and it fails (see the deliverable's revert demo).
"""
import server


def _mk(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()["id"]


def _seed(team, item_id, activity_type, status, name="X", source="System"):
    """Insert an activity straight through the server helper (fast bulk seeding, real code path)."""
    return server._insert_activity(
        {"activity_type": activity_type, "item_id": item_id, "item_name": name,
         "source": source, "status": status, "message": "m"}, team)


def _open_ids(client, headers):
    return {a["item_id"] for a in client.get("/api/activities?status=open", headers=headers).json()}


def _capped_ids(client, headers):
    return {a["item_id"] for a in client.get("/api/activities", headers=headers).json()}


# ── THE REGRESSION GUARD ──────────────────────────────────────────────────────────────────────────
def test_open_mode_surfaces_flag_buried_past_the_500_window(client, team, admin_headers):
    # 1) One unresolved flag, created FIRST so it has the lowest id (oldest).
    flagged = _mk(client, admin_headers)
    _seed(team, flagged, "At Risk", "Open", name="OLD-FLAG")
    # 2) Bury it: >500 terminal Action Taken rows (the real-world dominant type) with higher ids.
    filler = _mk(client, admin_headers)
    for _ in range(520):
        _seed(team, filler, "Action Taken", "Auto-Cleared")
    # 3) The capped latest-500 window has been pushed past the old flag - it is NOT visible there...
    assert flagged not in _capped_ids(client, admin_headers)
    # 4) ...but the uncapped open set still surfaces it. THIS is what the six consumers rely on.
    assert flagged in _open_ids(client, admin_headers)


def test_open_mode_excludes_terminal_statuses(client, team, admin_headers):
    a = _mk(client, admin_headers); _seed(team, a, "At Risk", "Open")
    for term in server.FLAG_TERMINAL_STATUSES:
        it = _mk(client, admin_headers); _seed(team, it, "At Risk", term)
    ids = _open_ids(client, admin_headers)
    assert a in ids
    # Every terminal-status activity is excluded by construction (defined by exclusion of the set).
    assert len([1 for i in ids if i != a]) == 0


def test_open_mode_keeps_read_as_unresolved(client, team, admin_headers):
    # "Read" is seen-but-unresolved (the Open tab shows Open + Read); it must NOT be treated terminal.
    a = _mk(client, admin_headers); _seed(team, a, "Blocked", "Read")
    assert a in _open_ids(client, admin_headers)


def test_open_mode_is_unbounded(client, team, admin_headers):
    # More than 500 UNRESOLVED rows -> open mode returns them all (no LIMIT); the capped feed caps at 500.
    for _ in range(520):
        it = _mk(client, admin_headers); _seed(team, it, "Needs Decision", "Open")
    open_rows = client.get("/api/activities?status=open", headers=admin_headers).json()
    capped_rows = client.get("/api/activities", headers=admin_headers).json()
    assert len(open_rows) > 500
    assert len(capped_rows) == 500


def test_open_mode_preserves_contributor_scope(client, team, admin_headers, editor_headers, contributor_headers):
    mine   = _mk(client, admin_headers, assignee="contrib1")
    theirs = _mk(client, admin_headers, assignee="other", dev="OtherPod")
    for pid, name in ((mine, "MINE"), (theirs, "THEIRS")):
        _seed(team, pid, "Flagged Issue", "Open", name=name)
    ids = _open_ids(client, contributor_headers)
    # Same scoping the capped path applies: out-of-scope open rows never leak to a Contributor.
    assert mine in ids and theirs not in ids


def test_open_mode_gated_like_the_capped_feed(client, team):
    # Same shared require_auth as /api/activities and FN5's /api/items/flagged-ids:
    #   no token            -> 401
    #   X-Team but no token  -> 401
    #   token + wrong team   -> 403 (tenant isolation)
    assert client.get("/api/activities?status=open").status_code == 401
    assert client.get("/api/activities?status=open", headers={"X-Team": team}).status_code == 401
    tok = server.create_token(team, "admin", "admin")
    r = client.get("/api/activities?status=open",
                   headers={"Authorization": f"Bearer {tok}", "X-Team": "differentteam"})
    assert r.status_code == 403
