"""AssetHub outbound client (FLOW-0), offline.

AssetHub is deployed dark, so every test here runs against a mocked transport. The bounds
exercised below (bounded retries, no-retry on terminal statuses, jittered backoff, the
Contributor gate, no-secret-logging) are the mitigation for the temporarily waived request
limiting, so they are asserted, not assumed. Contract: docs/openapi-v1-foundation-contract.md
v1.1. Live verification is the Part 9 enablement gate, not here.
"""
import json
import logging
import re

import pytest

import server

# A recognizable non-real key so we can assert it never leaks. Not a valid credential; the
# client sends it verbatim and never parses it (parsing is AssetHub's job).
KEY = "ahk_aaaaaaaaaaaaaaaa_" + "b" * 52
BASE = "https://assethub.example"


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


def _transport(*responses):
    """Fake transport: (method,url,headers)->(status,body_bytes,headers). Consumes the response
    sequence, then repeats the last. Records every call on t.calls."""
    seq = list(responses)
    state = {"i": 0}
    calls = []
    def t(method, url, headers):
        calls.append({"method": method, "url": url, "headers": dict(headers)})
        status, body, hdrs = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        elif body is None:
            body = b""
        return status, body, (hdrs or {})
    t.calls = calls
    return t


def _client(team="t", role="admin", transport=None, key=KEY, base=BASE, sleep=None):
    return server.AssetHubClient(team, role, base_url=base, api_key=key,
                                 transport=transport, sleep=(sleep or (lambda d: None)))


def _ok_body(corr="c1", extra=None):
    b = {"data": {"api_version": "v1"}, "correlation_id": corr}
    if extra:
        b.update(extra)
    return b


def _err_body(code, corr="c1", message="msg"):
    return {"error": {"code": code, "message": message}, "correlation_id": corr, "retryable": False}


IT_MAPPING = {"provider": "flow", "providerEnvironment": "production",
              "externalTenantReference": "default", "externalTeamReference": "it",
              "assethubTeam": "IT", "displayName": "Flow IT to AssetHub IT"}


def _valid_whoami(team_slug):
    return {"api_version": "v1",
            "connection": {"id": "conn-1", "display_name": "Flow IT to AssetHub IT",
                           "provider": "flow", "provider_environment": "production",
                           "external_tenant_reference": "default",
                           "external_team_reference": team_slug, "status": "active"},
            "team": {"name": "IT"},
            "credential": {"id": "cred-1", "version": 1, "expires_at": None},
            "scopes": ["identity.read", "assets.read"]}


# ── The configured flag + no-leak (Part 2.3 / Part 8) ────────────────────────────

def test_missing_credential_is_normal_and_flag_false(client, team, admin_headers, monkeypatch):
    monkeypatch.delenv("ASSETHUB_API_KEY_" + team.upper(), raising=False)
    _set_cfg(team, "assethubConnection", IT_MAPPING)   # mapping present, credential absent
    r = client.get("/api/all", headers=admin_headers).json()
    assert r["assethubConfigured"] is False


def test_credential_plus_mapping_makes_flag_true(client, team, admin_headers, monkeypatch):
    monkeypatch.setenv("ASSETHUB_API_KEY_" + team.upper(), KEY)
    _set_cfg(team, "assethubConnection", IT_MAPPING)
    r = client.get("/api/all", headers=admin_headers).json()
    assert r["assethubConfigured"] is True


def test_assethub_base_url_exposed_in_api_all(client, team, admin_headers, monkeypatch):
    # FLOW-1 item B: the non-secret host is exposed for the asset-tag deep link; the key never is.
    monkeypatch.setattr(server, "ASSETHUB_BASE_URL", "https://ah.example")
    monkeypatch.setenv("ASSETHUB_API_KEY_" + team.upper(), KEY)
    raw = client.get("/api/all", headers=admin_headers).text
    payload = __import__("json").loads(raw)
    assert payload["assethubBaseUrl"] == "https://ah.example"
    assert KEY not in raw and "ahk_" not in raw     # host yes, credential never


def test_token_never_appears_in_api_all(client, team, admin_headers, monkeypatch):
    monkeypatch.setenv("ASSETHUB_API_KEY_" + team.upper(), KEY)
    _set_cfg(team, "assethubConnection", IT_MAPPING)
    raw = client.get("/api/all", headers=admin_headers).text
    assert KEY not in raw
    assert "ahk_" not in raw               # no fragment of the key grammar leaks either
    payload = json.loads(raw)
    assert payload["assethubConfigured"] is True   # a plain bool, carrying no secret


# ── Request construction (Part 3.1 / Part 8) ─────────────────────────────────────

def test_header_and_path_construction():
    t = _transport((200, _ok_body(), {"X-Correlation-ID": "c1"}))
    _client(transport=t).whoami()
    h = t.calls[0]["headers"]
    assert h["Authorization"] == "Bearer " + KEY
    assert set(h) == {"Authorization", "Accept", "X-Correlation-ID"}   # nothing else, no cookie/custom
    cid = h["X-Correlation-ID"]
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", cid)
    assert t.calls[0]["url"] == BASE + "/api/v1/whoami"                # exact, no trailing slash


def test_paths_have_no_trailing_slash_and_sorted_params():
    t = _transport((200, {"data": [], "correlation_id": "c1"}, {}))
    _client(transport=t).get("/api/v1/assets", params={"per_page": 50, "page": 1})
    url = t.calls[0]["url"]
    assert url == BASE + "/api/v1/assets?page=1&per_page=50"
    assert not url.endswith("/")


# ── Retry matrix (Part 3.2) - the waived-control mitigation ───────────────────────

def test_500_retries_within_bound_then_fails():
    t = _transport((500, _err_body("internal_error"), {}))   # always 500
    with pytest.raises(server.AssetHubError) as ei:
        _client(transport=t).whoami()
    assert ei.value.status == 500
    assert len(t.calls) == server._ASSETHUB_MAX_RETRIES + 1    # bounded: initial + retries


def test_500_then_success_recovers():
    t = _transport((500, _err_body("internal_error"), {}), (200, _ok_body(), {}))
    res = _client(transport=t).whoami()
    assert res["data"]["api_version"] == "v1"
    assert len(t.calls) == 2


def test_503_backs_off_with_delays():
    delays = []
    t = _transport((503, _err_body("dependency_unavailable"), {}))
    with pytest.raises(server.AssetHubError):
        _client(transport=t, sleep=delays.append).whoami()
    assert len(t.calls) == server._ASSETHUB_MAX_RETRIES + 1
    assert delays and all(d > 0 for d in delays)                # backed off, did not hammer


@pytest.mark.parametrize("status", [401, 403, 404, 405, 422])
def test_terminal_statuses_retry_zero_times(status):
    t = _transport((status, _err_body("invalid_credential"), {}))
    with pytest.raises(server.AssetHubError):
        _client(transport=t).whoami()
    assert len(t.calls) == 1                                    # never retried unchanged


def test_backoff_is_jittered_not_fixed():
    lo = server._assethub_backoff_delay(2, rand=lambda: 0.0)
    hi = server._assethub_backoff_delay(2, rand=lambda: 1.0)
    assert hi > lo                                              # jitter varies the delay
    a1 = server._assethub_backoff_delay(1, rand=lambda: 0.0)
    a2 = server._assethub_backoff_delay(2, rand=lambda: 0.0)
    assert a2 > a1                                              # exponential growth


def test_timeout_is_surfaced_not_hung():
    def boom(method, url, headers):
        raise TimeoutError("timed out")
    calls = {"n": 0}
    def counting(method, url, headers):
        calls["n"] += 1
        raise TimeoutError("timed out")
    with pytest.raises(server.AssetHubError) as ei:
        _client(transport=counting).whoami()
    assert ei.value.code == "unreachable"
    assert ei.value.retryable is True
    assert calls["n"] == server._ASSETHUB_MAX_RETRIES + 1


# ── Response handling (Part 3.3) ─────────────────────────────────────────────────

def test_error_envelope_yields_code():
    t = _transport((403, _err_body("missing_scope", corr="cx"), {}))
    with pytest.raises(server.AssetHubError) as ei:
        _client(transport=t).whoami()
    assert ei.value.code == "missing_scope"
    assert ei.value.status == 403
    assert ei.value.correlation_id == "cx"


def test_unrecognized_error_code_degrades_gracefully():
    t = _transport((403, _err_body("some_future_code"), {}))
    with pytest.raises(server.AssetHubError) as ei:
        _client(transport=t).whoami()
    assert ei.value.code == "some_future_code"                 # kept verbatim, no crash


def test_success_envelope_siblings_and_unknown_field_tolerated():
    body = {"data": {"x": 1}, "correlation_id": "abc", "totally_new_top_level": True}
    t = _transport((200, body, {}))
    res = _client(transport=t).whoami()
    assert res["data"] == {"x": 1}
    assert res["correlation_id"] == "abc"                      # sibling, not nested


# ── whoami identity validation (Part 4) ──────────────────────────────────────────

def test_whoami_validation_passes_when_all_match():
    ok, mism, info = server._assethub_validate_whoami(_valid_whoami("it"), "it", IT_MAPPING)
    assert ok and not mism
    assert info["connectionId"] == "conn-1"


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["connection"].update(provider="jira"), "connection.provider"),
    (lambda d: d["connection"].update(provider_environment="staging"), "connection.provider_environment"),
    (lambda d: d["connection"].update(external_tenant_reference="other"), "connection.external_tenant_reference"),
    (lambda d: d["connection"].update(external_team_reference="ops"), "connection.external_team_reference"),
    (lambda d: d["team"].update(name="LOGISTICS"), "team.name"),
    (lambda d: d.update(scopes=["assets.read"]), "identity.read"),
    (lambda d: d.update(scopes=["identity.read"]), "assets.read"),
])
def test_each_whoami_check_fails_independently(mutate, needle):
    d = _valid_whoami("it")
    mutate(d)
    ok, mism, info = server._assethub_validate_whoami(d, "it", IT_MAPPING)
    assert not ok
    assert any(needle in m for m in mism)


# ── The Contributor gate (Part 6) ────────────────────────────────────────────────

def test_contributor_call_is_refused_before_any_request():
    t = _transport((200, _ok_body(), {}))
    with pytest.raises(server.AssetHubError) as ei:
        _client(role="contributor", transport=t).whoami()
    assert ei.value.code == "contributor_forbidden"
    assert t.calls == []                                       # refused before touching the transport


# ── Credential handling (Part 3.4) ───────────────────────────────────────────────

def test_key_never_appears_in_logs_or_errors(caplog):
    with caplog.at_level(logging.INFO):
        # a success and an error, both should log correlation but never the key
        _client(transport=_transport((200, _ok_body(), {}))).whoami()
        try:
            _client(transport=_transport((401, _err_body("invalid_credential"), {}))).whoami()
        except server.AssetHubError as e:
            assert KEY not in str(e)
            assert KEY not in e.message
    assert KEY not in caplog.text
    assert "Bearer " + KEY not in caplog.text


def test_rotation_changes_header_with_no_code_change():
    old, new = "ahk_oldoldoldoldold_" + "c" * 52, "ahk_newnewnewnewnew_" + "d" * 52
    t1 = _transport((200, _ok_body(), {})); _client(key=old, transport=t1).whoami()
    t2 = _transport((200, _ok_body(), {})); _client(key=new, transport=t2).whoami()
    assert t1.calls[0]["headers"]["Authorization"] == "Bearer " + old
    assert t2.calls[0]["headers"]["Authorization"] == "Bearer " + new


def test_not_configured_when_no_key():
    with pytest.raises(server.AssetHubError) as ei:
        _client(key="", transport=_transport((200, _ok_body(), {}))).whoami()
    assert ei.value.code == "not_configured"


# ── Health endpoint (Part 5) ─────────────────────────────────────────────────────

def test_health_reports_not_configured(client, team, admin_headers, monkeypatch):
    monkeypatch.delenv("ASSETHUB_API_KEY_" + team.upper(), raising=False)
    _set_cfg(team, "assethubConnection", IT_MAPPING)
    r = client.get("/api/assethub/health", headers=admin_headers).json()
    assert r["status"] == "not_configured"


def test_health_requires_admin(client, team, editor_headers, monkeypatch):
    monkeypatch.setenv("ASSETHUB_API_KEY_" + team.upper(), KEY)
    _set_cfg(team, "assethubConnection", IT_MAPPING)
    r = client.get("/api/assethub/health", headers=editor_headers)
    assert r.status_code in (401, 403)
