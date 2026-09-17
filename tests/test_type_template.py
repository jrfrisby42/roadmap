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


def test_template_authoring_is_plain_text_shaped():
    """2.2: the authoring surface is plain-text (a textarea), not a rich editor - it must not offer a
    capability the toolbar-less create surface cannot maintain."""
    src = _html()
    assert "class=\"type-tmpl-ta\"" in src and "<textarea" in src.split("function openTypeTemplate", 1)[1][:1200], \
        "template authoring must be a plain textarea (plain-text-shaped)"
    assert "does not change items that already exist" in src, "the admin note about non-propagation must be present"


def test_template_client_only():
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "_frzStampTypeTemplate" not in py and "setTypeTemplate" not in py, "TYPE-TEMPLATE-1 is client-only; no symbol in server.py"
