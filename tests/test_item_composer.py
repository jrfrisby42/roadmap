"""ITEM-COMPOSER-1 Stage 1 guard: the item modal becomes a responsive composer shell.

The item modal (#modalBg > .modal.frz-composer) is restructured into a fixed header, a single
scrolling body and a fixed footer, full-screen below 640px, with a focus trap, background scroll
lock, focus restore and Escape-with-confirm - all scoped to the item modal (the shared .modal is
untouched). NO registered field is moved in Stage 1.

SOURCE-SHAPE guards (roadmap.html). The visual (header/body/footer render, focus, scroll lock) was
screenshot- and query-verified live; mobile full-screen, the real focus-trap keystrokes and the
Escape-confirm are J.R.'s on-device checks. These fail when Stage 1 is reverted. server.py untouched.
"""
import re
import pathlib

ROADMAP = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"
SERVER = pathlib.Path(__file__).resolve().parent.parent / "server.py"


def _html() -> str:
    return ROADMAP.read_text(encoding="utf-8", errors="replace")


def test_composer_shell_markup():
    src = _html()
    assert '<div class="modal-bg" id="modalBg">\n  <div class="modal frz-composer">' in src, \
        "the item modal must carry the frz-composer class (scoping the shell to it, not the shared .modal)"
    assert '<div class="composer-header"' in src, "a fixed header region"
    assert '<div class="composer-body">' in src, "a scrolling body wrapper around the fields"
    # the change-reason panel must sit OUTSIDE the scrolling body (pinned above the fixed footer)
    m = re.search(r'<div class="composer-body">.*?</div><!-- end composer-body -->', src, re.DOTALL)
    assert m, "the composer-body wrapper must open and close"
    assert 'id="changeReasonPanel"' not in m.group(0), "the change-reason panel must be pinned OUTSIDE the scrolling body"


def test_composer_shell_css():
    src = _html()
    assert re.search(r"\.modal\.frz-composer \{[^}]*display: flex[^}]*flex-direction: column", src), "the shell is a flex column"
    assert re.search(r"\.modal\.frz-composer \.composer-body\s*\{[^}]*overflow-y: auto", src), "only the body scrolls"
    assert re.search(r"\.modal\.frz-composer \.composer-header\s*\{[^}]*flex: 0 0 auto", src), "the header is fixed"
    assert re.search(r"\.modal\.frz-composer \.modal-actions\s*\{[^}]*flex: 0 0 auto", src), "the footer is fixed"
    # full-screen below the established 640px breakpoint
    assert re.search(r"@media \(max-width: 640px\)\{[^@]*\.modal\.frz-composer \{[^}]*width: 100vw", src), "full-screen composer at <=640px"
    # background scroll lock targets the shell scroller (.frz-content), not just body
    assert "html.composer-lock .frz-beta .frz-content { overflow: hidden; }" in src, "the shell scroller must be locked while open"
    # the shared .modal mobile rule is carved out so other modals are untouched
    assert ".modal:not(.frz-composer) { width: 98vw !important;" in src, "the shared 768px .modal rule must exclude the composer"


def test_composer_a11y_observer():
    src = _html()
    assert "new MutationObserver(function(){" in src and "attributeFilter:['class']" in src, "the shell a11y is centralised on #modalBg's class"
    assert "document.documentElement.classList.add('composer-lock')" in src, "opening locks background scroll"
    assert "if(e.key!=='Escape'" in src and "confirm('Discard unsaved changes?')" in src, "Escape confirms when there are unsaved changes"
    assert "if(e.isTrusted) dirty=true" in src, "only user-initiated edits mark the form dirty (not programmatic/Tiptap init)"
    # Change 1: Escape AND the backdrop click share one discard confirm (consistent close paths)
    assert "function confirmDiscard(){ return !dirty || confirm('Discard unsaved changes?'); }" in src, "one shared discard-confirm helper"
    assert "e.currentTarget.__composerConfirmDiscard && !e.currentTarget.__composerConfirmDiscard()" in src, "the backdrop click must use the same confirm as Escape"
    assert "if(trapH) mb.removeEventListener('keydown', trapH)" in src, "the focus trap is removed on close"
    assert "if(lastFocus && lastFocus.focus)" in src, "focus is restored to the originating control on close"


def test_no_registered_field_lost():
    # Stage 0 checklist: every field the save reads must still exist in the markup after the wrapping.
    src = _html()
    for fid in ["fName", "fDesc", "fDev", "fType", "fProduct", "fStatus", "fRecurrence", "fSyncChildren",
                "fRequires", "fParallel", "fParent", "fHidden", "fPriority", "fStart", "fDueWeeks",
                "fRevised", "fRevisedOffset", "fExpected", "fTestWeeks", "fParallelResources", "fRelease",
                "fChangeReason", "fChangeNote"]:
        assert f'id="{fid}"' in src, f"registered field #{fid} must not be dropped by the shell restructure"


def test_server_untouched_by_stage1():
    # Stage 1 is roadmap.html only.
    src = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "frz-composer" not in src, "server.py must not be touched by the composer shell"


# ── Stage 2: header + actions ─────────────────────────────────────────────────
def test_stage2_header_markup():
    src = _html()
    assert '<div class="composer-id" id="composerId"></div>' in src, "the left identity group is populated dynamically"
    assert 'class="composer-actions"' in src, "the right actions group"
    assert 'id="composerWatch"' in src and 'id="composerClose"' in src, "watcher count + a visible Close control"
    assert 'id="flagIssueBtn"' in src and 'id="modalOpenPageBtn"' in src, "Flag + Open Full Item live in the header"
    # the old plain title h2 is gone (replaced by the identity group)
    assert '<h2 id="modalTitle"' not in src, "the old #modalTitle h2 must be replaced by the identity group"
    # Stage 7 mobile-exit: a visible Close exists in the header (not Cancel-only on a full-screen sheet)
    assert 'id="composerClose" title="Close" aria-label="Close"' in src, "an accessible header Close control"
    # no emoji / pictographs in the header - the header uses inline SVGs only
    import re as _re
    hdr = _re.search(r'<div class="composer-header">.*?</div>\s*</div>\s*\n\s*<div class="composer-body">', src, _re.DOTALL)
    assert hdr, "header block not found"
    assert not _re.search(r'[\U0001F000-\U0001FAFF\u2600-\u27BF]', hdr.group(0)), "no emoji/pictographs in the header"


def test_stage2_header_css_one_centreline_and_sizing():
    src = _html()
    # both identity groups sit on one centreline
    assert _re_css(src, r"\.composer-header \{[^}]*align-items: center"), "the header row centres its content"
    assert _re_css(src, r"\.composer-id \{[^}]*align-items: center"), "the identity group is a centred flex row"
    assert _re_css(src, r"\.composer-act \{[^}]*height: 30px"), "actions share a consistent control height"


def test_stage2_header_builder_and_watchers():
    src = _html()
    assert "function _frzComposerHeader(p, id){" in src, "the header builder exists"
    # edit: type icon (self-sized via typeIconHTML) + key + Jira TEXT (no icon asset)
    assert "typeIconHTML(p.type, {size:18, label:p.type})" in src, "edit shows a self-sized type icon"
    assert "'<a class=\"composer-jira\" href='" not in src  # sanity (we use double quotes)
    assert 'composer-jira' in src and "Jira '+esc(jk)" in src, "Jira is a text action + key (no approved Jira icon asset)"
    # create: context icon + New item + Space
    assert "_COMPOSER_CREATE_IC" in src and "'New item'" in src.replace('"New item"', "'New item'") or "New item" in src
    assert "composer-space" in src, "create shows the destination Space when known"
    # watcher count from the item blob (no fetch); comment count is NOT fetched here
    assert "Array.isArray(p._watchers)) ? p._watchers.length : 0" in src, "watcher count comes from the item blob, no fetch"
    assert "_ipCommentCount" not in _html_builder(src), "the header must NOT fetch comments (comment count is out of scope this stage)"


def test_stage2_close_uses_confirm():
    src = _html()
    m = re.search(r"getElementById\('composerClose'\)\?\.addEventListener\('click',.*?\}\);", src, re.DOTALL)
    assert m and "__composerConfirmDiscard" in m.group(0), "the header Close routes through the shared discard confirm"


def _re_css(src, pat):
    import re as _re
    return bool(_re.search(r"\.modal\.frz-composer " + pat, src))


def _html_builder(src):
    m = re.search(r"function _frzComposerHeader\(p, id\)\{.*?\n\}", src, re.DOTALL)
    return m.group(0) if m else ""
