"""Audit page rendering — every dynamic value must be HTML-escaped, including the
filter dropdown <option>s (the spot that previously injected usernames raw)."""
from fastapi.testclient import TestClient

import server


def _admin_client(team):
    c = TestClient(server.app)
    c.cookies.set("frazil_session", server.create_token(team, "admin", "admin"))
    return c


def test_audit_dropdown_escapes_usernames(team):
    evil = "<script>alert(1)</script>"
    with server.db(team) as c:
        c.execute("INSERT INTO audit_log(ts,username,action) VALUES(?,?,?)",
                  ("2026-06-04 00:00:00 UTC", evil, "login"))

    r = _admin_client(team).get(f"/audit?team={team}")
    assert r.status_code == 200
    # Raw script tag must appear nowhere (rows AND the user dropdown).
    assert "<script>alert(1)</script>" not in r.text
    # The escaped form is present (proves the value is rendered, just safely).
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


def test_audit_requires_admin_cookie(team):
    # No session cookie -> redirect to login, never renders the page.
    r = TestClient(server.app).get(f"/audit?team={team}", follow_redirects=False)
    assert r.status_code == 302

    # Non-admin token -> forbidden, not a redirect.
    c = TestClient(server.app)
    c.cookies.set("frazil_session", server.create_token(team, "viewer1", "viewer"))
    r2 = c.get(f"/audit?team={team}", follow_redirects=False)
    assert r2.status_code == 403


def test_audit_date_params_not_reflected_xss(team):
    # 4.13.0: date_from/date_to are echoed into the form; a non-date payload must be
    # blanked (strict YYYY-MM-DD guard) so it can't break out of the value="" attribute.
    c = TestClient(server.app)
    c.cookies.set("frazil_session", server.create_token(team, "admin", "admin"))
    r = c.get(f'/audit?team={team}&date_from="><script>alert(1)</script>', follow_redirects=False)
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
