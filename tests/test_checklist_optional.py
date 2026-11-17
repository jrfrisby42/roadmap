"""CHECKLIST-OPTIONAL-1: a Type template's checklist step can be marked Optional.

Some onboarding steps do not apply to everyone (a temp, for example). An optional step still comes into
the new item (included by default; whoever creates it removes what does not apply), shows an "optional"
tag, is left out of the "3/5" counts, and never triggers the "steps still open" prompt on close. When
EVERY step is optional the counts use them all. A template step is a plain string (required) or
{text, optional:true}, so existing templates keep working. The server only stores the flag.
"""
import json
import pathlib

import server


def _create(client, headers, **fields):
    return client.post("/api/projects", json={"name": "Onboard Sam", "status": "Planned", **fields}, headers=headers)


def _cl(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])["checklist"]


STEPS = ["Laptop", {"text": "Benefits enrollment", "optional": True}, {"text": "Badge", "optional": False}]


def test_create_keeps_the_optional_flag_only_when_true(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=STEPS + [{"text": "Parking", "optional": "yes"}]).json()["id"]
    cl = _cl(team, pid)
    assert [(s["text"], s.get("optional")) for s in cl] == [
        ("Laptop", None), ("Benefits enrollment", True), ("Badge", None), ("Parking", None)]


def test_put_keeps_the_flag_unless_sent_and_audits_a_change(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=STEPS).json()["id"]
    cl = _cl(team, pid)
    # The item page sends {id, text} only: the flag survives a rename/reorder.
    r = client.put(f"/api/items/{pid}/checklist", headers=admin_headers,
                   json={"steps": [{"id": cl[1]["id"], "text": "Benefits"}, {"id": cl[0]["id"], "text": "Laptop"}]})
    assert r.status_code == 200
    assert [(s["text"], s.get("optional")) for s in _cl(team, pid)] == [("Benefits", True), ("Laptop", None)]
    # Sent explicitly, it is applied both ways; a new step can arrive optional.
    cl = _cl(team, pid)
    client.put(f"/api/items/{pid}/checklist", headers=admin_headers, json={"steps": [
        {"id": cl[0]["id"], "text": "Benefits", "optional": False},
        {"id": cl[1]["id"], "text": "Laptop", "optional": True},
        {"text": "Parking", "optional": True}]})
    assert [(s["text"], s.get("optional")) for s in _cl(team, pid)] == [
        ("Benefits", None), ("Laptop", True), ("Parking", True)]
    with server.db(team) as c:
        ch = json.loads(c.execute("SELECT changes FROM audit_log WHERE project_id=? AND action='checklist:edit' "
                                  "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()["changes"])
    assert ch["optional_changed"] == ["Benefits", "Laptop"] and ch["added"] == ["Parking"]


def test_tick_keeps_the_flag(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=STEPS).json()["id"]
    sid = _cl(team, pid)[1]["id"]
    client.post(f"/api/items/{pid}/checklist/{sid}/check", json={"done": True}, headers=admin_headers)
    st = _cl(team, pid)[1]
    assert st["done"] is True and st["optional"] is True


def test_recurrence_keeps_the_flag(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=STEPS, recurrence="weekly", start="2026-01-05", dueWeeks=1).json()["id"]
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    assert [s.get("optional") for s in _cl(team, child["id"])] == [None, True, None]


# ── Frontend wiring guards (behaviour verified headlessly in a vm harness during the build) ─────

_HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8")


def test_counts_and_close_prompt_skip_optional_steps():
    counts = [l for l in _HTML.splitlines() if l.startswith("function checklistCounts(p)")][0]
    assert "req = l.filter(s => !s.optional), base = req.length ? req : l" in counts
    assert "return checklistOf(p).filter(s => !s.done && !s.optional);" in _HTML


def test_template_steps_round_trip_and_admin_gets_the_toggle():
    assert "t.checklist.map(_clStepObj)" in _HTML                                  # strings and objects both read
    assert "{ optionToggle: true }" in _HTML                                         # Admin > Types only
    assert "(x.optional ? { text: x.text, optional: true } : x.text)" in _HTML     # required stays a plain string
    assert "return body._clSteps();" in _HTML                                        # New Item sends the flag
    assert "class=\"cl-opt\"" in _HTML and "cl-optbtn" in _HTML


def test_add_a_checklist_only_on_types_with_a_template():
    # CHECKLIST-TMPL-ONLY-1: the item page's "+ Add a checklist" shows only when the item's Type has a template.
    assert "const tn = _clTypeSteps(p && p.type).length;" in _HTML and "if(mode==='page' && canEdit && tn){" in _HTML
