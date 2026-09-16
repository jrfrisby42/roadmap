"""SCOPE-AUDIT-2 (Option 3) guard: one unscoped rich-text content vocabulary (roadmap.html only).

Part B found the `.frz-beta`-scope tax: the rich-text CONTENT rules were `.frz-beta .frz-rte *` and so never
reached the item composer modal (a classic <body> child outside .frz-beta), which mirrored a subset of them by
ID on #frzModalNotes - the tax behind occurrences 1-5. Option 3 consolidates: the content vocabulary is UNSCOPED
to a single `.frz-rte` root (a dedicated editor class, never generic, so unscoping cannot leak), the modal
description editor #frzModalNotes now WEARS `.frz-rte`, and the redundant #frzModalNotes content mirror is deleted.

The decisive detail (found by measurement, not assumption): #frzModalNotes carries `frz-rte-modal`, NOT `frz-rte`.
An unscoping that only targeted `.frz-rte` would silently miss the modal description - the exact shape of an inert
config ship (correct rule, never reached). So this guard asserts the modal editor carries `.frz-rte` specifically,
not merely that the rules unscoped. All colour claims were measured element-level in the running modal, light AND
dark: modal #frzModalNotes == the .frz-beta .frz-rte reference for h1(20)/h3(14)/blockquote(3px bar)/pre(boxed)/
inline-code(boxed)/all 5 panel variants/expand, in both themes; the item-page editor was unchanged. server.py untouched.
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


def test_scopeaudit2_content_vocabulary_unscoped():
    src = _html()
    # a representative sample of the content vocabulary is UNSCOPED (`.frz-rte X`, no `.frz-beta ` prefix)
    for rule in (
        ".frz-rte h1 { font-size:20px;",
        ".frz-rte h3 { font-size:14px;",
        ".frz-rte blockquote { border-left:3px solid var(--frz-border);",
        ".frz-rte pre { background:var(--frz-bg-sunken);",
        ".frz-rte .frz-panel[data-variant=info] { background:#e7f1fa;",
        ".frz-rte details.frz-expand {",
    ):
        assert rule in src, f"content vocabulary must be unscoped: missing `{rule}`"
    # and the old scoped forms are GONE (they were the scope tax)
    assert ".frz-beta .frz-rte h1" not in src, "the `.frz-beta .frz-rte` scoped content rules must be gone (unscoped now)"
    assert ".frz-beta .frz-rte .frz-panel[data-variant=info]" not in src, "scoped panel-variant rules must be gone"
    # dark variants unscoped too
    assert "body.dark-mode .frz-rte .frz-panel[data-variant=purple] { background:#2a2042;" in src, \
        "the dark panel variants must be unscoped alongside the light ones"
    assert "body.dark-mode .frz-beta .frz-rte .frz-panel" not in src, "no dark `.frz-beta .frz-rte` panel rule may remain"


def test_scopeaudit2_modal_editor_wears_frz_rte():
    """The inert-ship guard: #frzModalNotes must carry `.frz-rte` so the unscoped block reaches it.
    Without this the rules would be correct but never reach the modal - a silent inert ship."""
    src = _html()
    assert "host.id='frzModalNotes'; host.className='frz-rte-modal frz-rte';" in src, \
        "the modal description editor #frzModalNotes must be created wearing BOTH frz-rte-modal AND frz-rte"


def test_scopeaudit2_redundant_modal_mirror_deleted():
    src = _html()
    # the by-ID content reproductions are gone (superseded by the shared block); only LAYOUT stays
    for gone in (
        "#frzModalNotes p {",
        "#frzModalNotes table {",
        "#frzModalNotes .frz-panel {",
        "#frzModalNotes .frz-mention {",
    ):
        assert gone not in src, f"redundant modal content mirror must be deleted: `{gone}` still present"
    # but the modal box LAYOUT is kept (ID specificity wins over `.frz-rte .ProseMirror`)
    assert "body.frz-beta-active #frzModalNotes { flex:1;" in src, "the modal box layout rule must be kept"
    assert "body.frz-beta-active #frzModalNotes .ProseMirror { flex:1; min-height:90px;" in src, \
        "the modal .ProseMirror layout (min-height:90) must be kept - ID specificity beats the shared .frz-rte .ProseMirror"


def test_scopeaudit2_invariants():
    src = _html()
    # INVARIANT: the classic flag-OFF fallback editor `.rte-box` keeps its own .frz-beta-scoped mirror (separate root)
    assert ".frz-beta .rte-box .frz-panel[data-variant=info]" in src, \
        "the `.rte-box` classic fallback mirror must stay .frz-beta-scoped (a separate root, out of scope for this pass)"
    # client-only
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "frz-rte" not in py, "SCOPE-AUDIT-2 is client-only; no frz-rte symbol in server.py"
