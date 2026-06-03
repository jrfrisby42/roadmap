"""Planning sessions: lifecycle (draft → list/get → discard), payload validation,
role gating, and the atomic commit that applies status changes through the
config-driven status-flag maps (never hardcoded status names)."""


def _create_item(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()["id"]


def _create_session(client, headers, name="Sprint 24", stype="Sprint"):
    return client.post("/api/planning-sessions", json={"name": name, "type": stype},
                       headers=headers)


def _get_item(client, headers, pid):
    allr = client.get("/api/all", headers=headers).json()
    return next(p for p in allr["projects"] if p["id"] == pid)


# ── Creation / validation / role gating ───────────────────────────────────────
def test_create_session_requires_name(client, admin_headers):
    r = client.post("/api/planning-sessions", json={"name": "", "type": "Sprint"},
                    headers=admin_headers)
    assert r.status_code == 400


def test_create_session_bad_type(client, admin_headers):
    r = client.post("/api/planning-sessions", json={"name": "X", "type": "Bogus"},
                    headers=admin_headers)
    assert r.status_code == 400


def test_create_session_role_gating(client, viewer_headers):
    assert _create_session(client, viewer_headers).status_code == 403


# ── Lifecycle ─────────────────────────────────────────────────────────────────
def test_session_lifecycle(client, admin_headers):
    sid = _create_session(client, admin_headers).json()["id"]

    listing = client.get("/api/planning-sessions", headers=admin_headers).json()
    assert any(s["id"] == sid and s["status"] == "draft" for s in listing)

    got = client.get(f"/api/planning-sessions/{sid}", headers=admin_headers)
    assert got.status_code == 200 and got.json()["type"] == "Sprint"

    upd = client.put(f"/api/planning-sessions/{sid}/draft",
                     json={"payload": {"sprint_items": []}}, headers=admin_headers)
    assert upd.status_code == 200

    disc = client.delete(f"/api/planning-sessions/{sid}", headers=admin_headers)
    assert disc.status_code == 200 and disc.json()["status"] == "discarded"


def test_get_missing_session_404(client, admin_headers):
    assert client.get("/api/planning-sessions/ps_nope", headers=admin_headers).status_code == 404


# ── Commit: Sprint applies the first active status + start date ────────────────
def test_commit_sprint_sets_active_status_and_start(client, admin_headers):
    client.put("/api/config/statusIsActive", json={"In Progress": True}, headers=admin_headers)
    pid = _create_item(client, admin_headers, status="Planned")
    sid = _create_session(client, admin_headers).json()["id"]

    r = client.post(f"/api/planning-sessions/{sid}/commit", json={
        "name": "Sprint 24", "type": "Sprint",
        "sprint_items": [{"id": pid, "start": "2026-07-01"}],
    }, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["items_changed"] == 1

    item = _get_item(client, admin_headers, pid)
    assert item["status"] == "In Progress"
    assert item["start"] == "2026-07-01"


def test_commit_sprint_missing_start_rejected(client, admin_headers):
    pid = _create_item(client, admin_headers)
    sid = _create_session(client, admin_headers).json()["id"]
    r = client.post(f"/api/planning-sessions/{sid}/commit", json={
        "name": "Sprint 24", "type": "Sprint",
        "sprint_items": [{"id": pid}],  # no start
    }, headers=admin_headers)
    assert r.status_code == 422


# ── Commit: Review applies the approved status ─────────────────────────────────
def test_commit_review_sets_approved_status(client, admin_headers):
    client.put("/api/config/statusIsApproved", json={"In Testing": True}, headers=admin_headers)
    pid = _create_item(client, admin_headers, status="Planned")
    sid = _create_session(client, admin_headers, name="Review wk24", stype="Review").json()["id"]

    r = client.post(f"/api/planning-sessions/{sid}/commit", json={
        "name": "Review wk24", "type": "Review", "approved_ids": [pid],
    }, headers=admin_headers)
    assert r.status_code == 200
    assert _get_item(client, admin_headers, pid)["status"] == "In Testing"


# ── Commit: deferral applies deferred status + flags and clears schedule ───────
def test_commit_defers_item(client, admin_headers):
    client.put("/api/config/statusIsDeferred", json={"Backlogged": True}, headers=admin_headers)
    pid = _create_item(client, admin_headers, status="Planned", start="2026-06-01",
                       revised="2026-07-01")
    sid = _create_session(client, admin_headers, name="Review wk24", stype="Review").json()["id"]

    r = client.post(f"/api/planning-sessions/{sid}/commit", json={
        "name": "Review wk24", "type": "Review",
        "deferred_items": [{"id": pid, "reason": "Not Ready", "note": "later"}],
    }, headers=admin_headers)
    assert r.status_code == 200

    item = _get_item(client, admin_headers, pid)
    assert item["status"] == "Backlogged"
    assert item["deferred"] is True
    assert item["deferReason"] == "Not Ready"
    assert item["start"] == ""        # schedule fields cleared on deferral


# ── Commit: Release requires a release number ──────────────────────────────────
def test_commit_release_requires_release_number(client, admin_headers):
    sid = _create_session(client, admin_headers, name="R1", stype="Release").json()["id"]
    r = client.post(f"/api/planning-sessions/{sid}/commit", json={
        "name": "R1", "type": "Release", "release_number": "",
    }, headers=admin_headers)
    assert r.status_code == 422
