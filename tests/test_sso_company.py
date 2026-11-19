"""SSO-COMPANY-1: Companies + "Sign in with Microsoft" (design/sso-company-plan.md).

Microsoft is faked: `_sso_redeem` (the token-endpoint call) is replaced per test with the claims a real ID token
would carry, and the certificate assertion is checked on its own against a throwaway key. Cookies are Secure, so
the client talks https."""
import base64
import json
import os
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

import server

TID = "11111111-2222-3333-4444-555555555555"
CID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OID = "oid-jr-0001"


@pytest.fixture
def key_file(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "key.pem"
    p.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    return str(p), k.public_key()


@pytest.fixture
def company(tmp_path, monkeypatch, key_file, team):
    """One Company holding `team` (plus a second Org) and a separate Company holding a third Org."""
    other = team + "b"
    third = team + "c"
    for t in (other, third):
        os.makedirs(os.path.join(server.TENANTS_DIR, t), exist_ok=True)
        server.init_team_db(t)
    f = tmp_path / "flow-companies.json"
    f.write_text(json.dumps({"companies": [
        {"id": "frazil", "name": "Frazil", "domains": ["freezingpointllc.com", "frazil.com"], "orgs": [team, other],
         "entra": {"tenantId": TID, "clientId": CID, "certKey": key_file[0], "certThumbprint": "88:D7:B4:92:BC:0F:8B:AC:9A:19:D1:28:B8:6B:98:2F:5C:73:CF:95",
                   "certExpires": "2028-11-18"}},
        {"id": "acme", "name": "Acme", "domains": ["acme.com"], "orgs": [third, team],
         "entra": {"tenantId": "acme-tid", "clientId": "acme-cid", "certKey": key_file[0], "certThumbprint": "00"}},
    ]}))
    monkeypatch.setattr(server, "COMPANIES_FILE", str(f))
    server._COMPANIES_CACHE.update(path=None, mtime=None, data=[])
    return {"team": team, "other": other, "third": third, "file": f}


@pytest.fixture
def https():
    return TestClient(server.app, base_url="https://testserver")


def _users(team):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT value FROM config WHERE key='users'").fetchone()["value"])


def _set_users(team, users):
    with server.db(team) as c:
        c.execute("UPDATE config SET value=? WHERE key='users'", (json.dumps(users),))


def _add_user(team, username, email, role="editor", **kw):
    us = _users(team)
    us.append(dict(username=username, email=email, role=role, password=server.hash_password("pw123456"), **kw))
    _set_users(team, us)


def _fake_ms(monkeypatch, **claims):
    """Replace the token-endpoint call: return claims built from the nonce the start step put in the cookie."""
    def redeem(co, code, verifier):
        st = server._sso_unsign(_fake_ms.state_cookie)
        base = {"tid": TID, "iss": f"https://login.microsoftonline.com/{TID}/v2.0", "aud": CID, "nonce": st["nonce"],
                "exp": int(time.time()) + 3600, "oid": OID, "preferred_username": "jr@freezingpointllc.com"}
        base.update(claims)
        return base
    monkeypatch.setattr(server, "_sso_redeem", redeem)


def _sign_in(https, email="jr@freezingpointllc.com", **start):
    r = https.get("/api/auth/sso/microsoft/start", params={"email": email, **start}, follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    if not loc.startswith("https://login.microsoftonline.com/"):
        return r, None
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc).query))
    _fake_ms.state_cookie = https.cookies.get("frazil_sso")
    cb = https.get("/api/auth/sso/microsoft/callback", params={"code": "c0de", "state": q["state"]}, follow_redirects=False)
    return cb, q


# ── Company file ────────────────────────────────────────────────────────────────

def test_no_file_means_no_microsoft_sign_in(https, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COMPANIES_FILE", str(tmp_path / "missing.json"))
    server._COMPANIES_CACHE.update(path=None, mtime=None, data=[])
    assert https.get("/api/auth/sso/enabled").json() == {"enabled": False}
    r = https.get("/api/auth/sso/microsoft/start", params={"email": "jr@frazil.com"}, follow_redirects=False)
    assert r.headers["location"] == "/?sso_error=domain"


def test_a_broken_file_fails_closed(https, monkeypatch, tmp_path):
    f = tmp_path / "bad.json"; f.write_text("{not json")
    monkeypatch.setattr(server, "COMPANIES_FILE", str(f))
    server._COMPANIES_CACHE.update(path=None, mtime=None, data=[])
    assert server._companies() == []


def test_an_org_belongs_to_the_first_company_that_lists_it(company):
    assert server._company_for_org(company["team"])["id"] == "frazil"
    assert server._company_for_org(company["third"])["id"] == "acme"
    assert company["team"] not in server._company_by_id("acme")["orgs"]
    assert server._company_for_email("x@FRAZIL.com")["id"] == "frazil"
    assert server._company_for_email("x@gmail.com") is None


# ── Start + the certificate assertion ───────────────────────────────────────────

def test_start_sends_the_browser_to_the_companys_tenant_with_pkce(https, company):
    r = https.get("/api/auth/sso/microsoft/start", params={"email": "jr@frazil.com", "ret": "/gantt"}, follow_redirects=False)
    u = urllib.parse.urlsplit(r.headers["location"])
    assert u.netloc == "login.microsoftonline.com" and u.path == f"/{TID}/oauth2/v2.0/authorize"
    q = dict(urllib.parse.parse_qsl(u.query))
    assert q["client_id"] == CID and q["code_challenge_method"] == "S256" and q["login_hint"] == "jr@frazil.com"
    assert q["redirect_uri"].endswith("/api/auth/sso/microsoft/callback") and "prompt" not in q
    st = server._sso_unsign(https.cookies.get("frazil_sso"))
    assert st["state"] == q["state"] and st["ret"] == "/gantt"
    assert server._b64u(__import__("hashlib").sha256(st["v"].encode()).digest()) == q["code_challenge"]


def test_unknown_domain_goes_back_to_the_sign_in_page(https, company):
    r = https.get("/api/auth/sso/microsoft/start", params={"email": "someone@gmail.com"}, follow_redirects=False)
    assert r.headers["location"] == "/?sso_error=domain"


def test_silent_recheck_starts_from_the_org(https, company):
    r = https.get("/api/auth/sso/microsoft/start", params={"team": company["team"], "silent": 1}, follow_redirects=False)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))
    assert q["prompt"] == "none" and "login_hint" not in q


def test_ret_is_kept_on_this_site():
    assert server._sso_safe_ret("//evil.com/x") == "/"
    assert server._sso_safe_ret("https://evil.com") == "/"
    assert server._sso_safe_ret("/item/5?x=1") == "/item/5?x=1"


def test_client_assertion_is_signed_by_the_certificate_key(company, key_file):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    co = server._company_by_id("frazil")
    jwt = server._sso_client_assertion(co)
    h, p, s = jwt.split(".")
    hdr, pl = json.loads(server._b64u_dec(h)), json.loads(server._b64u_dec(p))
    assert hdr["alg"] == "RS256" and server._b64u_dec(hdr["x5t"]).hex().upper() == "88D7B492BC0F8BAC9A19D128B86B982F5C73CF95"
    assert pl["iss"] == pl["sub"] == CID and pl["aud"] == f"https://login.microsoftonline.com/{TID}/oauth2/v2.0/token"
    assert pl["exp"] - pl["iat"] <= 300
    key_file[1].verify(server._b64u_dec(s), (h + "." + p).encode(), padding.PKCS1v15(), hashes.SHA256())   # raises if bad


def test_redeem_posts_the_code_verifier_and_assertion(company, monkeypatch):
    seen = {}

    class R:
        def __init__(self, b): self.b = b
        def read(self): return self.b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["form"] = dict(urllib.parse.parse_qsl(req.data.decode()))
        idt = "x." + server._b64u(json.dumps({"oid": "o"}).encode()) + ".y"
        return R(json.dumps({"id_token": idt}).encode())
    monkeypatch.setattr(server, "urlopen", fake_urlopen)
    claims = server._sso_redeem(server._company_by_id("frazil"), "the-code", "the-verifier")
    assert claims == {"oid": "o"}
    assert seen["url"] == f"https://login.microsoftonline.com/{TID}/oauth2/v2.0/token"
    f = seen["form"]
    assert f["code"] == "the-code" and f["code_verifier"] == "the-verifier" and f["grant_type"] == "authorization_code"
    assert f["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer" and f["client_assertion"].count(".") == 2
    assert "client_secret" not in f


# ── Callback: checks, matching, linking ─────────────────────────────────────────

@pytest.mark.parametrize("bad", [{"tid": "other"}, {"aud": "other"}, {"iss": "https://evil/v2.0"}, {"nonce": "zzz"},
                                 {"exp": 1}, {"oid": ""}])
def test_a_token_that_fails_any_check_signs_nobody_in(https, company, monkeypatch, bad):
    _add_user(company["team"], "jr", "jr@freezingpointllc.com")
    _fake_ms(monkeypatch, **bad)
    cb, _ = _sign_in(https)
    assert cb.headers["location"] == "/?sso_error=microsoft"
    assert not any(u.get("entraOid") for u in _users(company["team"]))


def test_a_forged_or_missing_state_is_refused(https, company, monkeypatch):
    _fake_ms(monkeypatch)
    r = https.get("/api/auth/sso/microsoft/callback", params={"code": "c", "state": "nope"}, follow_redirects=False)
    assert r.headers["location"] == "/?sso_error=state"


def test_microsoft_refusing_a_silent_check_lands_on_sign_in(https, company):
    https.get("/api/auth/sso/microsoft/start", params={"team": company["team"], "silent": 1}, follow_redirects=False)
    st = server._sso_unsign(https.cookies.get("frazil_sso"))
    r = https.get("/api/auth/sso/microsoft/callback", params={"error": "login_required", "state": st["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/?sso_error=signin"


def test_first_sign_in_links_by_email_and_drops_the_password(https, company, monkeypatch):
    _add_user(company["team"], "jr", "JR@frazil.com")
    _fake_ms(monkeypatch, preferred_username="jr@freezingpointllc.com", email="jr@frazil.com")
    cb, _ = _sign_in(https)
    assert cb.headers["location"] == "/?sso=done"
    u = next(u for u in _users(company["team"]) if u["username"] == "jr")
    assert u["entraOid"] == OID and u["password"] == "" and u["ssoAt"] > 0
    r = https.post("/api/auth/sso/finish", json={})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["team"] == company["team"] and d["username"] == "jr" and d["via"] == "microsoft"
    assert server.decode_token(d["token"])["username"] == "jr"
    # the hand-off cookie is single use
    assert https.post("/api/auth/sso/finish", json={}).status_code == 401


def test_no_account_creates_nobody(https, company, monkeypatch):
    before = len(_users(company["team"]))
    _fake_ms(monkeypatch, preferred_username="new.person@freezingpointllc.com")
    cb, _ = _sign_in(https, email="new.person@freezingpointllc.com")
    assert cb.headers["location"] == "/?sso_error=noaccount"
    assert len(_users(company["team"])) == before


def test_revoked_and_differently_linked_users_never_match(https, company, monkeypatch):
    _add_user(company["team"], "gone", "jr@freezingpointllc.com", revokedAt="2026-01-01")
    _add_user(company["other"], "someone", "jr@freezingpointllc.com", entraOid="a-different-person")
    _fake_ms(monkeypatch)
    cb, _ = _sign_in(https)
    assert cb.headers["location"] == "/?sso_error=noaccount"


def test_several_orgs_ask_which_one(https, company, monkeypatch):
    _add_user(company["team"], "jr", "jr@freezingpointllc.com")
    _add_user(company["other"], "jrfrisby", "jr@frazil.com")
    _fake_ms(monkeypatch, email="jr@frazil.com")
    _sign_in(https)
    assert https.post("/api/auth/sso/finish", json={}).json() == {"pick": [company["team"], company["other"]]}
    assert https.post("/api/auth/sso/finish", json={"team": company["third"]}).status_code == 400
    d = https.post("/api/auth/sso/finish", json={"team": company["other"]}).json()
    assert d["team"] == company["other"] and d["username"] == "jrfrisby"


def test_never_reaches_another_companys_orgs(https, company, monkeypatch):
    _add_user(company["third"], "jr", "jr@freezingpointllc.com")   # same email, in Acme's Org
    _fake_ms(monkeypatch)
    cb, _ = _sign_in(https)
    assert cb.headers["location"] == "/?sso_error=noaccount"
    assert not any(u.get("entraOid") for u in _users(company["third"]))


def test_a_linked_user_matches_by_oid_after_an_email_change(https, company, monkeypatch):
    _add_user(company["team"], "jr", "old@frazil.com", entraOid=OID)
    _fake_ms(monkeypatch, preferred_username="renamed@freezingpointllc.com")
    _sign_in(https, email="renamed@freezingpointllc.com")
    assert https.post("/api/auth/sso/finish", json={}).json()["username"] == "jr"


# ── Passwords, the built-in admin, admin saves, the daily re-check ─────────────

def test_linked_users_cannot_use_a_password_but_the_builtin_admin_can(client, company):
    _add_user(company["team"], "jr", "jr@frazil.com", entraOid=OID)
    us = _users(company["team"])
    for u in us:
        if u.get("builtin"):
            u["entraOid"] = "admin-oid"
    _set_users(company["team"], us)
    server._rate.clear()
    r = client.post("/api/login", json={"team": company["team"], "username": "jr", "password": "pw123456"})
    assert r.status_code == 409 and "Microsoft" in r.json()["detail"]
    r = client.post("/api/login", json={"team": company["team"], "username": "admin", "password": "frazil123"})
    assert r.status_code == 200


def test_outside_people_keep_passwords(client, company):
    _add_user(company["team"], "contractor", "bob@gmail.com", role="contributor")
    server._rate.clear()
    r = client.post("/api/login", json={"team": company["team"], "username": "contractor", "password": "pw123456"})
    assert r.status_code == 200


def test_an_admin_save_neither_wipes_nor_forges_the_link(client, company, admin_headers):
    _add_user(company["team"], "jr", "jr@frazil.com", entraOid=OID, ssoAt=123)
    _add_user(company["team"], "eve", "eve@frazil.com")
    us = _users(company["team"])
    for u in us:
        u.pop("password", None)
        if u["username"] == "jr":
            u.pop("entraOid"); u.pop("ssoAt")
        if u["username"] == "eve":
            u["entraOid"] = OID
    r = client.put("/api/config/users", json=us, headers=admin_headers)
    assert r.status_code == 200, r.text
    after = {u["username"]: u for u in _users(company["team"])}
    assert after["jr"]["entraOid"] == OID and after["jr"]["ssoAt"] == 123
    assert "entraOid" not in after["eve"]


def test_forgot_password_ignores_linked_users(client, company, monkeypatch):
    _add_user(company["team"], "jr", "jr@frazil.com", entraOid=OID)
    sent = []
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    monkeypatch.setattr(server, "send_email", lambda *a, **k: sent.append(a), raising=False)
    server._rate.clear()
    r = client.post("/api/forgot-password", json={"team": company["team"], "email": "jr@frazil.com"})
    assert r.status_code == 200 and sent == []


def test_a_day_old_microsoft_session_goes_back_through_microsoft(client, company):
    _add_user(company["team"], "jr", "jr@frazil.com", entraOid=OID, ssoAt=int(time.time()) - 100)
    h = {"Authorization": "Bearer " + server.create_token(company["team"], "jr", "editor"), "X-Team": company["team"]}
    assert client.get("/api/all", headers=h).status_code == 200
    us = _users(company["team"])
    next(u for u in us if u["username"] == "jr")["ssoAt"] = int(time.time()) - 2 * 86400
    _set_users(company["team"], us)
    r = client.get("/api/all", headers=h)
    assert r.status_code == 401 and r.json()["detail"] == "SSO_RECHECK"


def test_no_recheck_once_the_org_leaves_the_company(client, company, monkeypatch, tmp_path):
    _add_user(company["team"], "jr", "jr@frazil.com", entraOid=OID, ssoAt=1)
    monkeypatch.setattr(server, "COMPANIES_FILE", str(tmp_path / "none.json"))
    server._COMPANIES_CACHE.update(path=None, mtime=None, data=[])
    h = {"Authorization": "Bearer " + server.create_token(company["team"], "jr", "editor"), "X-Team": company["team"]}
    assert client.get("/api/all", headers=h).status_code == 200


def test_company_status_is_operator_only_and_hides_the_key(client, company, admin_headers, monkeypatch):
    assert client.get("/api/sys/companies", headers=admin_headers).status_code == 403
    monkeypatch.setattr(server, "SYS_STATUS_USERS", frozenset({"admin"}))
    d = client.get("/api/sys/companies", headers=admin_headers).json()
    co = d["companies"][0]
    assert co["id"] == "frazil" and co["keyPresent"] is True and "certKey" not in co
    assert d["cryptography"] is True


def test_the_page_gets_yes_no_flags_never_the_link_or_hash(client, company, admin_headers):
    _add_user(company["team"], "jr", "jr@frazil.com", entraOid=OID)
    _add_user(company["team"], "newbie", "n@frazil.com")
    us = _users(company["team"])
    next(u for u in us if u["username"] == "newbie")["password"] = ""
    _set_users(company["team"], us)
    got = {u["username"]: u for u in client.get("/api/all", headers=admin_headers).json()["users"]}
    assert got["jr"]["microsoft"] is True and got["jr"]["pending"] is False and "entraOid" not in got["jr"]
    assert got["newbie"]["pending"] is True and got["newbie"]["microsoft"] is False
    assert got["admin"]["pending"] is False and "password" not in got["admin"]
    # the flags are never stored by an admin save
    r = client.put("/api/config/users", json=list(got.values()), headers=admin_headers)
    assert r.status_code == 200, r.text
    assert not any("microsoft" in u or "pending" in u for u in _users(company["team"]))


# ── Add User "Signs in with Microsoft" ─────────────────────────────────────────

def test_microsoft_welcome_marks_invited_and_emails(client, company, admin_headers, monkeypatch):
    _add_user(company["team"], "newstaff", "new.staff@freezingpointllc.com")
    us = _users(company["team"]); next(u for u in us if u["username"] == "newstaff").pop("password"); _set_users(company["team"], us)
    sent = []
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    monkeypatch.setattr(server, "send_email", lambda *a, **k: sent.append(a))
    r = client.post("/api/users/newstaff/sso-welcome", headers=admin_headers)
    assert r.status_code == 200 and r.json() == {"ok": True, "sent": True}
    assert sent and sent[0][0] == "new.staff@freezingpointllc.com" and "Sign in with Microsoft" in sent[0][2]
    got = next(u for u in client.get("/api/all", headers=admin_headers).json()["users"] if u["username"] == "newstaff")
    assert got["microsoft"] is True and got["msInvited"] is True and got["pending"] is False
    # an admin save keeps the invite mark (server-owned) and never stores the flags
    allu = client.get("/api/all", headers=admin_headers).json()["users"]
    assert client.put("/api/config/users", json=allu, headers=admin_headers).status_code == 200
    stored = next(u for u in _users(company["team"]) if u["username"] == "newstaff")
    assert stored.get("ssoInvitedAt") and "msInvited" not in stored


def test_microsoft_welcome_without_mail_still_works(client, company, admin_headers, monkeypatch):
    _add_user(company["team"], "newstaff", "new.staff@frazil.com")
    monkeypatch.setattr(server, "mail_configured", lambda: False)
    assert client.post("/api/users/newstaff/sso-welcome", headers=admin_headers).json() == {"ok": True, "sent": False}


def test_microsoft_welcome_needs_a_company_email_and_an_admin(client, company, admin_headers, editor_headers):
    _add_user(company["team"], "outsider", "bob@gmail.com")
    r = client.post("/api/users/outsider/sso-welcome", headers=admin_headers)
    assert r.status_code == 400 and "@freezingpointllc.com" in r.json()["detail"]
    assert client.post("/api/users/outsider/sso-welcome", headers=editor_headers).status_code == 403
    assert client.post("/api/users/nobody/sso-welcome", headers=admin_headers).status_code == 404


def test_microsoft_welcome_needs_microsoft_sign_in_on_the_org(client, team, admin_headers, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "COMPANIES_FILE", str(tmp_path / "none.json"))
    server._COMPANIES_CACHE.update(path=None, mtime=None, data=[])
    _add_user(team, "newstaff", "new.staff@frazil.com")
    assert client.post("/api/users/newstaff/sso-welcome", headers=admin_headers).status_code == 400
    assert client.get("/api/all", headers=admin_headers).json()["ssoDomains"] == []


def test_the_page_learns_the_company_domains(client, company, admin_headers):
    assert client.get("/api/all", headers=admin_headers).json()["ssoDomains"] == ["frazil.com", "freezingpointllc.com"]


def test_an_invited_user_links_on_first_microsoft_sign_in(https, company, monkeypatch):
    _add_user(company["team"], "newstaff", "new.staff@freezingpointllc.com", ssoInvitedAt="2026-11-19T00:00:00Z")
    _fake_ms(monkeypatch, preferred_username="new.staff@freezingpointllc.com", oid="oid-new")
    _sign_in(https, email="new.staff@freezingpointllc.com")
    d = https.post("/api/auth/sso/finish", json={}).json()
    assert d["username"] == "newstaff"
    assert next(u for u in _users(company["team"]) if u["username"] == "newstaff")["entraOid"] == "oid-new"


# ── Unlink: Microsoft -> password, and back (SSO-UNLINK-1) ──────────────────────

def _drop_pw(team, username):
    us = _users(team); next(u for u in us if u["username"] == username)["password"] = ""; _set_users(team, us)


def test_unlink_clears_the_link_signs_them_out_and_stops_relinking(https, client, company, admin_headers, monkeypatch):
    _add_user(company["team"], "jr", "jr@freezingpointllc.com", entraOid=OID, ssoAt=int(time.time()))
    _drop_pw(company["team"], "jr")
    h = {"Authorization": "Bearer " + server.create_token(company["team"], "jr", "editor"), "X-Team": company["team"]}
    assert client.get("/api/all", headers=h).status_code == 200
    r = client.post("/api/users/jr/sso-unlink", headers=admin_headers)
    assert r.status_code == 200 and r.json()["hasPassword"] is False
    u = next(u for u in _users(company["team"]) if u["username"] == "jr")
    assert "entraOid" not in u and "ssoAt" not in u and u["ssoOff"] is True and u["tokenGen"] == 1
    assert client.get("/api/all", headers=h).status_code == 401           # signed out everywhere
    got = next(x for x in client.get("/api/all", headers=admin_headers).json()["users"] if x["username"] == "jr")
    assert got["microsoft"] is False and got["pending"] is True and got["ssoOff"] is True
    # Microsoft does not link them again
    _fake_ms(monkeypatch)
    cb, _ = _sign_in(https)
    assert cb.headers["location"] == "/?sso_error=noaccount"
    assert "entraOid" not in next(u for u in _users(company["team"]) if u["username"] == "jr")


def test_an_admin_save_keeps_ssooff_and_the_signout_counter(client, company, admin_headers):
    _add_user(company["team"], "jr", "jr@freezingpointllc.com", entraOid=OID)
    assert client.post("/api/users/jr/sso-unlink", headers=admin_headers).status_code == 200
    allu = client.get("/api/all", headers=admin_headers).json()["users"]
    for x in allu:
        x["ssoOff"] = False; x["tokenGen"] = 0     # a forged / stale client copy changes nothing
    assert client.put("/api/config/users", json=allu, headers=admin_headers).status_code == 200
    u = next(u for u in _users(company["team"]) if u["username"] == "jr")
    assert u["ssoOff"] is True and u["tokenGen"] == 1
    assert not any(x.get("ssoOff") for x in _users(company["team"]) if x["username"] != "jr")


def test_unlink_refusals(client, company, admin_headers, editor_headers):
    _add_user(company["team"], "pwuser", "pw@freezingpointllc.com")
    assert client.post("/api/users/pwuser/sso-unlink", headers=admin_headers).status_code == 400   # not on Microsoft
    assert client.post("/api/users/nobody/sso-unlink", headers=admin_headers).status_code == 404
    _add_user(company["team"], "jr", "jr@freezingpointllc.com", entraOid=OID)
    assert client.post("/api/users/jr/sso-unlink", headers=editor_headers).status_code == 403
    us = _users(company["team"]); next(u for u in us if u["username"] == "admin")["entraOid"] = "a"; _set_users(company["team"], us)
    assert client.post("/api/users/admin/sso-unlink", headers=admin_headers).status_code == 400   # not yourself


def test_unlinking_an_invite_and_the_builtin_admin_keeps_passwords(client, company, admin_headers):
    _add_user(company["team"], "inv", "inv@freezingpointllc.com", ssoInvitedAt="2026-11-19T00:00:00Z")
    r = client.post("/api/users/inv/sso-unlink", headers=admin_headers)
    assert r.status_code == 200 and r.json()["hasPassword"] is True
    assert "ssoInvitedAt" not in next(u for u in _users(company["team"]) if u["username"] == "inv")
    server._rate.clear()
    assert client.post("/api/login", json={"team": company["team"], "username": "inv", "password": "pw123456"}).status_code == 200


def test_use_microsoft_turns_it_back_on(https, client, company, admin_headers, monkeypatch):
    _add_user(company["team"], "jr", "jr@freezingpointllc.com", entraOid=OID)
    client.post("/api/users/jr/sso-unlink", headers=admin_headers)
    sent = []
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    monkeypatch.setattr(server, "send_email", lambda *a, **k: sent.append(a))
    assert client.post("/api/users/jr/sso-welcome", headers=admin_headers).json() == {"ok": True, "sent": True}
    assert sent[0][1] == "Sign in to Frazil Flow with Microsoft"
    assert "ssoOff" not in next(u for u in _users(company["team"]) if u["username"] == "jr")
    _fake_ms(monkeypatch)
    _sign_in(https)
    assert https.post("/api/auth/sso/finish", json={}).json()["username"] == "jr"
