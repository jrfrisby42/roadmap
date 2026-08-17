"""FN5: the FLAGGED predicate has two halves - the server (list_items ?flag=1 + /api/items/flagged-ids)
and the client (roadmap.html: the dot, the item banner, the Kanban toggle). They must agree on what
"flagged" means, or a correct view and a windowed view sit under one "Flagged" label disagreeing.

This asserts:
  1. PARITY - the client FLAG_TYPES / FLAG_TERMINAL_STATUSES constants (extracted from roadmap.html)
     equal the server ones. Same intent as tests/test_sla_parity.py: two implementations, one rule.
  2. BEHAVIOUR - the server flag filter + flagged-ids endpoint select exactly the items with an
     UNRESOLVED flag activity (an allowlisted type NOT in a terminal state), correct across pagination.
"""
import os
import re

import server

ROADMAP = os.path.join(os.path.dirname(__file__), os.pardir, "roadmap.html")


def _extract_array(var_name):
    """Pull a top-level `var NAME = ['a','b',...];` string-array literal out of roadmap.html."""
    with open(ROADMAP, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"\bvar\s+" + re.escape(var_name) + r"\s*=\s*\[([^\]]*)\]\s*;", src)
    assert m, f"{var_name} not found as a top-level array in roadmap.html"
    return [s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip()]


# ── 1. PARITY ────────────────────────────────────────────────────────────────

def test_flag_types_client_matches_server():
    assert set(_extract_array("FLAG_TYPES")) == set(server.FLAG_TYPES)


def test_flag_terminal_statuses_client_matches_server():
    assert set(_extract_array("FLAG_TERMINAL_STATUSES")) == set(server.FLAG_TERMINAL_STATUSES)


def test_flag_types_are_the_three_modal_types():
    # Guard against silent widening/narrowing: exactly the three the Flag Issue modal offers.
    assert set(server.FLAG_TYPES) == {"At Risk", "Blocked", "Needs Decision"}


# ── 2. BEHAVIOUR (server) ────────────────────────────────────────────────────

def _mk(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()["id"]


def _flag(client, headers, item_id, activity_type="At Risk", status="Open"):
    return client.post("/api/activities", headers=headers, json={
        "activity_type": activity_type, "item_id": item_id, "item_name": "Item",
        "source": "User", "message": "x", "status": status,
    })


def _flag_ids(client, headers):
    return set(client.get("/api/items/flagged-ids", headers=headers).json()["ids"])


def _filter_ids(client, headers, qs=""):
    return {i["id"] for i in client.get("/api/items?flag=1&" + qs, headers=headers).json()["items"]}


def test_flagged_selects_unresolved_flag_of_each_type(client, team, admin_headers):
    a = _mk(client, admin_headers); _flag(client, admin_headers, a, "At Risk", "Open")
    b = _mk(client, admin_headers); _flag(client, admin_headers, b, "Blocked", "Read")
    c = _mk(client, admin_headers); _flag(client, admin_headers, c, "Needs Decision", "Open")
    plain = _mk(client, admin_headers)                              # no flag
    assert _flag_ids(client, admin_headers) == {a, b, c}
    assert _filter_ids(client, admin_headers) == {a, b, c}
    assert plain not in _filter_ids(client, admin_headers)


def test_flagged_excludes_terminal_status(client, team, admin_headers):
    a = _mk(client, admin_headers); _flag(client, admin_headers, a, "At Risk", "Open")
    for term in server.FLAG_TERMINAL_STATUSES:
        it = _mk(client, admin_headers); _flag(client, admin_headers, it, "At Risk", term)
    # only the Open one is flagged; every terminal-status flag is excluded
    assert _flag_ids(client, admin_headers) == {a}
    assert _filter_ids(client, admin_headers) == {a}


def test_flagged_excludes_non_flag_activity_types(client, team, admin_headers):
    a = _mk(client, admin_headers); _flag(client, admin_headers, a, "At Risk", "Open")
    n = _mk(client, admin_headers); _flag(client, admin_headers, n, "Needs Date Check", "Open")  # not a flag type
    assert _flag_ids(client, admin_headers) == {a}
    assert n not in _filter_ids(client, admin_headers)


def test_flag_filter_composes_with_other_filters(client, team, admin_headers):
    # the owner index column derives from the blob's `dev` field (server.py:3361)
    a = _mk(client, admin_headers, dev="alice"); _flag(client, admin_headers, a, "Blocked", "Open")
    b = _mk(client, admin_headers, dev="bob");   _flag(client, admin_headers, b, "Blocked", "Open")
    assert _filter_ids(client, admin_headers, "owner=alice") == {a}


def test_flag_filter_correct_across_pagination(client, team, admin_headers):
    ids = [_mk(client, admin_headers) for _ in range(5)]
    for it in ids:
        _flag(client, admin_headers, it, "At Risk", "Open")
    _mk(client, admin_headers)  # an unflagged item, must never appear
    r1 = client.get("/api/items?flag=1&page=1&page_size=2", headers=admin_headers).json()
    r2 = client.get("/api/items?flag=1&page=2&page_size=2", headers=admin_headers).json()
    r3 = client.get("/api/items?flag=1&page=3&page_size=2", headers=admin_headers).json()
    assert r1["total"] == 5                          # total reflects the FILTERED set, not all items
    seen = {i["id"] for i in r1["items"] + r2["items"] + r3["items"]}
    assert seen == set(ids)


def test_no_flag_param_returns_all(client, team, admin_headers):
    a = _mk(client, admin_headers); _flag(client, admin_headers, a, "At Risk", "Open")
    _mk(client, admin_headers)
    assert client.get("/api/items", headers=admin_headers).json()["total"] == 2
