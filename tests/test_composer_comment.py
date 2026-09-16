"""COMPOSER-COMMENT-1 guards: modal activity cap + modal comment box (roadmap.html only).

Part A caps the modal's Activity feed at the 2 most recent entries (the full feed lives on the item page,
reachable via the "Full history" link). Part B fixes the modal comment editor: (1) it is a shared editor
(frzMountCommentEditor) whose @mention typeahead resolved the reporter gate from the item page's _itemPageId
- unset in the modal/panel - so @reporter never appeared there; the fix binds the editor's own item and
threads it to _mentionUsers. (2) The editor is a classic <body> child OUTSIDE .frz-beta, so the .frz-beta
comment-editor styles never reach it and the editable rendered as a bare line; modal-scoped CSS gives it a
real box and reserves the placeholder height so clicking does not shrink the editable or shift the layout.

Source-shape guards; the visual/behaviour was screenshot-verified live in both themes. server.py is untouched
(empty diff). These fail when the stage is reverted.
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


# ── Part A: cap the modal activity feed at 2 ─────────────────────────────────────────────────────────────
def test_comment1_activity_capped_at_2():
    src = _html()
    m = re.search(r"function _frzComposerActivity\(id\)\{.*?\n  \}", src, re.DOTALL)
    assert m, "_frzComposerActivity not found"
    body = m.group(0)
    assert ".slice(0,2)" in body, "the modal activity feed must be capped at the 2 most recent entries"
    assert ".slice(0,6)" not in body, "the old 6-entry cap must be gone"
    # the Full history link is kept (the full feed lives on the item page)
    assert 'id="composerActivityFull"' in src, "the 'Full history' link must remain"


# ── Part B symptom 2: the @mention typeahead resolves the reporter from the ACTIVE editor's item ──────────
def test_comment1_mention_item_threaded_to_editor():
    src = _html()
    # the editor carries the comment's target item
    assert "ed._frzItem = p || _frzCurItem();" in src, "frzMountCommentEditor must bind the comment's item to the editor"
    # the mention probe passes that item to _mentionUsers
    assert "_mentionUsers(prefix, ed._frzItem)" in src, "the mention probe must pass the editor's item to _mentionUsers"


def test_comment1_mentionusers_uses_passed_item_with_fallback():
    src = _html()
    m = re.search(r"function _mentionUsers\(prefix, item\)\{.*?\n  \}", src, re.DOTALL)
    assert m, "_mentionUsers must accept the active item as a second argument"
    body = m.group(0)
    # resolve from the passed item first, else fall back to the item-page _itemPageId (item-page path unchanged)
    assert "var p = item || (function(){ var id=_val('_itemPageId')" in body, \
        "_mentionUsers must use the passed item first, then fall back to _itemPageId (keeps the item page byte-identical)"
    assert "p.source==='portal' && p.reporterEmail" in body, "the reporter gate (portal + email) is unchanged"


# ── Part B symptom 1: the modal comment editor gets a real box + reserved height (it is outside .frz-beta) ─
def test_comment1_modal_editor_reserved_and_styled():
    src = _html()
    # the editable gets a min-height + padding + border, scoped to the modal (the .frz-beta rules never reach
    # the composer modal, a classic <body> child). MODAL-SURFACE-2 Item 2 made the editable ONE line
    # (min-height:20, was 62) that grows with content; the box + padding + border remain.
    assert re.search(r"\.modal\.frz-composer #composerCommentComposer \.frz-rte\.frz-rte-comment \.ProseMirror \{ min-height: 20px;", src), \
        "the modal comment editable must have its one-line min-height (outside .frz-beta so the shared rule does not reach it)"
    assert re.search(r"\.modal\.frz-composer #composerCommentComposer \.frz-rte\.frz-rte-comment \{ border:", src), \
        "the modal comment editor must have a visible box (border)"
    # DARK-AUDIT-1 Part A: the no-shift RESERVE was removed (nothing sits below #composerComments, so
    # expanding on click pushes no visible content). The collapsed placeholder returns to ~36px. Fail-on-
    # revert: the 137px reserve must NOT come back on the placeholder or the container.
    assert "#composerCommentComposer { min-height: 137px;" not in src, \
        "the container reserve must stay removed (nothing follows the comment box, so no reserve is needed)"
    assert "#composerCommentComposer .composer-cmt-ph { min-height: 137px;" not in src, \
        "the placeholder reserve must stay removed (collapsed placeholder returns to ~36px)"


def test_comment1_invariants():
    src = _html()
    # the modal + item page + panel share ONE editor; the item-page/panel styling (.frz-beta) is untouched
    assert "function frzMountCommentEditor(p, opts)" in src, "the shared comment editor must still exist"
    assert ".frz-beta .frz-rte.frz-rte-comment .ProseMirror { min-height:20px" in src, \
        "the .frz-beta comment-editor rule (item page + List panel) must be unchanged"
    # server.py is untouched (client-only stage)
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "_frzComposerActivity" not in py and "composerCommentComposer" not in py, \
        "COMPOSER-COMMENT-1 is client-only; no symbol should appear in server.py"
