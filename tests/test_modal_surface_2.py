"""MODAL-SURFACE-2 guards: follow-up to the dark elevation repoint (roadmap.html only).

Item 1: the comment placeholder + mounted editor box read var(--surface2) (the recessed inner rung) like the
other 16 modal fields, not var(--surface) (the modal's own colour) where they had zero background delta.
Item 2: the editable is one line at rest and grows with content; the placeholder matches its one-line box.
Item 3: the comment toolbar is a centred flex row, so the svg-content image button aligns with the text
buttons (equal height was the wrong lever - .frz-beta's align-items never reached the modal). Item 4: three
foregrounds that fell below WCAG AA on the lifted 0.0232 surface are lifted, dark only (the info-blue, the
--accent2 red scoped to modals, the empty-select placeholder); the Urgent priority colour is left as an
accepted, protected consequence.

Source-shape guards; all colour/height claims were measured element-level in dark (comment surfaces 0.0160;
one line 38px collapsed == 37.5 mounted; toolbar topDelta 0; blue 5.06, red 5.77, placeholder 5.51; light
values unchanged). server.py untouched.
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "roadmap.html"
SERVER = ROOT / "server.py"


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


# ── Item 1: the comment surfaces recess to --surface2 ────────────────────────────────────────────────────
def test_ms2_comment_surfaces_recessed():
    src = _html()
    assert ".modal.frz-composer .composer-cmt-ph { border: 1px solid var(--border); border-radius: 8px; background: var(--surface2);" in src, \
        "the collapsed placeholder must read var(--surface2) (recessed), not the modal's own var(--surface)"
    assert ".modal.frz-composer #composerCommentComposer .frz-rte.frz-rte-comment { border: 1px solid var(--border); border-radius: 8px; background: var(--surface2); }" in src, \
        "the mounted comment editor box must read var(--surface2) (the second delta-zero element)"


# ── Item 2: one-line editable that grows ─────────────────────────────────────────────────────────────────
def test_ms2_editable_one_line():
    src = _html()
    assert re.search(r"\.frz-rte\.frz-rte-comment \.ProseMirror \{ min-height: 20px; max-height: 220px; overflow-y: auto;", src), \
        "the editable is one line (min-height:20) and grows to max-height:220 then scrolls"
    assert ".modal.frz-composer #composerCommentComposer .composer-cmt-ph { min-height: 38px; }" in src, \
        "the placeholder matches the one-line mounted box (38px)"


# ── Item 3: the toolbar is a centred flex row ────────────────────────────────────────────────────────────
def test_ms2_toolbar_flex_centre():
    src = _html()
    assert ".modal.frz-composer .frz-rte-tb-comment { display: flex; align-items: center; flex-wrap: wrap; gap: 3px; }" in src, \
        "the modal comment toolbar must be a centred flex row so the image button aligns (the .frz-beta rule never reached it)"


# ── Item 4: the three unprotected foregrounds lifted (dark only); Urgent left as accepted ────────────────
def test_ms2_foregrounds_lifted_dark_only():
    src = _html()
    # info-blue link/locks
    assert re.search(r"body\.dark-mode #delayActiveLock, body\.dark-mode #prActiveLock \{ color: #4C9DF2 !important; \}", src), \
        "the #0072f0 info-blue must lift to #4C9DF2 in dark"
    assert "body.dark-mode .modal.frz-composer .composer-lock-msg," in src, "the lock-message class must be included in the blue lift"
    # --accent2 red, scoped to modals (so the item page keeps its own --accent2)
    assert "body.dark-mode .modal-bg { --accent2: #F5828B; }" in src, \
        "--accent2 must lift to #F5828B inside modals in dark, scoped to .modal-bg (item-page invariant)"
    # empty-select placeholder
    assert 'body.dark-mode .modal-bg select option[value=""] { color: #9AA1AB !important; }' in src, \
        "the #888 empty-select placeholder must lift to #9AA1AB in dark"


def test_ms2_urgent_accepted_and_invariants():
    src = _html()
    # the Urgent accepted-consequence is recorded with the number
    assert "Urgent priority option (#e8394a) measures 3.49" in src and "left exactly" in src, \
        "the Urgent 3.49 must be recorded as an accepted, protected consequence"
    # invariant: badge literals + light --accent2 untouched
    assert '--accent2: #e8394a;' in src, "the LIGHT --accent2 (#e8394a) must be unchanged (dark-only fix)"
    assert '#e8394a">Urgent' in src or 'color:#e8394a">Urgent' in src, "the Urgent priority literal must be unchanged"
    # client-only
    py = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "F5828B" not in py and "frz-rte-tb-comment" not in py, "MODAL-SURFACE-2 is client-only; no symbol in server.py"
