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


# ── Stage 3: create-mode canvas/rail + editor-panel absorption ────────────────
# These are SOURCE-SHAPE guards over roadmap.html. The live visual (admin + editor create modals) and
# the per-role field-set parity were screenshot- and DOM-verified during the build; these fail when
# Stage 3 is reverted. server.py is untouched by this stage. Invariants are labelled INVARIANT.

def test_stage3_canvas_rail_structure():
    src = _html()
    assert '<div class="composer-cols">' in src, "the body splits into a content canvas + property rail"
    assert '<div class="composer-canvas">' in src and '<div class="composer-rail">' in src, "canvas + rail regions"
    assert '<input type="text" id="fName" class="composer-title"' in src, "the item name is the hero title in the canvas"
    # the 3-cell quick-edit grid: Priority (admin) + Space + Type, as value cells not labelled select rows
    assert '<div class="composer-qgrid">' in src, "a quick-edit grid, not stacked select rows"
    assert '<div class="composer-qcell" id="priorityRow"' in src, "Priority is a grid cell (moved out of the footer)"
    assert 'id="qcellProduct"' in src and 'id="qcellType"' in src, "Space + Type are grid cells"
    # collapsible groups: Ownership open, the rest collapsed
    assert '<details class="composer-group" id="grpOwnership" open>' in src, "Ownership is open on create"
    for gid in ["grpSchedule", "grpRelationships", "grpAdvanced"]:
        assert f'<details class="composer-group" id="{gid}">' in src, f"{gid} is a collapsible group, collapsed by default"


def test_stage3_editor_panel_retired():
    # INVARIANT: the role-scoped #editorFieldPanel and every proxy select it created are gone, replaced
    # by one unified rail gated per-field. This is the core of the stage; it fails on revert. (Checks the
    # runtime CODE constructs, not bare names - the names legitimately survive in explanatory comments.)
    src = _html()
    assert "panel.id = 'editorFieldPanel'" not in src, "the injected editor panel must not be recreated"
    assert "getElementById('editorFieldPanel')" not in src, "no code path looks up the retired panel"
    for proxy in ["fProductEditor", "fStatusEditor", "fDueWeeksEditor", "fRevisedOffsetEditor",
                  "fExpectedEditor", "fParentEditorInput", "editorJiraInlineRow"]:
        assert f"getElementById('{proxy}')" not in src, f"the retired editor proxy {proxy} must not be referenced"
    assert "window._syncEditorJiraList" not in src, "the retired editor Jira-list sync must be gone"
    assert "function frzApplyComposerRoleScope(p){" in src, "per-field role scope replaces the panel"
    # the unified layout classes replace the old two-column ones in the composer markup
    assert 'class="proj-modal-cols"' not in src and 'class="proj-modal-left"' not in src, "the old column markup is gone"


def test_stage3_role_scope_gates_the_panel_only_fields():
    # INVARIANT (per-role parity): editors keep exactly the retired panel's editable set. The role-scope
    # fn hides Start + Depends-On (never in the panel) and makes Test Period read-only; the other
    # editor-omitted fields keep their existing admin-only gates. No field is newly exposed to editors.
    src = _html()
    m = re.search(r"function frzApplyComposerRoleScope\(p\)\{.*?\n\}", src, re.DOTALL)
    assert m, "role-scope function not found"
    body = m.group(0)
    assert "editorOnly" in body, "the scope keys off editor-not-admin"
    assert "getElementById('fStart')" in body and "getElementById('dependsOnRow')" in body, "Start + Depends-On are hidden for editors"
    assert "getElementById('fTestWeeks')" in body and "editorScheduleNote" in body, "Test read-only + the not-broken note (constraint #4)"
    # existing admin-only gates still resolve on the real rows (moved into editor-visible groups)
    assert "priorityRow.style.display = isAdmin ? '' : 'none'" in src, "Priority admin-gate uses '' (grid cell), not the old footer 'flex'"
    assert "hideFromFlowRow" in src, "Hide-from-Flow moved into Advanced and stays admin-gated"


def test_stage3_editor_cannot_select_released():
    # INVARIANT: the retired panel filtered Released out of the editor status list. That exclusion moves
    # onto the single real #fStatus, keyed on role, so it is not newly exposed to editors.
    src = _html()
    assert "validStatuses.filter(s => !statusIsReleased[s] || s === prev)" in src, \
        "editors cannot SET a released status (an already-released item keeps its value shown)"


def test_stage3_change_reason_trigger_rehomed_to_real_field():
    # INVARIANT (constraint #3): the editor delay change-reason trigger used to hang off the retired
    # #fRevisedOffsetEditor proxy's onchange. It must be re-homed onto the REAL #fRevisedOffset change
    # listener so retiring the proxy does not silently drop the editor's reason path. Stage 6 owns the
    # full dual-history acceptance.
    src = _html()
    m = re.search(r"getElementById\('fRevisedOffset'\)\?\.addEventListener\('change', \(\)=>\{.*?\}\);", src, re.DOTALL)
    assert m, "the real fRevisedOffset change listener not found"
    assert "checkEditorReasonNeeded();" in m.group(0), "the editor reason trigger must fire from the real field's change"


def test_stage3_footer_label_and_context():
    src = _html()
    assert "id?'Save Changes':'Create Item'" in src, "create saves via a Create Item button (not 'Add Item')"
    assert 'id="composerFooterSpace"' in src, "the footer shows the destination Space context"
    assert "function _frzUpdateComposerFooterSpace(){" in src, "footer Space context is kept in sync with the picker"


def test_stage3_validation_reveals_collapsed_group():
    src = _html()
    m = re.search(r"function _frzRevealComposerField\(fieldId\)\{.*?\n\}", src, re.DOTALL)
    assert m, "the validation-reveal helper not found"
    body = m.group(0)
    assert "grp.open = true" in body and "classList.add('has-error')" in body and ".focus()" in body, \
        "a collapsed group with an error auto-opens, is marked, and focus moves to the field"
    # wired at the schedule-buried validations (test-period and release-required)
    assert "_frzRevealComposerField('fTestWeeks')" in src, "the test-period guard reveals the Schedule group"
    assert "_frzRevealComposerField('fRelease')" in src, "the release-required guard reveals the Schedule group"


def test_stage3_server_untouched():
    # Stage 3 is roadmap.html only.
    src = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "frz-composer" not in src and "frzApplyComposerRoleScope" not in src, "server.py must not be touched by Stage 3"
