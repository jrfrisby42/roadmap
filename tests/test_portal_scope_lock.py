"""PORTAL-SCOPE-LOCK-1: keep a team off the COMBINED public portal without making it
unreachable by its own link or by transfer.

`intakeCombined` (per-team config, default true):
  - false -> the team's projects are OMITTED from the anonymous all-teams /report list,
  - but its OWN scoped link /report?team=X still returns them (open:true),
  - and it stays a transfer target via the AUTHENTICATED /api/transfer/targets.

The public /api/intake/projects must never let an anonymous caller enumerate excluded teams;
the full list lives behind auth on /api/transfer/targets. And a default-true boolean must be
presence-only in _migrate_config_keys, or an admin's False reverts on the next boot.
"""
import json

import server

_n = {"i": 0}


def _uniq(tag):
    _n["i"] += 1
    return f"psl{tag}{_n['i']}"   # lowercase alnum -> passes the ^[a-z0-9]+$ listdir filter


def _mk_team(tag, combined="default", product=None):
    """Create an intake-ENABLED team. combined: True/False sets the flag; 'default' deletes it
    (simulates a team predating the key). product name is unique so results can be isolated."""
    slug = _uniq(tag)
    product = product or ("Prod" + slug.upper())
    server.init_team_db(slug)
    with server.db(slug) as c:
        def setcfg(k, v):
            c.execute("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, json.dumps(v)))
        setcfg("intakeEnabled", True)
        setcfg("products", [{"name": product}])
        setcfg("intakeProjects", [])   # empty = all products offered
        if combined == "default":
            c.execute("DELETE FROM config WHERE key='intakeCombined'")
        else:
            setcfg("intakeCombined", combined)
    return slug, product


def _hdr(slug):
    return {"Authorization": "Bearer " + server.create_token(slug, "admin", "admin"), "X-Team": slug}


def _products(resp):
    return {r["product"] for r in resp.json().get("projects", [])}


# ── The core behaviour ─────────────────────────────────────────────────────────────

def test_excluded_hidden_from_combined_but_reachable_by_link_and_transfer(client):
    incl, p_incl = _mk_team("incl", combined=True)
    excl, p_excl = _mk_team("excl", combined=False)
    dflt, p_dflt = _mk_team("dflt", combined="default")   # no key -> default true

    # Public combined list (no team): included + default present, excluded ABSENT.
    combined = _products(client.get("/api/intake/projects"))
    assert p_incl in combined
    assert p_dflt in combined          # missing key defaults to shown
    assert p_excl not in combined      # the whole point of the flag

    # The excluded team's OWN scoped link still works, open:true.
    scoped = client.get(f"/api/intake/projects?team={excl}")
    assert scoped.status_code == 200
    body = scoped.json()
    assert body.get("open") is True and p_excl in _products(scoped)

    # Transfer picker (authenticated) sees ALL enabled teams, excluded included.
    tr = client.get("/api/transfer/targets", headers=_hdr(incl))
    assert tr.status_code == 200
    tprods = _products(tr)
    assert {p_incl, p_excl, p_dflt} <= tprods


def test_transfer_targets_requires_auth(client):
    """Security crux: the full list (with excluded teams) is auth-gated. An anonymous caller
    gets 401/403 - there is no public 'show everything' path to enumerate excluded teams."""
    excl, p_excl = _mk_team("sec", combined=False)
    anon = client.get("/api/transfer/targets")
    assert anon.status_code in (401, 403)
    # And the excluded team is absent from the only public list an anon can read.
    assert p_excl not in _products(client.get("/api/intake/projects"))


# ── The migrate re-seed trap (the spec's Part 2 hazard) ─────────────────────────────

def test_intakecombined_false_survives_restart():
    slug, _ = _mk_team("boot", combined=False)
    # Simulate a restart: clear the per-process guard and re-run the migration.
    server._migrated_teams.discard(slug)
    server._migrate_config_keys(slug)
    with server.db(slug) as c:
        v = json.loads(c.execute("SELECT value FROM config WHERE key='intakeCombined'").fetchone()["value"])
    assert v is False   # NOT resurrected to true (presence-only key)


def test_missing_intakecombined_seeded_true_on_migrate():
    slug, _ = _mk_team("seed", combined="default")   # key deleted
    server._migrated_teams.discard(slug)
    server._migrate_config_keys(slug)
    with server.db(slug) as c:
        row = c.execute("SELECT value FROM config WHERE key='intakeCombined'").fetchone()
    assert row and json.loads(row["value"]) is True


# ── The helper default ──────────────────────────────────────────────────────────────

def test_intake_combined_helper_default_true():
    slug, _ = _mk_team("help", combined="default")
    assert server._intake_combined(slug) is True     # missing -> shown
    with server.db(slug) as c:
        c.execute("INSERT INTO config(key,value) VALUES('intakeCombined',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps(False),))
    assert server._intake_combined(slug) is False
