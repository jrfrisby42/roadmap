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


# ── Stage 4: edit mode (metadata strip + activity + per-mode group defaults) ───
# SOURCE-SHAPE guards over roadmap.html. The live edit modal (scheduled/unscheduled, admin/editor),
# the create-hides-both reset, and the activity reuse were screenshot- and DOM-verified during the
# build. These fail when Stage 4 is reverted. server.py is untouched (only the version bump).

def test_stage4_strip_and_activity_markup_edit_only():
    src = _html()
    assert '<div class="composer-meta-strip" id="composerMetaStrip" style="display:none"></div>' in src, \
        "the read-only metadata strip exists and is hidden by default (edit-only)"
    assert '<div class="composer-activity" id="composerActivity" style="display:none">' in src, \
        "the activity section exists and is hidden by default (edit-only)"
    assert 'id="composerActivityList"' in src and 'id="composerActivityFull"' in src, \
        "activity has a list container and a full-history link to the item page"


def test_stage4_strip_is_readonly_facts_not_grid_duplication():
    # The strip carries ONLY facts with no other modal home (Assignee/Reporter/Departments). Space,
    # Type and Priority stay editable in the quick-edit grid and must NOT be duplicated as strip chips.
    src = _html()
    m = re.search(r"function _frzComposerMetaStrip\(p\)\{.*?\n\}", src, re.DOTALL)
    assert m, "the metadata-strip builder not found"
    body = m.group(0)
    assert "displayName(p.assignee)" in body, "Assignee is shown read-only via displayName (no modal field)"
    assert "p.reporter" in body and "p.departments" in body, "Reporter + Departments are the other read-only facts"
    for editable in ["fProduct", "fType", "fPriority"]:
        assert editable not in body, f"the strip must not duplicate the editable grid field {editable}"


def test_stage4_activity_reuses_existing_source_no_new_endpoint():
    # INVARIANT: activity is the EXISTING in-memory `activities` audit source, item-scoped and limited -
    # no fetch, no new endpoint, no comments (comments are Stage 4B).
    src = _html()
    m = re.search(r"function _frzComposerActivity\(id\)\{.*?\n\}", src, re.DOTALL)
    assert m, "the activity builder not found"
    body = m.group(0)
    assert "activities" in body and "a.item_id !== id" in body, "reads the in-memory activities array, item-scoped"
    assert ".slice(0,6)" in body, "shows a LIMITED set (full history stays on the item page)"
    assert "API.get" not in body and "/api/" not in body and "fetch(" not in body, "no fetch / new endpoint"
    assert "/api/comments" not in body, "activity is not comments (Stage 4B)"


def test_stage4_group_defaults_scheduled_and_needs_attention():
    src = _html()
    m = re.search(r"function _frzComposerGroupDefaults\(p, id\)\{.*?\n\}", src, re.DOTALL)
    assert m, "the group-defaults helper not found"
    body = m.group(0)
    assert "set('grpOwnership', true)" in body, "Ownership is always open"
    assert "set('grpSchedule', !!p.start)" in body, "Schedule & Capacity opens only when the item is actually scheduled"
    assert "p.recurrence" in body and "ignoreConflictsWith" in body and "p.hidden" in body, \
        "Advanced opens only when a value needs attention"


def test_stage4_footer_last_modified_never_all_changes_saved():
    src = _html()
    m = re.search(r"function _frzUpdateComposerFooterSpace\(\)\{.*?\n\}", src, re.DOTALL)
    assert m, "the footer-status function not found"
    body = m.group(0)
    assert "'Last modified '" in body, "edit mode footer shows Last modified from updated_ts"
    # the rendered footer must never claim autosave; check the function body, not our explaining comment
    assert "All changes saved" not in body, "the modal footer must never render 'All changes saved'"


def test_stage4_wired_edit_only_in_open():
    src = _html()
    assert "_frzComposerMetaStrip(id ? p : null);" in src, "strip populates on edit, hides on create"
    assert "_frzComposerActivity(id || null);" in src, "activity populates on edit, hides on create"
    assert "_frzComposerGroupDefaults(p, id);" in src, "group defaults are reset per open (the modal is reused)"


def test_stage4_server_untouched():
    src = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "composerMetaStrip" not in src and "_frzComposerActivity" not in src, "server.py must not be touched by Stage 4"


# ── Stage 4B: comments in the modal (separate, optional; reuses existing endpoints) ───────────────
# SOURCE-SHAPE guards over roadmap.html. The full read + write round-trip (mount the rich composer,
# post, re-render) was exercised live on the seeded server; the create/classic-hide and editor-can-
# comment paths were DOM-verified. These fail when Stage 4B is reverted. server.py is untouched.

def test_stage4b_comments_markup_edit_only():
    src = _html()
    assert '<div class="composer-comments" id="composerComments" style="display:none">' in src, \
        "the comments section exists and is hidden by default (edit-only, beta fills it)"
    assert 'id="composerCommentComposer"' in src and 'id="composerCommentsList"' in src, \
        "a composer host slot and a thread list"
    assert 'id="composerCommentsFull"' in src, "a full-page link to the item page"


def test_stage4b_reuses_existing_endpoints_no_new_endpoint():
    # INVARIANT (the spec's report-first): the modal reads and writes comments through the EXISTING
    # endpoints - GET /api/comments/{id} to read, and the host-mode rich composer (which POSTs to
    # /api/comments via _frzPostCommentNow) to write. No new endpoint, no new upload path.
    src = _html()
    m = re.search(r"function _frzRenderModalThread\(id\)\{.*?\n  \}", src, re.DOTALL)
    assert m, "the modal thread renderer not found"
    assert "API.get('/api/comments/'+id)" in m.group(0), "reads via the existing GET /api/comments/{id}"
    w = re.search(r"function _frzWireModalComposer\(p\)\{.*?\n  \}", src, re.DOTALL)
    assert w, "the modal composer wirer not found"
    body = w.group(0)
    assert "frzMountCommentEditor(p, { host:host, onPosted:" in body, "reuses the existing host-mode rich composer"
    assert "_frzPostCommentNow(host._frzPostCtx)" in body, "posts via the existing _frzPostCommentNow (POST /api/comments)"
    # server.py adds NO new comment route for this stage
    ssrc = SERVER.read_text(encoding="utf-8", errors="replace")
    assert ssrc.count('@app.post("/api/comments")') == 1 and ssrc.count('@app.get("/api/comments/{item_id}")') == 1, \
        "the existing comment endpoints are unchanged and none were added"


def test_stage4b_edit_only_beta_only_and_permission_gated():
    src = _html()
    init = re.search(r"function _frzInitModalComments\(id\)\{.*?\n  \}", src, re.DOTALL)
    assert init, "the modal comments init not found"
    assert "if(!root || id==null){" in init.group(0), "comments are edit-only (id != null) and beta-only (root)"
    w = re.search(r"function _frzWireModalComposer\(p\)\{.*?\n  \}", src, re.DOTALL)
    assert "canEdit=(_val('isAdmin',false)||_val('isEditor',false))" in w.group(0), \
        "the composer shows only for roles the endpoint already permits (admin/editor reach the modal)"


def test_stage4b_wired_into_beta_open_wrapper():
    src = _html()
    assert "try{ _frzInitModalComments(id); }catch(e){}" in src, \
        "the beta openProjectModal wrapper initialises the modal comments"


def test_stage4b_server_untouched():
    src = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "composerComments" not in src and "_frzInitModalComments" not in src, "server.py must not be touched by Stage 4B"


# ── Stage 5 (Addendum A1): make the status-driven Delay lock as VISIBLE as the Parallel one ────────
# The whole point of this stage is whether J.R. can SEE why a field is locked without hovering, so the
# accepted coverage is the two screenshots (Delay locked on an Approved item, unlocked on In Progress).
# These source-shape guards assert the rule exists and, critically, that NO enforcement changed.

def test_stage5_delay_lock_is_surfaced_visibly():
    src = _html()
    m = re.search(r"function updateDelayFieldsState\(\)\{.*?\n\}", src, re.DOTALL)
    assert m, "updateDelayFieldsState not found"
    body = m.group(0)
    # a lock badge on the label (mirrors the parallelResources 'Active' badge) + the reason inline
    assert "badge.id = 'delayActiveLock';" in body, "a lock badge is added to the Delay label when locked"
    assert "(!hasStart ? 'Needs start' : 'Active only')" in body, "the badge names the reason at a glance"
    assert "getElementById('delayLockMsg')" in body, "the existing reason is surfaced inline (not title-only)"
    assert '<div id="delayLockMsg" class="composer-lock-msg" style="display:none"></div>' in src, "the inline message element exists"
    # the badge is removed when unlocked (no false lock)
    assert "getElementById('delayActiveLock')?.remove();" in body, "the badge is cleared when the field is not locked"


def test_stage5_no_enforcement_change():
    # INVARIANT: the disable rule is UNCHANGED (canDelay = hasStart && isActive; revSel.disabled =
    # !canDelay), config-driven via isActiveStatus (no hardcoded status name), the existing message copy
    # is unchanged, and the parallelResources lock (client + its 'Active' badge) is left intact.
    src = _html()
    body = re.search(r"function updateDelayFieldsState\(\)\{.*?\n\}", src, re.DOTALL).group(0)
    assert "const canDelay   = hasStart && isActive;" in body and "revSel.disabled = !canDelay;" in body, \
        "the Delay disable logic is unchanged (nothing newly locked or permitted)"
    assert "isActiveStatus(currStatus)" in body, "active-ness is resolved through config, never a hardcoded status name"
    assert "'Delay date is only available on Active items'" in body and "'Set a Start Date before adding a Delay'" in body, \
        "the existing lock messages are surfaced, not rewritten"
    # parallelResources keeps its own (pre-existing) client lock + badge - untouched by this stage
    assert "prInp.title = prIsActive ? 'Cannot be changed while item is active' : '';" in src, "the parallelResources lock is intact"
    assert "lockSpan.id = 'prActiveLock';" in src, "the parallelResources 'Active' badge is intact"


def test_stage5_server_untouched():
    src = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "delayActiveLock" not in src and "delayLockMsg" not in src, "server.py must not be touched by Stage 5"


# ── 6.39.6: close A1 parity - Parallel Resources gets the inline message when IT is locked (active) ──
def test_6396_parallel_inline_message_on_lock():
    src = _html()
    # the element exists, reuses the shared lock-message class, and is named so it does not read as the
    # fParallel dependency select
    assert '<div id="fParallelResourcesInline" class="composer-lock-msg" style="display:none"></div>' in src, \
        "Parallel Resources has an inline lock message reusing .composer-lock-msg, id fParallelResourcesInline"
    assert 'id="fParallelInline"' not in src, "the id must be fParallelResourcesInline, not fParallelInline"
    # shown ONLY when the field is locked (saved status active) - the inverse of Delay - and hidden
    # otherwise; same prIsActive source as the existing badge; no enforcement change.
    m = re.search(r"const prMsg = document\.getElementById\('fParallelResourcesInline'\);.*?\n  \}", src, re.DOTALL)
    assert m, "the parallel inline-message wiring not found"
    body = m.group(0)
    assert "if(prIsActive){ prMsg.textContent = 'Cannot be changed while item is active';" in body, \
        "shown with the existing copy when the saved status is active (the lock condition)"
    assert "prMsg.style.display = 'none';" in body, "hidden when not locked (editable)"
    # the client lock itself is unchanged (still disabled when active); server untouched
    assert "prInp.disabled = prIsActive;" in src, "the existing parallelResources client lock is unchanged"
    ssrc = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "fParallelResourcesInline" not in ssrc, "server.py must not be touched by 6.39.6"


# ── 6.39.7: remove the composer status "not in project workflow" annotation (match the item page) ────
def test_6397_status_annotation_removed_but_coercion_preserved():
    # The composer's status control no longer annotates an off-Space-workflow status - it shows the plain
    # value, matching the item page. But the branch that keeps that status SELECTED (so save does not
    # coerce it to a workflow value) is load-bearing and must remain. Guard fails on revert (the revert
    # brings the annotation string back).
    src = _html()
    assert "not in project workflow" not in src, "the off-workflow status annotation must be gone (composer matches the item page)"
    m = re.search(r"function refreshStatusOptions\(currentStatusValue\)\{.*?\n\}", src, re.DOTALL)
    assert m, "refreshStatusOptions not found"
    body = m.group(0)
    # the off-workflow status is still added as a PLAIN option and selected (coercion-prevention kept)
    assert "opt.value = prev; opt.textContent = prev;" in body, "the current status is shown as a plain value"
    assert "fSt.insertAdjacentElement('afterbegin', opt);" in body and "fSt.value = prev;" in body, \
        "the off-workflow status stays selectable + selected so save does not coerce it"
    assert "var(--accent2)" not in body, "the red warning colour on the status option is gone"
    # (6.39.7 built the list from the per-Space workflow; 6.39.9 moved the source to the Org `statuses`.
    # The coercion-prevention branch above is source-agnostic and still applies - see
    # test_6399_status_options_from_org_statuses_not_perspace for the current source assertion.)


def test_6397_server_untouched():
    src = SERVER.read_text(encoding="utf-8", errors="replace")
    assert "not in project workflow" not in src, "server.py has no part in this and must be untouched"


# ── 6.39.8: item-page links no longer hidden by a stale _itemPageId (shell nav-away) ─────────────────
def test_6398_onitempage_requires_visible_overlay():
    # onItemPage must also require the item-page overlay to be visible, so a stale _itemPageId (the shell
    # hides the overlay without closeItemPage) does not hide the open-item-page links in the modal.
    # Guard fails on revert (the overlay-visibility clause is removed).
    src = _html()
    m = re.search(r"const onItemPage = !!\(id && _itemPageId === id.*?\);", src, re.DOTALL)
    assert m, "the onItemPage computation was not found (or lost the overlay guard)"
    assert "getElementById('itemPageOverlay')" in m.group(0) and "style.display !== 'none'" in m.group(0), \
        "onItemPage must require the item-page overlay to be actually visible"


# ── 6.39.9: composer status options are the intersection of the Space list and the Org statuses ─────
def test_6399_status_options_are_space_intersect_org():
    # refreshStatusOptions builds #fStatus from the INTERSECTION of the Space's workflow list
    # (getStatusesForProduct) and the Organization's `statuses` - preserving per-Space scoping while
    # making a stale per-Space entry (a name the Org no longer has) unofferable. Guard fails on revert.
    src = _html()
    m = re.search(r"function refreshStatusOptions\(currentStatusValue\)\{.*?\n\}", src, re.DOTALL)
    assert m, "refreshStatusOptions not found"
    body = m.group(0)
    assert "getStatusesForProduct(productName).filter(s => statuses.includes(s))" in body, \
        "the option list is the Space workflow list intersected with the Org `statuses`"
    # the item page control (the reference) lists the full Org `statuses`
    assert "if(field==='status'){" in src and "return statuses.map(s=>o(s,s));" in src, \
        "the item page status control lists the Org statuses (the full set)"


# ── 6.39.9 Fix A: projects-only /api/all refreshes are refused across a team change ─────────────────
# The composer status divergence root cause: a projects-only refresh (projects := fresh, config left
# stale) run AFTER the effective team (shared localStorage token) changed imports the other Org's
# projects onto this tab's stale statuses/products. The app treats a team change as a full reload, so
# every projects-only refresh must reload instead of partial-updating when the team no longer matches
# the one this tab booted on. These are SOURCE-SHAPE guards; they fail on revert.
def test_6399a_team_change_helper_exists():
    src = _html()
    m = re.search(r"function _frzTeamChangedSinceBoot\(\)\{.*?\}", src, re.DOTALL)
    assert m, "_frzTeamChangedSinceBoot helper not found"
    body = m.group(0)
    # compares the effective team to the team booted on; empty _BOOT_TEAM is treated as unchanged
    assert "_BOOT_TEAM" in body and "activeTeamSlug()" in body and "!==" in body, \
        "helper must compare activeTeamSlug() to _BOOT_TEAM"


def test_6399a_reload_all_data_guarded():
    # _frzReloadAllData (openItem's miss branch, the reproduced path) reloads instead of importing
    # another Org's projects when the team changed.
    src = _html()
    m = re.search(r"async function _frzReloadAllData\(\)\{.*?\n\}", src, re.DOTALL)
    assert m, "_frzReloadAllData not found"
    body = m.group(0)
    guard = body.index("_frzTeamChangedSinceBoot()")
    assign = body.index("projects = data.projects")
    assert guard < assign, "the team-change guard must precede the projects-only assignment"
    assert "location.reload()" in body[:assign], "guard must force a full reload"


def test_6399a_classic_projects_only_refreshes_guarded():
    # The classic scenario-commit and save-conflict refreshes (projects := fresh.projects) are guarded
    # the same way. Count the guarded projects-only refreshes to catch a silent regression.
    src = _html()
    # every projects-only refresh of this exact shape must sit immediately after a team-change guard
    refreshes = re.findall(
        r"(if\(_frzTeamChangedSinceBoot\(\)\)\{ location\.reload\(\); return;? \}\s*\n\s*)?"
        r"try \{ (?:var|const) fresh = await API\.get\('/api/all'\); if\(fresh && fresh\.projects\) projects = fresh\.projects; \}",
        src,
    )
    assert len(refreshes) >= 2, "expected the two classic projects-only refreshes (scenario commit + save conflict)"
    assert all(refreshes), "every classic projects-only refresh must be preceded by the team-change guard"


def test_6399a_jira_and_planning_refreshes_guarded_but_timer_excluded():
    # 6.39.9 extended the guard to 4 more projects-only refreshes (release FF reload, planning server
    # commit, manual Jira pull-all, item-page Jira sync) but DELIBERATELY leaves the background Jira
    # sync timer (runBackgroundJiraSync) unguarded - a timer tick must not trigger a surprise reload;
    # the intersection hardening backstops the composer there.
    src = _html()
    # the four guarded functions each contain the guard
    for fn in ("runManualPullSyncAll", "syncJiraFromItemPage"):
        m = re.search(r"async function " + fn + r"\(.*?\)\{.*?\n\}", src, re.DOTALL)
        assert m, fn + " not found"
        assert "_frzTeamChangedSinceBoot()" in m.group(0), fn + " must carry the team-change guard"
    # background timer stays unguarded but still does a projects-only import
    mb = re.search(r"async function runBackgroundJiraSync\(\)\{.*?\n\}", src, re.DOTALL)
    assert mb, "runBackgroundJiraSync not found"
    assert "_frzTeamChangedSinceBoot()" not in mb.group(0), \
        "the background Jira sync timer must NOT trigger a reload on a tick"
    assert "projects = data.projects || projects" in mb.group(0), \
        "the background timer still does its projects-only import (backstopped by the intersection fix)"
    # total guard call sites across the file (3 core + 4 extended = 7)
    assert src.count("if(_frzTeamChangedSinceBoot()){ location.reload()") == 7, \
        "expected 7 team-change reload guards (3 core + 4 extended; timer excluded)"
