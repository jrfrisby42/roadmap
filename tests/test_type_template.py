"""TYPE-TEMPLATE-1 guard: per-Type description templates (roadmap.html only; client-side).

A Type carries a plain-text-shaped description template (label line per field, no structured blocks -
create mode is a toolbar-less contenteditable, verified). On create, selecting a Type stamps its
template into an EMPTY description only; existing content and existing items are never touched. The
template is stored ON THE TYPE OBJECT (types[i].template), NOT a parallel map keyed by name - so
deleteType's splice prunes it and the orphan class (the fourth in this app, after IT's status flags
and the already-orphaned typeScheduled) CANNOT occur. This is the load-bearing guard: it fails if the
template is moved to a parallel map. Verified element-level in both themes: stamp on create, never
overwrite, empty-desc swap, round-trip to the item page, dark authoring panel. server.py untouched.
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


def test_template_stored_on_type_object_not_parallel_map():
    """THE deletion guard: the template is a field on the type OBJECT, so deleteType's splice removes it.
    Revert-demo: move it to a parallel map (e.g. `typeTemplates[...] = v`) and this fails."""
    src = _html()
    assert "types[i].template=v" in src or "types[i].template = v" in src, \
        "setTypeTemplate must store the template ON the type object (types[i].template)"
    assert "delete types[i].template" in src, "an empty template must be removed from the type object (absent = default)"
    # deleteType must remain the plain splice (which prunes the template with the object) - no separate prune needed
    dt = src.split("function deleteType(i){", 1)[1][:300]   # window (avoid splitting on the ${...} template-literal brace)
    assert "types.splice(i, 1)" in dt or "types.splice(i,1)" in dt, "deleteType must splice the type object (prunes the template for free)"
    # no parallel template map keyed by name (the orphan-prone shape this build deliberately avoids)
    assert "typeTemplates" not in src, "there must be NO parallel typeTemplates map (orphan class)"


def test_template_stamps_create_only_never_overwrites():
    src = _html()
    stamp = src.split("function _frzStampTypeTemplate(){", 1)[1].split("\n}", 1)[0]
    assert "if(editingId!=null) return;" in stamp, "stamp must be create-mode only (never touches existing items)"
    assert "textContent.trim()===''" in stamp, "stamp must only fill an EMPTY description (never overwrite user content)"
    assert "_frzStampedHTML" in stamp, "stamp must track the prior auto-stamp so a Type change only replaces an untouched template"


def test_template_authoring_is_the_rich_editor():
    """TYPE-TEMPLATE-2 reverses TYPE-TEMPLATE-1's plain-text-shaped choice: CREATE-EDITOR-1 gave create
    mode a maintainable editor, so authoring now uses the SAME create-toolbar editor - what an admin
    authors is exactly what stamps (2.2 symmetry, now pointing the other way). Revert: swap the mount back
    for the bare textarea and the two surfaces diverge again."""
    src = _html()
    ot = src.split("function openTypeTemplate", 1)[1].split("function setTypeTemplate", 1)[0]
    assert "window._frzMountTemplateEditor(" in ot, "template authoring must mount the rich editor"
    assert "_frzTemplateToHTML(t.template" in ot, "an existing template loads through the plain->HTML converter"
    assert "does not change items that already exist" in src, "the admin note about non-propagation must stay"
    # the mount uses the CREATE toolbar, never the full set (headings/tables/panels are not maintainable here)
    mt = src.split("function _frzMountTemplateEditor(", 1)[1].split("\n  }", 1)[0]
    assert "buttons:_FRZ_TB_CREATE" in mt and "_FRZ_TB_FULL" not in mt, "authoring must use _FRZ_TB_CREATE, never _FRZ_TB_FULL"


def test_template_client_only():
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "_frzStampTypeTemplate" not in py and "setTypeTemplate" not in py, "TYPE-TEMPLATE-1 is client-only; no symbol in server.py"


# ── TYPE-TEMPLATE-2: templates carry formatting ────────────────────────────────────────────────────
def test_stamp_stops_escaping_carries_html():
    """TYPE-TEMPLATE-2: the stamp no longer escapes - templates carry HTML via _frzTemplateToHTML. Revert:
    re-introduce esc() per line and bold/list formatting stamps as visible literal text (proof case 1 fails)."""
    src = _html()
    st = src.split("function _frzStampTypeTemplate(){", 1)[1].split("\n}", 1)[0]
    assert "_frzTemplateToHTML(tmpl)" in st, "the stamp must build HTML via _frzTemplateToHTML"
    assert "esc(l)" not in st, "the stamp must NOT escape template lines any more (formatting must survive)"


def test_template_to_html_converter_has_both_paths():
    """_frzTemplateToHTML: an already-HTML template passes through; a legacy plain-line template converts to
    paragraphs (no data migration). Revert: drop the plain branch and every legacy plain template collapses
    to one line on stamp."""
    src = _html()
    fn = src.split("function _frzTemplateToHTML(", 1)[1].split("\n}", 1)[0]
    assert "return tmpl;" in fn, "an already-HTML template must be stamped as-is"
    assert "split('\\n')" in fn and "'<p>'+" in fn, "legacy plain lines must convert to paragraphs"
    assert "esc(" not in fn, "no escaping - templates carry HTML (frzSanitize runs on save)"


def test_sanitize_wrapper_exposed_for_authoring_save():
    """Authoring saves through the exposed frzSanitize wrapper (same store shape as the composer)."""
    src = _html()
    assert "window._frzSanitizeHTML=" in src, "the sanitize wrapper must be exposed for the template save path"
