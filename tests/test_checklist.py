"""CHECKLIST-1: tickable steps on an item.

`checklist` is a blob array of {id, text, done, by?, at?}, kept OUT of the description. It is in
SERVER_OWNED_FIELDS, so the generic item PUT restores it and only the two dedicated routes write it:
POST /api/items/{pid}/checklist/{sid}/check (tick/untick; admin/editor/contributor-in-scope) and
PUT /api/items/{pid}/checklist (add/rename/reorder/remove; admin/editor). A create may seed step
TEXT (the New Item form, from the Type template) - always unticked. Recurrence keeps the steps but
unticks them. Checklist writes do not bump updated_ts, so a tick never 409s someone's open modal.
"""
import json

import server


def _create(client, headers, **fields):
    return client.post("/api/projects", json={"name": "Deploy", "status": "Planned", **fields}, headers=headers)


def _stored(team, pid):
    with server.db(team) as c:
        row = c.execute("SELECT data, updated_ts FROM projects WHERE id=?", (pid,)).fetchone()
    p = json.loads(row["data"]); p["_ts"] = row["updated_ts"]
    return p


def _hdr(team, user, role):
    return {"Authorization": "Bearer " + server.create_token(team, user, role), "X-Team": team}


def _audit(team, pid):
    with server.db(team) as c:
        return [r["action"] for r in c.execute("SELECT action FROM audit_log WHERE project_id=? ORDER BY id", (pid,))]


STEPS = ["Merge to main", "Back up the database", "Copy the files"]


# ── Create ───────────────────────────────────────────────────────────────────────

def test_create_seeds_unticked_steps_and_ignores_forged_ticks(client, team, admin_headers):
    r = _create(client, admin_headers, checklist=[
        "  Merge   to main ", {"text": "Back up the database", "done": True, "by": "mallory", "at": "2020-01-01"},
        "", {"text": "   "}, 42, "Copy the files"])
    assert r.status_code == 200
    cl = _stored(team, r.json()["id"])["checklist"]
    assert [s["text"] for s in cl] == ["Merge to main", "Back up the database", "42", "Copy the files"]
    assert all(s["done"] is False and "by" not in s and "at" not in s and s["id"] for s in cl)
    assert len({s["id"] for s in cl}) == 4


def test_create_without_steps_has_no_checklist_field(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=["", "  "]).json()["id"]
    assert "checklist" not in _stored(team, pid)


def test_create_caps_steps(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=["s%d" % i for i in range(80)]).json()["id"]
    assert len(_stored(team, pid)["checklist"]) == server._CHECKLIST_CAP


# ── Tick / untick ────────────────────────────────────────────────────────────────

def test_tick_records_who_and_when_and_untick_clears(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=STEPS).json()["id"]
    sid = _stored(team, pid)["checklist"][1]["id"]
    r = client.post(f"/api/items/{pid}/checklist/{sid}/check", json={"done": True}, headers=admin_headers)
    assert r.status_code == 200
    st = _stored(team, pid)["checklist"][1]
    assert st["done"] is True and st["by"] == "admin" and st["at"]
    assert r.json()["checklist"][1]["done"] is True
    client.post(f"/api/items/{pid}/checklist/{sid}/check", json={"done": False}, headers=admin_headers)
    st = _stored(team, pid)["checklist"][1]
    assert st["done"] is False and "by" not in st and "at" not in st
    assert _audit(team, pid).count("checklist:check") == 1 and _audit(team, pid).count("checklist:uncheck") == 1


def test_tick_is_idempotent_and_keeps_the_first_ticker(client, team, admin_headers, editor_headers):
    pid = _create(client, admin_headers, checklist=STEPS).json()["id"]
    sid = _stored(team, pid)["checklist"][0]["id"]
    client.post(f"/api/items/{pid}/checklist/{sid}/check", json={"done": True}, headers=admin_headers)
    first = _stored(team, pid)["checklist"][0]
    client.post(f"/api/items/{pid}/checklist/{sid}/check", json={"done": True}, headers=editor_headers)
    again = _stored(team, pid)["checklist"][0]
    assert again["by"] == "admin" and again["at"] == first["at"]
    assert _audit(team, pid).count("checklist:check") == 1


def test_tick_does_not_bump_updated_ts(client, team, admin_headers):
    """A tick is not an edit of the item: someone's open modal (base updated_ts) must still save."""
    created = _create(client, admin_headers, checklist=STEPS).json()
    pid = created["id"]
    before = _stored(team, pid)
    sid = before["checklist"][0]["id"]
    client.post(f"/api/items/{pid}/checklist/{sid}/check", json={"done": True}, headers=admin_headers)
    assert _stored(team, pid)["_ts"] == before["_ts"]
    r = client.put(f"/api/projects/{pid}", json={"name": "Deploy v2", "status": "Planned", "_baseUpdatedTs": before["_ts"]},
                   headers=admin_headers)
    assert r.status_code == 200
    assert _stored(team, pid)["checklist"][0]["done"] is True   # the stale save did not undo the tick


def test_tick_unknown_step_404_and_roles(client, team, admin_headers, viewer_headers):
    pid = _create(client, admin_headers, checklist=STEPS).json()["id"]
    sid = _stored(team, pid)["checklist"][0]["id"]
    assert client.post(f"/api/items/{pid}/checklist/nope/check", json={"done": True}, headers=admin_headers).status_code == 404
    assert client.post(f"/api/items/{pid}/checklist/{sid}/check", json={"done": True}, headers=viewer_headers).status_code == 403
    assert client.post(f"/api/items/9999/checklist/{sid}/check", json={"done": True}, headers=admin_headers).status_code == 404


def test_contributor_ticks_only_in_scope(client, team, admin_headers):
    contrib = _hdr(team, "contrib1", "contributor")
    mine = _create(client, admin_headers, checklist=STEPS, assignee="contrib1").json()["id"]
    other = _create(client, admin_headers, checklist=STEPS, assignee="someone").json()["id"]
    s1 = _stored(team, mine)["checklist"][0]["id"]; s2 = _stored(team, other)["checklist"][0]["id"]
    assert client.post(f"/api/items/{mine}/checklist/{s1}/check", json={"done": True}, headers=contrib).status_code == 200
    assert client.post(f"/api/items/{other}/checklist/{s2}/check", json={"done": True}, headers=contrib).status_code == 403
    # ...but contributors never reshape the list
    assert client.put(f"/api/items/{mine}/checklist", json={"steps": []}, headers=contrib).status_code == 403


def test_contributor_save_with_a_stale_checklist_is_not_an_illegal_edit(client, team, admin_headers):
    """The contributor's page loaded before someone ticked a step; their status save resends the
    stale checklist. It must not 403 as an illegal field edit, and must not undo the tick."""
    contrib = _hdr(team, "contrib1", "contributor")
    pid = _create(client, admin_headers, checklist=STEPS, assignee="contrib1", description="x").json()["id"]
    stale = _stored(team, pid); stale_cl = stale["checklist"]
    client.post(f"/api/items/{pid}/checklist/{stale_cl[0]['id']}/check", json={"done": True}, headers=admin_headers)
    body = {k: v for k, v in stale.items() if k != "_ts"}
    body["description"] = "updated by contributor"
    r = client.put(f"/api/projects/{pid}", json=body, headers=contrib)
    assert r.status_code == 200, r.text
    after = _stored(team, pid)
    assert after["description"] == "updated by contributor" and after["checklist"][0]["done"] is True


# ── Generic PUT can't write it ───────────────────────────────────────────────────

def test_generic_put_cannot_forge_or_wipe_the_checklist(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=STEPS).json()["id"]
    orig = _stored(team, pid)["checklist"]
    forged = [{"id": "x", "text": "forged", "done": True, "by": "mallory"}]
    client.put(f"/api/projects/{pid}", json={"name": "Deploy", "status": "Planned", "checklist": forged}, headers=admin_headers)
    assert _stored(team, pid)["checklist"] == orig
    client.put(f"/api/projects/{pid}", json={"name": "Deploy", "status": "Planned"}, headers=admin_headers)
    assert _stored(team, pid)["checklist"] == orig


# ── Reshape (item page) ──────────────────────────────────────────────────────────

def test_put_reorders_renames_adds_removes_and_keeps_ticks(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=STEPS).json()["id"]
    a, b, c = _stored(team, pid)["checklist"]
    client.post(f"/api/items/{pid}/checklist/{b['id']}/check", json={"done": True}, headers=admin_headers)
    r = client.put(f"/api/items/{pid}/checklist", json={"steps": [
        {"id": b["id"], "text": "Back up the prod database"},   # renamed + moved first: keeps its tick
        {"text": "Restart the service"},                         # new
        {"id": a["id"], "text": a["text"]},                       # c removed
        {"id": "made-up", "text": "Smoke test", "done": True},   # unknown id -> new, unticked
    ]}, headers=admin_headers)
    assert r.status_code == 200
    cl = _stored(team, pid)["checklist"]
    assert [s["text"] for s in cl] == ["Back up the prod database", "Restart the service", "Merge to main", "Smoke test"]
    assert cl[0]["id"] == b["id"] and cl[0]["done"] is True and cl[0]["by"] == "admin"
    assert cl[1]["done"] is False and cl[3]["done"] is False and cl[3]["id"] != "made-up"
    assert c["id"] not in {s["id"] for s in cl}
    assert "checklist:edit" in _audit(team, pid)


def test_put_duplicate_ids_do_not_clone_a_tick(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=STEPS).json()["id"]
    a = _stored(team, pid)["checklist"][0]
    client.post(f"/api/items/{pid}/checklist/{a['id']}/check", json={"done": True}, headers=admin_headers)
    client.put(f"/api/items/{pid}/checklist", json={"steps": [{"id": a["id"], "text": "x"}, {"id": a["id"], "text": "y"}]},
               headers=admin_headers)
    cl = _stored(team, pid)["checklist"]
    assert [s["done"] for s in cl] == [True, False] and cl[0]["id"] != cl[1]["id"]


def test_put_empty_removes_the_checklist(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=STEPS).json()["id"]
    r = client.put(f"/api/items/{pid}/checklist", json={"steps": []}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["checklist"] == []
    assert "checklist" not in _stored(team, pid)


def test_put_validation_and_roles(client, team, admin_headers, viewer_headers, editor_headers):
    pid = _create(client, admin_headers).json()["id"]
    assert client.put(f"/api/items/{pid}/checklist", json={"steps": "nope"}, headers=admin_headers).status_code == 422
    too_many = [{"text": "s%d" % i} for i in range(server._CHECKLIST_CAP + 1)]
    assert client.put(f"/api/items/{pid}/checklist", json={"steps": too_many}, headers=admin_headers).status_code == 422
    assert client.put(f"/api/items/{pid}/checklist", json={"steps": [{"text": "a"}]}, headers=viewer_headers).status_code == 403
    r = client.put(f"/api/items/{pid}/checklist", json={"steps": [{"text": "a"}]}, headers=editor_headers)
    assert r.status_code == 200 and _stored(team, pid)["checklist"][0]["text"] == "a"


# ── Recurrence ───────────────────────────────────────────────────────────────────

def test_recurrence_keeps_steps_but_unticks_them(client, team, admin_headers):
    pid = _create(client, admin_headers, checklist=STEPS, recurrence="weekly", start="2026-01-05", dueWeeks=1).json()["id"]
    parent = _stored(team, pid)["checklist"]
    for st in parent[:2]:
        client.post(f"/api/items/{pid}/checklist/{st['id']}/check", json={"done": True}, headers=admin_headers)
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    cl = _stored(team, child["id"])["checklist"]
    assert [s["text"] for s in cl] == STEPS
    assert all(s["done"] is False and "by" not in s and "at" not in s for s in cl)
    assert not ({s["id"] for s in cl} & {s["id"] for s in parent})
    assert [s["done"] for s in _stored(team, pid)["checklist"]] == [True, True, False]   # parent untouched


def test_checklist_is_server_owned_and_inherited():
    assert "checklist" in server.SERVER_OWNED_FIELDS
    assert "checklist" in server.RECURRENCE_INHERITED and "checklist" not in server.RECURRENCE_SKIP_KEYS


# ── Frontend wiring guards (string-level; the UI itself is verified headlessly) ─────

import pathlib
_HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8")


def test_item_page_puts_the_checklist_right_under_the_description():
    order = _HTML.split("function _frzOrderMain(){", 1)[1].split("forEach(function(n)", 1)[0]
    assert order.index("cardOf('ipNotes')") < order.index("'frzChecklistCard'") < order.index("'frzAttachCard'")


def test_modal_and_panel_mount_compact_and_page_mounts_full():
    assert "checklistMount(host, p, { mode: 'compact' })" in _HTML                          # edit modal
    assert "_call('checklistMount', _clh, p, {mode:'compact'})" in _HTML                     # List panel
    assert "_call('checklistMount', card, p, {mode:'page'})" in _HTML                        # item page
    assert "let open = mode === 'page';" in _HTML                                             # compact starts collapsed
    assert "const canTick = _clCanTick(p), canEdit = mode==='page' && _clCanEdit();" in _HTML  # add/remove on the page only


def test_modal_description_keeps_its_space():
    row = _HTML.split('id="modalChecklistRow"', 1)[1][:80]
    assert "flex:none" in row
    assert '<div class="form-row composer-desc" style="flex:1;' in _HTML


def test_every_person_close_path_asks_about_open_steps():
    assert "checklistCloseOk(existing, data.status)" in _HTML                 # edit modal
    assert "!(await checklistCloseOk(p, value))" in _HTML                      # item page + List panel (saveItemPageField)
    assert "checklistBulkCloseOk(ids, value)" in _HTML                         # List bulk edit
    assert "_call('checklistCloseOk', p, newStatus, {completionNote:true})" in _HTML   # Flow Kanban drop
    assert "Move to '+newStatus+' anyway" in _HTML                             # never blocks


def test_create_sends_template_steps_and_admin_authors_them():
    assert "checklist: _clCreateSteps()" in _HTML
    assert "types[i].checklist = steps.slice(0, 50)" in _HTML
    assert "_clTypeSteps(tname)" in _HTML
