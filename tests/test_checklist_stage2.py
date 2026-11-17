"""CHECKLIST-STAGE2-1: Type checklist templates reach the two places that used to miss them.

1. A ticket submitted through the public portal gets its Type's checklist steps (unticked, optional
   flags kept), the same steps the New Item form pre-fills. Types without a template get none.
2. The item page's "+ Add the <Type> checklist" button fills in the template steps instead of opening
   a blank step (source-shape guard; behaviour verified headlessly).
"""
import pathlib

import server

HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def _set(client, admin_headers, key, value):
    assert client.put(f"/api/config/{key}", json=value, headers=admin_headers).status_code == 200


def _portal(client, admin_headers):
    _set(client, admin_headers, "statuses", ["New", "In Progress", "Released"])
    _set(client, admin_headers, "statusIsDefault", {"New": True})
    _set(client, admin_headers, "types", [
        {"name": "Onboarding", "checklist": ["Laptop ordered", {"text": "Parking pass", "optional": True}, "  "]},
        {"name": "Bug"},
    ])
    _set(client, admin_headers, "intakeEnabled", True)


def _submit(client, team, title, ttype):
    server._rate.clear()
    r = client.post(f"/api/intake/{team}", json={"title": title, "description": "x", "type": ttype,
                                                  "email": "pat@example.com", "name": "Pat"})
    assert r.status_code == 200, r.text


def _item(client, admin_headers, title):
    return next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"] if p["name"] == title)


def test_portal_ticket_gets_the_type_checklist(client, team, admin_headers):
    _portal(client, admin_headers)
    _submit(client, team, "New hire Sam", "Onboarding")
    cl = _item(client, admin_headers, "New hire Sam")["checklist"]
    assert [s["text"] for s in cl] == ["Laptop ordered", "Parking pass"]      # blank template step dropped
    assert all(s["done"] is False and s["id"] and "by" not in s for s in cl)
    assert "optional" not in cl[0] and cl[1]["optional"] is True


def test_portal_ticket_without_a_template_gets_no_checklist(client, team, admin_headers):
    _portal(client, admin_headers)
    _submit(client, team, "Button broken", "Bug")
    assert "checklist" not in _item(client, admin_headers, "Button broken")


def test_portal_checklist_can_be_ticked(client, team, admin_headers):
    _portal(client, admin_headers)
    _submit(client, team, "New hire Ana", "Onboarding")
    it = _item(client, admin_headers, "New hire Ana")
    r = client.post(f"/api/items/{it['id']}/checklist/{it['checklist'][0]['id']}/check",
                    json={"done": True}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["checklist"][0]["done"] is True


def test_add_checklist_button_fills_in_the_template():
    assert "Add the '+esc(p.type)+' checklist" in HTML
    assert "saveShape(tpl.map(s => ({ text: s.text, optional: !!s.optional })))" in HTML
    assert "t.closest('.cl-start') || t.closest('.cl-addbtn')" not in HTML     # no longer opens a blank step
