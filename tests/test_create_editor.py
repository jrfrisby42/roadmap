"""CREATE-EDITOR-1: the rich description editor in CREATE mode, without images (roadmap.html only).

Create mode previously fell through to a plain #fDesc contenteditable (the `id==null` early return in
_frzInitModalNotes). This stage mounts the SAME Tiptap editor in create via `frzMountCreateNotes`, using
a create-only toolbar set `_FRZ_TB_CREATE` = the comment set MINUS the image button (inline image upload
needs an item id; Stage C is parked). Everything id-dependent is dropped in create: no _frzInsertImage
(image paste/drop swallowed, no half-upload), no _frzRehydrateEditorImages. The Type-template stamp is
routed through the editor's API when mounted (a raw #fDesc write under Tiptap desyncs its model). Cancel
still discards - the mount's _sync only mirrors editor HTML into the hidden #fDesc, never a server write.

These are STATIC SOURCE GUARDS (frontend behavior has no pytest harness; the rendered pass is done in the
browser). Each guard names its revert demonstration. server.py is untouched (client-only stage).
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


def _init_modal_notes(src):
    return src.split("function _frzInitModalNotes(", 1)[1].split("function frzMountModalNotes(", 1)[0]


def _create_mount(src):
    return src.split("function frzMountCreateNotes(", 1)[1].split("\n  }", 1)[0]


def _stamp(src):
    return src.split("function _frzStampTypeTemplate(){", 1)[1].split("\n}", 1)[0]


def test_create_toolbar_set_excludes_image_keeps_the_six():
    """Create set = comment set minus 'image'. Revert: put 'image' back and case 3 (image absent) fails."""
    src = _html()
    m = re.search(r"var _FRZ_TB_CREATE=(\[[^\]]*\]);", src)
    assert m, "_FRZ_TB_CREATE must be defined"
    arr = m.group(1)
    assert "'image'" not in arr, "the image button must NOT be in the create toolbar set"
    for b in ("'bold'", "'italic'", "'strike'", "'bullet'", "'ordered'", "'link'"):
        assert b in arr, f"the create toolbar must keep {b}"


def test_init_routes_create_to_the_create_mount_not_an_early_return():
    """Revert: restore `|| id==null) return;` and create mode falls back to the plain box (feature gone)."""
    body = _init_modal_notes(_html())
    assert "frzMountCreateNotes(fd)" in body, "create (id==null) must mount the create editor"
    assert "|| id==null) return" not in body, "the id==null early-return must be gone (that was the blocker)"
    # still gated the same way otherwise
    assert "!root || !frzRteEnabled() || !canEdit" in body, "gate stays root + rte flag + admin/editor"


def test_create_mount_uses_create_toolbar_and_no_id_dependencies():
    src = _html()
    cm = _create_mount(src)
    assert "buttons:_FRZ_TB_CREATE" in cm, "create mount must use the create (no-image) toolbar set"
    assert "_frzInsertImage(" not in cm, "create must NOT call the upload path (no half-upload)"
    assert "_frzRehydrateEditorImages(" not in cm, "create must NOT call rehydrate (no stored inline images)"
    assert "_frzWireMentions(ed)" in cm, "mentions still wired (they degrade gracefully with no item)"
    # image paste is swallowed (return true), not inserted/uploaded
    assert "handlePaste" in cm and "return true" in cm, "an image paste must be swallowed in create"


def test_stamp_is_editor_aware():
    """Revert: drop the editor branch (always fd.innerHTML=html) and the stamp desyncs a mounted Tiptap
    (case 6: stamp invisible in the editor)."""
    st = _stamp(_html())
    assert "host._frzEd" in st, "the stamp must detect the mounted create editor"
    assert "setContent(" in st, "the stamp must route through the editor API when mounted"
    # the create-only + never-overwrite guarantees are unchanged
    assert "if(editingId!=null) return;" in st
    assert "fd.textContent.trim()===''" in st and "_frzStampedHTML" in st


def test_edit_mount_unchanged_invariant():
    """INVARIANT: edit mode keeps the comment toolbar (with image) and still rehydrates images."""
    src = _html()
    em = src.split("function frzMountModalNotes(", 1)[1].split("function frzMountCreateNotes(", 1)[0]
    assert "buttons:_FRZ_TB_COMMENT" in em, "edit mode keeps the comment set (image included)"
    assert "_frzRehydrateEditorImages(ed, id)" in em, "edit mode still rehydrates stored images"
    assert "'image'" in re.search(r"var _FRZ_TB_COMMENT=(\[[^\]]*\]);", src).group(1), \
        "the comment set (edit + comments) still has image - unchanged"


def test_no_new_tiptap_extension_and_no_underline():
    """Out of scope: no new extension, no underline (Part 3)."""
    src = _html()
    cm = _create_mount(src)
    assert "_frzMakeExtensions(T)" in cm, "create reuses the existing extension factory (no new extension)"
    assert "underline" not in cm.lower(), "underline is out of scope for this stage"


def test_server_untouched_client_only():
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    for sym in ("frzMountCreateNotes", "_FRZ_TB_CREATE"):
        assert sym not in py, "CREATE-EDITOR-1 is client-only; no symbol in server.py"
