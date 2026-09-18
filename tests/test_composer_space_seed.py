"""COMPOSER-REFINE-1 Stage 3 (Space at create) regression guards.

The composer no longer prefills the first Space in the list on create. It seeds Space from the current
rail scope (via the beta bridge window._frzListScope) only when that scope pins a specific, known Space;
an unscoped user gets an EMPTY, required Space. While Space is empty the Status select is inert and Create
is blocked, each with a VISIBLE reason (the Status-row note explains, the disabled button states). Changing
Space to one that does not allow the current status resets to that Space's default and says so inline -
CREATE MODE ONLY, so edit mode's off-workflow retention is left untouched.

These are closure-scoped frontend functions with no JS runtime in the pytest layer, so this is a
SOURCE-SHAPE guard: it asserts the wiring exists at the right sites. Behavioural cases live in the local
harness rendered pass. Each assertion FAILS when Stage 3 is reverted.
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src() -> str:
    return ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")


def _open_modal_seed() -> str:
    # openProjectModal's Space-seeding region (through the _frzApplySpaceGate call on open).
    m = re.search(r"COMPOSER-REFINE-1 Stage 3: seed Space from the rail scope(.*?)_frzApplySpaceGate\(\);",
                  _src(), re.DOTALL)
    assert m, "openProjectModal Stage 3 seeding block not found"
    return m.group(1)


def _gate_body() -> str:
    m = re.search(r"function _frzApplySpaceGate\(\)\{(.*?)\n\}", _src(), re.DOTALL)
    assert m, "_frzApplySpaceGate() not found"
    return m.group(1)


def _change_body() -> str:
    m = re.search(r"function _frzOnComposerSpaceChange\(\)\{(.*?)\n\}", _src(), re.DOTALL)
    assert m, "_frzOnComposerSpaceChange() not found"
    return m.group(1)


# ── 3.1 seeding - scope, not first-in-list ───────────────────────────────────────────────────────────
def test_create_seeds_from_scope_via_bridge():
    body = _open_modal_seed()
    assert "window._frzListScope" in body, "create must read the rail scope via the beta bridge window._frzListScope"
    assert "'__all__'" in body, "the unscoped sentinel __all__ must be treated as absent (empty Space)"
    # membership check against known products, both object and string product shapes
    assert "products.some(" in body, "the seeded scope must be validated against known products before use"


def test_first_in_list_default_is_gone():
    # the exact old default was `products[0]?.name || products[0]` - it must no longer prefill create.
    src = _src()
    assert "products.length ? (products[0]?.name || products[0]" not in src, (
        "the old first-in-list Space default must be removed - it stamped a permanent wrong key prefix ~9% "
        "of the time on development"
    )


# ── 3.2 empty-Space gate - inert Status, blocked Create, VISIBLE reason ───────────────────────────────
def test_empty_gate_blocks_status_and_create_create_only():
    body = _gate_body()
    assert "!editingId" in body, "the gate must apply to CREATE mode only (edit always has a Space)"
    assert "fSt.disabled = empty" in body, "the Status select must be inert while Space is empty"
    assert "saveBtn.disabled = empty" in body, "Create must be blocked while Space is empty"


def test_empty_gate_has_a_visible_rendered_reason():
    src = _src()
    # a rendered element, not a title attribute / opacity alone (the Delay-Date-dropdown lesson)
    assert 'id="composerSpaceGate"' in src, "the empty-Space reason must be a rendered element"
    assert "Choose a Space to pick a status." in src, "the exact Status-row note copy must be present"
    assert "gate.style.display = empty" in _gate_body(), "the note must show exactly while Space is empty"


def test_no_second_create_button_note():
    # J.R. decision: ONE note only. The disabled button states; a second note beside it reads as nagging.
    src = _src()
    assert "Choose a Space before creating" not in src, "the create-button note was dropped - one note only"


# ── 3.3 Space change invalidating status - reset + inline note, create only ───────────────────────────
def test_space_change_resets_incompatible_status_create_only():
    body = _change_body()
    assert "!editingId" in body, "the status reset must be CREATE MODE ONLY (edit keeps off-workflow retention)"
    assert "getStatusesForProduct(newSpace).filter(s => statuses.includes(s))" in body, \
        "the reset must test against the Org-intersected Space status list (same intersection as refreshStatusOptions)"
    assert "getDefaultStatus()" in body, "the reset target must resolve through the default-status helper, never hardcoded"
    assert "composerStatusResetNote" in body, "a Space change that resets the status must say so inline"


def test_reset_note_names_both_statuses_and_the_space():
    body = _change_body()
    assert "'Status reset to '" in body and "isn't used in" in body, \
        "the reset note must name the new default, the dropped status, and the Space"


def test_change_listener_routes_through_the_handler():
    src = _src()
    assert "addEventListener('change', ()=>{ _frzOnComposerSpaceChange(); updateJiraFieldVisibility(); })" in src, \
        "the #fProduct change listener must route through _frzOnComposerSpaceChange (reset + gate), not bare refreshStatusOptions"


# ── save-handler belt-and-suspenders guard, create only ───────────────────────────────────────────────
def test_save_handler_requires_space_on_create_only():
    src = _src()
    assert "if(!editingId && !data.product){ _frzRevealComposerField('fProduct'); showToast('Choose a Space first'" in src, (
        "the save handler must block a create submit with no Space (belt-and-suspenders) and focus the field, "
        "while leaving edit of the legacy no-Space item saveable"
    )


# ── invariant: edit-mode off-workflow retention is untouched ──────────────────────────────────────────
def test_edit_mode_offworkflow_retention_preserved():
    # refreshStatusOptions still inserts the current value as a plain option when it is not in the Space's
    # list - the load-bearing branch the Stage-3 reset deliberately does NOT touch in edit mode.
    m = re.search(r"function refreshStatusOptions\(currentStatusValue\)\{(.*?)\n\}", _src(), re.DOTALL)
    assert m, "refreshStatusOptions() not found"
    assert "insertAdjacentElement('afterbegin', opt)" in m.group(1), \
        "edit-mode off-workflow status retention must remain in refreshStatusOptions"
