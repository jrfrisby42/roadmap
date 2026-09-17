"""INTAKE-TEMPLATE-1: append the resolved Type's description template to portal items.

Per-Org opt-in (`intakeAppendTemplate`, off by default). When on, a portal submission with a non-empty
description whose resolved Type carries a template gets the template appended below the reporter's text,
separated by _INTAKE_TEMPLATE_SEP. Templates are stored as HTML (normalized once by
tools/migrate_normalize_templates.py), so the append is a verbatim concat - there is NO second
template-to-HTML conversion in the server portal path. Best-effort: never fails the submission.
"""
import importlib.util
import pathlib

import server

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEP = server._INTAKE_TEMPLATE_SEP

TMPL_BUG = "<p><strong>Steps to reproduce</strong>:</p><ul><li><p>-</p></li></ul>"
TMPL_REQ = "<p><strong>What do you need</strong>:</p>"


def _set(client, admin_headers, key, value):
    return client.put(f"/api/config/{key}", json=value, headers=admin_headers)


def _expose(client, admin_headers, append=False, default_type=""):
    _set(client, admin_headers, "statuses", ["New", "In Progress", "Released"])
    _set(client, admin_headers, "statusIsDefault", {"New": True})
    _set(client, admin_headers, "types", [{"name": "Bug", "template": TMPL_BUG},
                                          {"name": "Feature"},
                                          {"name": "Request", "template": TMPL_REQ}])
    _set(client, admin_headers, "products", [{"name": "Fraznet"}])
    _set(client, admin_headers, "intakeEnabled", True)
    _set(client, admin_headers, "intakeDefaultType", default_type)
    _set(client, admin_headers, "intakeAppendTemplate", append)


def _submit(client, team, **kw):
    server._rate.clear()
    body = {"title": "T", "description": "reporter said this", "email": "r@example.com", "name": "Pat"}
    body.update(kw)
    return client.post(f"/api/intake/{team}", json=body)


def _item(client, admin_headers, title="T"):
    ps = client.get("/api/all", headers=admin_headers).json()["projects"]
    return next(p for p in ps if p["name"] == title)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_norm", ROOT / "tools" / "migrate_normalize_templates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── case 1 (INVARIANT): flag off -> description is exactly today's ───────────────────────────────────
def test_flag_off_no_append(client, team, admin_headers):
    _expose(client, admin_headers, append=False)
    assert _submit(client, team, type="Bug").status_code in (200, 201)
    assert _item(client, admin_headers)["description"] == "reporter said this"   # no template, no separator


# ── case 2 / 7: flag on, template present -> appended below, byte-exact ───────────────────────────────
def test_flag_on_appends_template_byte_exact(client, team, admin_headers):
    _expose(client, admin_headers, append=True)
    assert _submit(client, team, type="Bug").status_code in (200, 201)
    desc = _item(client, admin_headers)["description"]
    assert desc == "reporter said this" + SEP + TMPL_BUG              # reporter text first, unaltered, then sep + template
    assert desc.index("reporter said this") < desc.index(SEP) < desc.index(TMPL_BUG)


# ── case 3: empty reporter description -> nothing appended ───────────────────────────────────────────
def test_empty_description_skips_append(client, team, admin_headers):
    _expose(client, admin_headers, append=True)
    assert _submit(client, team, type="Bug", description="").status_code in (200, 201)
    assert _item(client, admin_headers)["description"] == ""          # a bare form is worse than nothing


# ── case 4: type has no template -> nothing appended ─────────────────────────────────────────────────
def test_type_without_template_skips_append(client, team, admin_headers):
    _expose(client, admin_headers, append=True)
    assert _submit(client, team, type="Feature").status_code in (200, 201)
    assert _item(client, admin_headers)["description"] == "reporter said this"


# ── case 5: the reporter's picked type is the one used ───────────────────────────────────────────────
def test_reporter_picked_type_template_used(client, team, admin_headers):
    _expose(client, admin_headers, append=True, default_type="Bug")   # default is Bug...
    assert _submit(client, team, type="Request").status_code in (200, 201)   # ...but the reporter picked Request
    assert _item(client, admin_headers)["description"] == "reporter said this" + SEP + TMPL_REQ


# ── case 6: the default-supplied type is used when the reporter picks none ────────────────────────────
def test_default_type_template_used(client, team, admin_headers):
    _expose(client, admin_headers, append=True, default_type="Bug")
    assert _submit(client, team, type="").status_code in (200, 201)   # blank -> default (Bug)
    it = _item(client, admin_headers)
    assert it["type"] == "Bug" and it["description"] == "reporter said this" + SEP + TMPL_BUG


# ── case 8: append is best-effort - a submission still succeeds (never raises) ────────────────────────
def test_submission_succeeds_and_append_is_guarded():
    src = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
    sub = src.split("def intake_submit(", 1)[1].split("\n@app.", 1)[0]
    assert "_cfg_val(team, \"intakeAppendTemplate\", False)" in sub, "append is gated on the per-Org flag"
    assert "desc = desc + _INTAKE_TEMPLATE_SEP + _tmpl" in sub, "append is a verbatim concat (no conversion)"
    assert "except Exception" in sub.split("_INTAKE_TEMPLATE_SEP", 1)[1][:400], "the append must be best-effort (try/except)"


# ── the no-second-conversion invariant (the whole point of the migration) ────────────────────────────
def test_no_template_conversion_in_server_portal_path():
    """The portal appends the STORED template verbatim; the conversion lives only in the one-time migration
    tool, never in server.py's runtime. Revert: reimplement _frzTemplateToHTML in server and this fails."""
    src = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
    assert "_frzTemplateToHTML" not in src, "no client-conversion symbol in server.py"
    # the type-template lookup returns the stored string as-is (no wrapping / newline conversion)
    fn = src.split("def _intake_type_template(", 1)[1].split("\ndef ", 1)[0]
    assert "split('\\n')" not in fn and "'<p>'" not in fn, "the lookup must not convert - templates are pre-normalized to HTML"


# ── the normalization migration ──────────────────────────────────────────────────────────────────────
def test_migration_to_html_conversion():
    mig = _load_migration()
    # already-HTML template passes through untouched
    html_tmpl = "<p><strong>A</strong>:</p><ul><li><p>x</p></li></ul>"
    assert mig.to_html(html_tmpl) == html_tmpl
    # legacy plain lines -> paragraphs (matching _frzTemplateToHTML)
    assert mig.to_html("A:\nB:\nC:") == "<p>A:</p><p>B:</p><p>C:</p>"
    # blank line -> <br> paragraph
    assert mig.to_html("A:\n\nB:") == "<p>A:</p><p><br></p><p>B:</p>"
    # empty -> empty
    assert mig.to_html("") == "" and mig.to_html("   ") == ""


def test_migration_is_idempotent():
    mig = _load_migration()
    once = mig.to_html("Measurements:\nHierarchy:")
    assert mig.to_html(once) == once, "converting an already-converted template must be a no-op"
    # normalize_types reports converted count and is stable on a second pass
    types = [{"name": "Plain", "template": "A:\nB:"}, {"name": "Html", "template": "<p>x</p>"}, {"name": "None"}]
    types2, converted = mig.normalize_types(types)
    assert converted == 1                                  # only the plain one
    _, converted2 = mig.normalize_types(types2)
    assert converted2 == 0                                 # idempotent
