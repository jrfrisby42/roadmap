"""PRECREATE-LINKS-1: attach external reference links while CREATING an item.

Links arrive at create on a `pendingLinks` body key ({label, url} each). `extLinks` stays on the
create strip list (server-owned), so nothing client-supplied lands in the blob directly - `pendingLinks`
is consumed, validated through the SHARED non-raising `_normalize_link_fields` (the same core the
item-page endpoints use via `_norm_item_link`), and the survivors seed `extLinks` with the SAME record
shape `add_item_link` writes. Drop-and-report per link: a bad scheme or an over-cap link is dropped and
surfaced (linksDropped), never fatal. Capped at `_EXT_LINK_CAP` (20), matching the item-page path.

Guard kinds:
- FAIL-ON-REVERT: the scheme rejection (test_unsafe_scheme_*, the load-bearing case - revert by pointing
  create at a non-validating path and a javascript: URL lands in extLinks); the extLinks strip
  (test_forged_extlinks_* - revert by removing body.pop("extLinks") and a forged array persists);
  pendingLinks never persisted (test_pendinglinks_not_persisted).
- INVARIANT: the item-page link endpoints are unchanged (test_ext_links.py still passes - the refactor
  is behavior-preserving); Stage A's attachment control + the "Linked assets ..." line are untouched.
"""
import json
import re
import pathlib

import server

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
HTML = ROOT / "roadmap.html"

_MISSING = object()


def _py():
    return SERVER.read_text(encoding="utf-8", errors="replace")


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


def _item(client, admin_headers, name):
    return next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"]
               if p["name"] == name)


def _stored(team, pid):
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    return json.loads(row["data"]) if row else None


def _create(client, headers, name, links=_MISSING, **fields):
    body = {"name": name, "status": "New", **fields}
    if links is not _MISSING:
        body["pendingLinks"] = links
    return client.post("/api/projects", json=body, headers=headers)


# ── case 1: creating an item with two links persists both (verified by fresh fetch) ────────────────
def test_create_with_two_links_persists_both(client, team, admin_headers):
    r = _create(client, admin_headers, "TwoLinks", links=[
        {"label": "Runbook", "url": "https://example.com/run"},
        {"label": "Spec", "url": "https://example.com/spec?q=1"},
    ])
    assert r.status_code in (200, 201), r.text
    assert r.json().get("linksDropped") in (None, [])
    it = _item(client, admin_headers, "TwoLinks")            # fresh fetch, not the POST return
    links = _stored(team, it["id"]).get("extLinks") or []
    assert [l["url"] for l in links] == ["https://example.com/run", "https://example.com/spec?q=1"]
    assert [l["label"] for l in links] == ["Runbook", "Spec"]
    # record shape matches add_item_link: server-generated id/addedBy/addedAt
    assert all(l["id"] and l["addedBy"] == "admin" and l["addedAt"] for l in links)


def test_editor_can_create_with_links(client, team, admin_headers, editor_headers):
    r = _create(client, editor_headers, "EdLinks", links=[{"label": "x", "url": "https://x.com"}])
    assert r.status_code in (200, 201), r.text
    it = _item(client, admin_headers, "EdLinks")
    assert (_stored(team, it["id"]).get("extLinks") or [])[0]["url"] == "https://x.com"
    assert (_stored(team, it["id"])["extLinks"][0]["addedBy"]) == "editor1"


def test_blank_label_falls_back_to_hostname_in_create(client, team, admin_headers):
    """The shared validator's hostname fallback applies at create too (one normalizer, no drift)."""
    r = _create(client, admin_headers, "BlankLabel",
                links=[{"label": "", "url": "https://portal.sharepoint.com/a/b?d=1"}])
    assert r.status_code in (200, 201), r.text
    it = _item(client, admin_headers, "BlankLabel")
    assert _stored(team, it["id"])["extLinks"][0]["label"] == "portal.sharepoint.com"


# ── case 3 (LOAD-BEARING GUARD): an unsafe scheme is rejected server-side, item still created ───────
def test_unsafe_scheme_dropped_server_side_item_created(client, team, admin_headers):
    """Post an unsafe scheme DIRECTLY to the API (bypassing the client). It must be dropped + reported,
    the item still created, and nothing unsafe stored. Revert demo: route create's link promotion at a
    non-validating path (skip _normalize_link_fields) and the javascript: URL lands in extLinks."""
    r = _create(client, admin_headers, "Unsafe", links=[
        {"label": "evil", "url": "javascript:alert(1)"},
        {"label": "ok", "url": "https://safe.com/ok"},
    ])
    assert r.status_code in (200, 201), r.text
    assert r.json().get("linksDropped") == ["javascript:alert(1)"]   # surfaced to the user
    it = _item(client, admin_headers, "Unsafe")
    links = _stored(team, it["id"]).get("extLinks") or []
    assert [l["url"] for l in links] == ["https://safe.com/ok"]      # only the safe one stored
    assert all(l["url"].startswith("https://") for l in links)


def test_various_bad_schemes_all_dropped(client, team, admin_headers):
    bad = ["ftp://e.com/x", "//e.com/x", "e.com", "mailto:a@b.com", "javascript:alert(1)", "data:text/html,x"]
    r = _create(client, admin_headers, "BadSchemes", links=[{"label": "b", "url": u} for u in bad])
    assert r.status_code in (200, 201), r.text
    it = _item(client, admin_headers, "BadSchemes")
    assert (_stored(team, it["id"]).get("extLinks") or []) == []     # none stored
    assert set(r.json().get("linksDropped") or []) == set(bad)       # each reported


# ── case 4: an invalid link is dropped, the item is still created, drop reported per link ───────────
def test_invalid_dropped_valid_kept_item_created(client, team, admin_headers):
    r = _create(client, admin_headers, "Mixed", links=[
        {"label": "good1", "url": "https://a.com"},
        {"label": "bad", "url": "notaurl"},
        {"label": "good2", "url": "https://b.com"},
    ])
    assert r.status_code in (200, 201), r.text
    it = _item(client, admin_headers, "Mixed")
    links = _stored(team, it["id"]).get("extLinks") or []
    assert [l["url"] for l in links] == ["https://a.com", "https://b.com"]
    assert r.json().get("linksDropped") == ["notaurl"]


# ── case 5: over-cap links handled (drop + report beyond _EXT_LINK_CAP) ─────────────────────────────
def test_over_cap_links_dropped_and_reported(client, team, admin_headers):
    cap = server._EXT_LINK_CAP
    over = cap + 2
    r = _create(client, admin_headers, "OverCap",
                links=[{"label": f"L{i}", "url": f"https://e.com/{i}"} for i in range(over)])
    assert r.status_code in (200, 201), r.text
    it = _item(client, admin_headers, "OverCap")
    links = _stored(team, it["id"]).get("extLinks") or []
    assert len(links) == cap                                          # capped
    dropped = r.json().get("linksDropped") or []
    assert len(dropped) == over - cap                                 # the excess reported


# ── case 6 (GUARD): extLinks sent directly in the create body is still stripped (forge hole closed) ─
def test_forged_extlinks_array_stripped(client, team, admin_headers):
    """Revert demo: remove `body.pop("extLinks", None)` from create_project and this forged array
    persists unvalidated (a javascript: URL straight into the blob)."""
    r = client.post("/api/projects", json={
        "name": "Forged", "status": "New",
        "extLinks": [{"id": "forged", "label": "evil", "url": "javascript:alert(1)"}],
    }, headers=admin_headers)
    assert r.status_code in (200, 201), r.text
    it = _item(client, admin_headers, "Forged")
    assert (_stored(team, it["id"]).get("extLinks") or []) == []


def test_forged_extlinks_plus_valid_pending(client, team, admin_headers):
    """A forged extLinks array is ignored; only validated pendingLinks seed the blob."""
    r = client.post("/api/projects", json={
        "name": "ForgedPlus", "status": "New",
        "extLinks": [{"id": "forged", "label": "evil", "url": "javascript:alert(1)"}],
        "pendingLinks": [{"label": "real", "url": "https://real.com"}],
    }, headers=admin_headers)
    assert r.status_code in (200, 201), r.text
    it = _item(client, admin_headers, "ForgedPlus")
    links = _stored(team, it["id"]).get("extLinks") or []
    assert [l["url"] for l in links] == ["https://real.com"]          # only the validated pending link


def test_pendinglinks_not_persisted(client, team, admin_headers):
    """pendingLinks is consumed, never written to the blob as a field."""
    r = _create(client, admin_headers, "NoLeak", links=[{"label": "x", "url": "https://x.com"}])
    assert r.status_code in (200, 201), r.text
    it = _item(client, admin_headers, "NoLeak")
    assert "pendingLinks" not in _stored(team, it["id"])


# ── invariant: no links -> no extLinks and no linksDropped (orgs that never use it behave as before) ─
def test_create_without_links_unchanged(client, team, admin_headers):
    r = _create(client, admin_headers, "Plain")
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert "linksDropped" not in body and "extLinks" not in body
    assert "extLinks" not in _stored(team, _item(client, admin_headers, "Plain")["id"])


# ── type confusion: a malformed pendingLinks never 500s; the item is still created ─────────────────
def test_pending_links_type_confusion_never_raises(client, team, admin_headers):
    cases = {
        "absent": _MISSING,
        "string_not_list": "nope",
        "dict_not_list": {"url": "https://x.com"},
        "number_not_list": 5,
        "list_with_None": [None],
        "list_with_string": ["https://x.com"],
        "dict_missing_url": [{"label": "x"}],
        "url_not_string": [{"label": "x", "url": 123}],
        "label_not_string": [{"label": 123, "url": "https://x.com"}],
        "over_cap": [{"label": f"L{i}", "url": f"https://e.com/{i}"} for i in range(server._EXT_LINK_CAP + 5)],
    }
    for label, links in cases.items():
        r = _create(client, admin_headers, f"tc-{label}", links=links)
        assert r.status_code in (200, 201), f"{label} must not 500: {r.status_code} {r.text[:200]}"
        it = _item(client, admin_headers, f"tc-{label}")
        assert len(_stored(team, it["id"]).get("extLinks") or []) <= server._EXT_LINK_CAP, f"{label}: never over cap"


# ── source-shape guards (fail on revert) ───────────────────────────────────────────────────────────
def test_source_shape_server():
    src = _py()
    # Option A: one shared non-raising validator; the raising endpoint wrapper delegates to it.
    assert "def _normalize_link_fields(" in src, "the shared non-raising link validator must exist"
    nm = re.search(r"def _norm_item_link\(.*?\n\n", src, re.DOTALL).group(0)
    assert "_normalize_link_fields(" in nm, "_norm_item_link must delegate to the shared core (no second validator)"
    assert 'raise HTTPException(422' in nm, "the endpoint wrapper still raises 422 on a bad scheme"
    # create consumes pendingLinks, keeps extLinks stripped, validates through the shared core, caps.
    cp = re.search(r"def create_project\(.*?\n@app\.put", src, re.DOTALL).group(0)
    assert 'body.pop("extLinks", None)' in cp, "create must keep stripping a client extLinks array"
    assert 'body.pop("pendingLinks", None)' in cp, "create must consume pendingLinks (not persist it)"
    assert "_normalize_link_fields(l)" in cp, "create must validate each pending link via the shared core"
    assert "_EXT_LINK_CAP" in cp, "create must cap links at the item-page cap"
    assert 'json_set(data, \'$.extLinks\'' in cp, "survivors seed extLinks via json_set"
    assert '"linksDropped"' in cp and 'body["linksDropped"]' in cp, "drops folded into audit + response"


def test_source_shape_client():
    src = _html()
    assert "_frzPendingLinks" in src and "_frzRenderCreateLinks" in src, "create-mode link state + render"
    assert "pendingLinks:" in src, "the create save must send pendingLinks"
    assert "window._frzLinkSchemeOk = _frzLinkSchemeOk" in src, "the one client validator is reused (exposed on window)"
    assert "linksDropped" in src, "the client surfaces dropped links to the user"
    # Stage A invariants: the attachment control + its asset line are untouched.
    assert "Linked assets can be added once the item is saved." in src, "Stage A's asset line must survive"
    assert "_frzRenderCreateAttach(_assetSec); _frzResetPendingLinks(); _frzRenderCreateLinks(_assetSec)" in src, \
        "the link control appends after the attach control (does not overwrite its host)"
