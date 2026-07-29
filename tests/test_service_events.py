"""WRITE-1b: resolution -> AssetHub ServiceEvent, offline.

When an item transitions INTO a terminal status and is linked to assets, Flow sends one
ServiceEvent per linked asset through the existing AssetHubClient, attributed to the assignee,
once, idempotently, and NON-blocking (a BackgroundTask). Every AssetHub call is mocked - no
network. The send is best-effort: it never fails or rolls back the resolution, records a durable
per-asset outcome (`assetServiceSync`), and never starts a timer or an unbounded retry.

Trigger = the TRANSITION (prev not terminal, new terminal), fired server-side at update_project,
so it holds for the modal / item page / Kanban drag / API alike.
"""
import json

import pytest

import server

KEY = "ahk_aaaaaaaaaaaaaaaa_" + "b" * 52
UUID1 = "11111111-1111-4111-8111-111111111111"
UUID2 = "22222222-2222-4222-8222-222222222222"
TERMINAL = "Released"       # default statusIsTerminal for a fresh team
NONTERMINAL = "Planned"


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


def _stored(team, pid):
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    return json.loads(row["data"]) if row else None


def _asset(uid=UUID1, tag="IT-1"):
    return {"id": uid, "asset_tag": tag, "name": "Dev Laptop", "serial_number": "S1",
            "category": {"id": "cat-1", "code": "IT", "name": "IT/Tech"},
            "status": {"value": "in_stock", "label": "In Stock"}, "location": None}


def _detail_ok(path, params):
    pubid = path.rsplit("/", 1)[-1]
    return {"data": _asset(pubid, "TAG-" + pubid[:4]), "correlation_id": "c"}


def _post_created(path, body, params):
    return {"data": {"idempotent_replay": False, "attribution": {"type": "person"},
                     "source_reference": body.get("source_reference")}, "correlation_id": "c-post"}


def _post_replay(path, body, params):
    return {"data": {"idempotent_replay": True, "attribution": {"type": "integration"}},
            "correlation_id": "c-post"}


def _post_raise(code, status):
    def h(path, body, params):
        raise server.AssetHubError(code, status=status, correlation_id="c-err")
    return h


def _install(monkeypatch, *, post_handler=_post_created, get_handler=_detail_ok):
    """Replace server.AssetHubClient with a fake exposing BOTH .get (link/refresh) and .post
    (service-event send). Records constructor roles and every get/post."""
    state = {"gets": [], "posts": [], "roles": []}

    class Fake:
        def __init__(self, team, role, **kw):
            state["roles"].append(role)

        def get(self, path, params=None):
            state["gets"].append({"path": path, "params": params})
            return get_handler(path, params)

        def post(self, path, body, params=None):
            state["posts"].append({"path": path, "body": body, "params": params})
            return post_handler(path, body, params)

    monkeypatch.setattr(server, "AssetHubClient", Fake)
    return state


def _enable(team, monkeypatch):
    monkeypatch.setenv("ASSETHUB_API_KEY_" + team.upper(), KEY)
    _set_cfg(team, "assethubConnection", {"providerEnvironment": "production", "assethubTeam": "IT"})


def _seed_user(team, username="dev1", email="dev1@freezingpointllc.com", revoked=False):
    u = {"username": username, "role": "editor"}
    if email:
        u["email"] = email
    if revoked:
        u["revokedAt"] = "2026-01-01T00:00:00Z"
    _set_cfg(team, "users", [{"username": "admin", "role": "admin", "builtin": True}, u])


def _create(client, headers, **fields):
    body = {"name": "Laptop replacement", "status": NONTERMINAL, **fields}
    return client.post("/api/projects", json=body, headers=headers).json()["id"]


def _link(client, headers, pid, pubid=UUID1):
    return client.post(f"/api/items/{pid}/asset-links", json={"publicId": pubid, "role": "related"},
                       headers=headers)


def _resolve(client, headers, pid, status=TERMINAL):
    return client.put(f"/api/projects/{pid}", json={"status": status}, headers=headers)


def _setup_linked(client, admin_headers, team, monkeypatch, *, assignee="dev1", email="dev1@freezingpointllc.com",
                  revoked=False, pubids=(UUID1,)):
    """Enable AssetHub, link the given assets, seed the assignee user. Returns (pid, state)."""
    _enable(team, monkeypatch)
    state = _install(monkeypatch)
    pid = _create(client, admin_headers, assignee=assignee)
    for u in pubids:
        assert _link(client, admin_headers, pid, u).status_code == 200
    if assignee:
        _seed_user(team, assignee, email, revoked)
    return pid, state


# ── the trigger + a well-formed send ─────────────────────────────────────────────

def test_transition_sends_one_wellformed_request_per_asset(client, team, admin_headers, monkeypatch):
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch, pubids=(UUID1, UUID2))
    state = _install(monkeypatch)                       # fresh counter for the resolve
    assert _resolve(client, admin_headers, pid).status_code == 200
    assert len(state["posts"]) == 2                     # one per linked asset
    for p in state["posts"]:
        b = p["body"]
        assert b["source_system"] == "flow"
        assert b["source_reference"] == str(pid)        # the numeric id, not the item key
        assert b["service_type"] == "Other"
        assert b["summary"]
        assert b["actor_email"] == "dev1@freezingpointllc.com"
        assert p["path"].endswith("/service-events")
    assert UUID1 in state["posts"][0]["path"] or UUID1 in state["posts"][1]["path"]


def test_source_reference_is_numeric_id_not_item_key(client, team, admin_headers, monkeypatch):
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch)
    stored = _stored(team, pid)
    assert stored.get("itemKey")                        # the item HAS a display key...
    state = _install(monkeypatch)
    _resolve(client, admin_headers, pid)
    assert state["posts"][0]["body"]["source_reference"] == str(pid)   # ...but we send the id


# ── things that must NOT send ─────────────────────────────────────────────────────

def test_already_terminal_save_sends_nothing(client, team, admin_headers, monkeypatch):
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch)
    _resolve(client, admin_headers, pid)                # transition 1: sends
    state = _install(monkeypatch)                       # reset counter
    # A save that leaves the already-terminal item terminal (no transition).
    assert client.put(f"/api/projects/{pid}", json={"status": TERMINAL, "name": "Renamed"},
                      headers=admin_headers).status_code == 200
    assert state["posts"] == []


def test_no_asset_links_sends_nothing(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    state = _install(monkeypatch)
    _seed_user(team)
    pid = _create(client, admin_headers, assignee="dev1")   # no link
    assert _resolve(client, admin_headers, pid).status_code == 200
    assert state["posts"] == []


def test_not_configured_team_sends_nothing(client, team, admin_headers, monkeypatch):
    # No env key / connection -> _assethub_configured False -> inert (the common case: every team but it).
    state = _install(monkeypatch)
    pid = _create(client, admin_headers, assignee="dev1")
    assert _resolve(client, admin_headers, pid).status_code == 200
    assert state["posts"] == []


# ── actor resolution (Part 3) ─────────────────────────────────────────────────────

def test_assignee_email_is_sent(client, team, admin_headers, monkeypatch):
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch)
    state = _install(monkeypatch)
    _resolve(client, admin_headers, pid)
    assert state["posts"][0]["body"]["actor_email"] == "dev1@freezingpointllc.com"


def test_unassigned_omits_actor_and_skips_send(client, team, admin_headers, monkeypatch):
    # No assignee -> no resolvable actor. The contract REQUIRES actor_email (missing == 422), so
    # rather than send a doomed request or an empty string, we skip and record the gap.
    _enable(team, monkeypatch)
    _install(monkeypatch)
    pid = _create(client, admin_headers)                # no assignee
    assert _link(client, admin_headers, pid).status_code == 200
    state = _install(monkeypatch)
    _resolve(client, admin_headers, pid)
    assert state["posts"] == []                         # nothing sent, no empty-string actor
    sync = _stored(team, pid)["assetServiceSync"][UUID1]
    assert sync["state"] == "skipped_no_actor"
    assert sync["actorEmailSent"] is False


def test_assignee_without_email_omits_actor_and_skips_send(client, team, admin_headers, monkeypatch):
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch, assignee="dev1", email="")
    state = _install(monkeypatch)
    _resolve(client, admin_headers, pid)
    assert state["posts"] == []
    assert _stored(team, pid)["assetServiceSync"][UUID1]["state"] == "skipped_no_actor"


def test_revoked_assignee_still_resolves_and_sends(client, team, admin_headers, monkeypatch):
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch, revoked=True)
    state = _install(monkeypatch)
    _resolve(client, admin_headers, pid)
    assert len(state["posts"]) == 1
    assert state["posts"][0]["body"]["actor_email"] == "dev1@freezingpointllc.com"


def test_missing_and_empty_actor_treated_identically():
    # WRITE-1a treats a missing and an empty actor_email identically (both hit `if not actor_email`
    # -> 422); so Flow's "omit" and "empty" collapse to the same no-actor branch. We build no body
    # that carries actor_email="" - it is either present-and-nonempty or absent.
    body = server._assethub_service_event_body({"name": "X"}, 7, "")
    assert "actor_email" not in body
    body2 = server._assethub_service_event_body({"name": "X"}, 7, "a@b.com")
    assert body2["actor_email"] == "a@b.com"


# ── the Contributor distinction (Part 4) ──────────────────────────────────────────

def test_send_runs_under_server_identity_not_contributor(client, team, admin_headers, monkeypatch):
    # The dispatch always constructs the client with the SERVER identity, never a user role, so a
    # Contributor-triggered resolution would still send. (A Contributor cannot actually reach a
    # terminal status - _enforce_contributor_status bars it - so this is proven at the dispatch.)
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch)
    state = _install(monkeypatch)
    server._dispatch_service_events(team, pid, server._ASSETHUB_SYSTEM_ROLE)
    assert len(state["posts"]) == 1
    assert state["roles"] == [server._ASSETHUB_SYSTEM_ROLE]
    assert server._ASSETHUB_SYSTEM_ROLE != "contributor"


def test_contributor_initiated_direct_client_call_still_refused(team):
    # The other direction: a Contributor as the CALLER is still refused at the client entry point,
    # before any request. (Real client, no transport - the actor gate raises first.)
    c = server.AssetHubClient(team, "contributor", api_key="k", base_url="https://x")
    with pytest.raises(server.AssetHubError) as ei:
        c.post("/api/v1/assets/x/service-events", {"a": 1})
    assert ei.value.code == "contributor_forbidden"


def test_contributor_barred_from_terminal_so_no_live_trigger(client, team, contributor_headers,
                                                              admin_headers, monkeypatch):
    # Documents the premise correction: a Contributor cannot drive the trigger via the API at all.
    _enable(team, monkeypatch)
    _install(monkeypatch)
    pid = _create(client, admin_headers, assignee="contrib1")
    _set_cfg(team, "users", [{"username": "contrib1", "role": "contributor", "ownerFilter": "contrib1"}])
    r = client.put(f"/api/projects/{pid}", json={"status": TERMINAL}, headers=contributor_headers)
    assert r.status_code == 403


# ── non-blocking + failure recording (Part 5) ─────────────────────────────────────

def test_resolution_commits_even_when_send_fails(client, team, admin_headers, monkeypatch):
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch)
    state = _install(monkeypatch, post_handler=_post_raise("internal_error", 500))
    r = _resolve(client, admin_headers, pid)
    assert r.status_code == 200
    assert _stored(team, pid)["status"] == TERMINAL          # the resolution committed
    sync = _stored(team, pid)["assetServiceSync"][UUID1]
    assert sync["state"] == "failed" and sync["code"] == "internal_error" and sync["httpStatus"] == 500


def test_send_success_records_sent(client, team, admin_headers, monkeypatch):
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch)
    _install(monkeypatch, post_handler=_post_created)
    _resolve(client, admin_headers, pid)
    sync = _stored(team, pid)["assetServiceSync"][UUID1]
    assert sync["state"] == "sent" and sync["actorEmailSent"] is True


def test_idempotent_replay_recorded_distinctly(client, team, admin_headers, monkeypatch):
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch)
    _install(monkeypatch, post_handler=_post_replay)
    _resolve(client, admin_headers, pid)
    assert _stored(team, pid)["assetServiceSync"][UUID1]["state"] == "replayed"


@pytest.mark.parametrize("code,status", [("not_found", 404), ("missing_scope", 403)])
def test_404_and_403_recorded_distinctly_and_not_retried(client, team, admin_headers, monkeypatch, code, status):
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch)
    state = _install(monkeypatch, post_handler=_post_raise(code, status))
    _resolve(client, admin_headers, pid)
    sync = _stored(team, pid)["assetServiceSync"][UUID1]
    assert sync["state"] == "failed" and sync["code"] == code and sync["httpStatus"] == status
    assert len(state["posts"]) == 1                          # dispatch does not re-send (no retry loop)


def test_dispatch_sends_once_per_asset_no_loop(client, team, admin_headers, monkeypatch):
    # No unbounded retry / no timer at the dispatch level: exactly one post per asset per call.
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch, pubids=(UUID1, UUID2))
    state = _install(monkeypatch, post_handler=_post_raise("internal_error", 500))
    _resolve(client, admin_headers, pid)
    assert len(state["posts"]) == 2                          # one per asset, not more


# ── goes through AssetHubClient, and stays server-owned ───────────────────────────

def test_send_goes_through_assethubclient(client, team, admin_headers, monkeypatch):
    # If the send used a forked urllib path, the monkeypatched client would never see the call.
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch)
    state = _install(monkeypatch)
    _resolve(client, admin_headers, pid)
    assert len(state["posts"]) == 1                          # observed through the client seam


def test_assetservicesync_is_server_owned(client, team, admin_headers, monkeypatch):
    pid, _ = _setup_linked(client, admin_headers, team, monkeypatch)
    _install(monkeypatch)
    _resolve(client, admin_headers, pid)
    assert _stored(team, pid)["assetServiceSync"][UUID1]["state"] == "sent"
    # A client PUT can neither forge nor wipe it.
    client.put(f"/api/projects/{pid}", json={"status": TERMINAL, "assetServiceSync": {"x": "forged"}},
               headers=admin_headers)
    sync = _stored(team, pid)["assetServiceSync"]
    assert "x" not in sync and UUID1 in sync                 # forged key dropped, real record kept


def test_create_strips_client_assetservicesync(client, team, admin_headers):
    pid = client.post("/api/projects", json={"name": "X", "status": NONTERMINAL,
                                             "assetServiceSync": {"y": "z"}}, headers=admin_headers).json()["id"]
    assert "assetServiceSync" not in _stored(team, pid)


def test_recurrence_invariant_holds_with_service_sync():
    unhandled = [f for f in server.SERVER_OWNED_FIELDS
                 if f not in server.RECURRENCE_SKIP_KEYS and f not in server.RECURRENCE_INHERITED]
    assert not unhandled
    assert "assetServiceSync" in server.RECURRENCE_SKIP_KEYS
