"""DEPT-DEFAULTS-1 - a newly created team is seeded with the standard department set.

Names + pill colors, NO notify emails (per-team routing), all fully admin-editable/deletable
through the normal config route. Existing teams are not retro-seeded.
"""
import json

import server


def _cfg(team, key):
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def test_new_team_seeds_default_departments(team):
    depts = _cfg(team, "departments")
    assert depts == server._DEFAULT_DEPARTMENTS          # the 13 standard names, in order
    assert len(depts) == 13
    assert {"FINANCE", "HUMAN RESOURCES", "EXECUTIVE", "TECHNOLOGY"} <= set(depts)


def test_new_team_seeds_colors_but_no_emails(team):
    meta = _cfg(team, "departmentMeta")
    assert meta.get("FINANCE", {}).get("color") == "#83d043"     # colors seeded
    assert meta.get("WAREHOUSE", {}).get("color") == "#191a1a"
    # THE point: no notify emails are baked into the default - routing is per-team.
    assert all("emails" not in v for v in meta.values())


def test_default_departments_are_admin_editable(client, team, admin_headers):
    # "Deletable and editable by an admin": the standard config route replaces the whole list.
    r = client.put("/api/config/departments", json=["ONLY ONE"], headers=admin_headers)
    assert r.status_code == 200
    assert _cfg(team, "departments") == ["ONLY ONE"]


def test_default_departments_not_editable_by_non_admin(client, team, editor_headers):
    # Config edits stay admin-only - seeding defaults did not open the route.
    r = client.put("/api/config/departments", json=["NOPE"], headers=editor_headers)
    assert r.status_code == 403
