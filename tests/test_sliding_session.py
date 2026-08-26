"""SESSION-SLIDE-1: sliding-session renewal + token generation ("sign out everywhere").

Renewal is delivered as an `X-Renew-Token` response header from `require_auth`, only past
half-life, and only AFTER the revocation + generation checks (so a revoked or bumped user never
receives a fresh token). Generation is a live per-request check on a `config['users']` field
(no DB column). Legacy 4-field tokens (no gen) minted before the deploy keep working.
"""
import base64
import json
import time

import server

TTL = server._TOKEN_EXPIRY          # 604800 (7 days)
HALF = TTL // 2


def _tok(team, user, role, remaining, gen=0):
    """A 5-field token with a controlled remaining lifetime (seconds until expiry)."""
    expiry = int(time.time()) + remaining
    payload = f"{team}:{user}:{role}:{expiry}:{gen}"
    return base64.urlsafe_b64encode(f"{payload}:{server._sign(payload)}".encode()).decode()


def _legacy_tok(team, user, role, remaining):
    """A legacy 4-field token (no gen) - what a pre-deploy session presents."""
    expiry = int(time.time()) + remaining
    payload = f"{team}:{user}:{role}:{expiry}"
    return base64.urlsafe_b64encode(f"{payload}:{server._sign(payload)}".encode()).decode()


def _hdr(team, token):
    return {"Authorization": "Bearer " + token, "X-Team": team}


def _set_users(team, users):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('users',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(users),))


# ── Renewal timing ──────────────────────────────────────────────────────────────────

def test_fresh_token_is_not_renewed(client, team):
    r = client.get("/api/all", headers=_hdr(team, _tok(team, "u1", "admin", TTL)))
    assert r.status_code == 200
    assert "X-Renew-Token" not in r.headers   # more than half a lifetime left -> no renewal


def test_past_halflife_token_is_renewed(client, team):
    r = client.get("/api/all", headers=_hdr(team, _tok(team, "u1", "admin", 100)))
    assert r.status_code == 200
    nt = r.headers.get("X-Renew-Token")
    assert nt, "expected a renewal header past half-life"
    d = server.decode_token(nt)                 # the renewed token is valid...
    assert d["team"] == team and d["username"] == "u1" and d["role"] == "admin" and d["gen"] == 0
    assert d["expiry"] > int(time.time()) + HALF   # ...with a full fresh lifetime


# ── Legacy (pre-deploy) tokens ────────────────────────────────────────────────────────

def test_legacy_4field_token_authenticates_and_upgrades(client, team):
    r = client.get("/api/all", headers=_hdr(team, _legacy_tok(team, "u1", "editor", 100)))
    assert r.status_code == 200                 # gen treated as 0 -> authenticates
    nt = r.headers.get("X-Renew-Token")
    assert nt, "a legacy token past half-life should be renewed (upgraded)"
    d = server.decode_token(nt)                 # renewed token is a valid 5-field token, gen 0
    assert d["gen"] == 0 and d["username"] == "u1" and d["role"] == "editor"


# ── Generation ("sign out everywhere") ────────────────────────────────────────────────

def test_generation_mismatch_rejected(client, team):
    _set_users(team, [{"username": "alice", "role": "editor", "tokenGen": 5}])
    r = client.get("/api/all", headers=_hdr(team, _tok(team, "alice", "editor", TTL, gen=0)))
    assert r.status_code == 401


def test_generation_match_ok(client, team):
    _set_users(team, [{"username": "alice", "role": "editor", "tokenGen": 5}])
    r = client.get("/api/all", headers=_hdr(team, _tok(team, "alice", "editor", TTL, gen=5)))
    assert r.status_code == 200


def test_absent_tokengen_reads_as_zero(client, team):
    _set_users(team, [{"username": "alice", "role": "editor"}])   # no tokenGen field
    assert client.get("/api/all", headers=_hdr(team, _tok(team, "alice", "editor", TTL, gen=0))).status_code == 200


# ── Revoked user never renews (the 2.2 invariant) ─────────────────────────────────────

def test_revoked_user_rejected_and_not_renewed(client, team):
    _set_users(team, [{"username": "alice", "role": "editor", "revokedAt": 123}])
    r = client.get("/api/all", headers=_hdr(team, _tok(team, "alice", "editor", 100)))   # past half-life
    assert r.status_code == 401
    assert "X-Renew-Token" not in r.headers   # renewal runs AFTER the revocation check


# ── The signout-all endpoint ──────────────────────────────────────────────────────────

def test_signout_all_bumps_generation_and_kills_old_tokens(client, team, admin_headers):
    _set_users(team, [{"username": "admin", "role": "admin", "builtin": True},
                      {"username": "alice", "role": "editor", "tokenGen": 0}])
    old = _tok(team, "alice", "editor", TTL, gen=0)
    assert client.get("/api/all", headers=_hdr(team, old)).status_code == 200   # works before
    r = client.post("/api/users/alice/signout-all", headers=admin_headers)
    assert r.status_code == 200 and r.json()["tokenGen"] == 1
    assert client.get("/api/all", headers=_hdr(team, old)).status_code == 401   # old gen now dead
    # A fresh login-equivalent token at the new generation works again.
    assert client.get("/api/all", headers=_hdr(team, _tok(team, "alice", "editor", TTL, gen=1))).status_code == 200


def test_signout_all_requires_admin(client, team, editor_headers, viewer_headers):
    _set_users(team, [{"username": "alice", "role": "editor", "tokenGen": 0}])
    assert client.post("/api/users/alice/signout-all", headers=editor_headers).status_code == 403
    assert client.post("/api/users/alice/signout-all", headers=viewer_headers).status_code == 403


def test_signout_all_missing_user_404(client, team, admin_headers):
    _set_users(team, [{"username": "admin", "role": "admin", "builtin": True}])
    assert client.post("/api/users/ghost/signout-all", headers=admin_headers).status_code == 404


# ── Renewed session still resolves role/team/username ─────────────────────────────────

def test_role_gate_holds_on_a_near_expiry_token(client, team):
    # A viewer past half-life is still a viewer: renewal preserves role, and a role-gated write is refused.
    r = client.post("/api/projects", json={"name": "X", "status": "Planned"},
                    headers=_hdr(team, _tok(team, "v1", "viewer", 100)))
    assert r.status_code == 403   # viewer cannot create; role resolved correctly from the (renewing) token
