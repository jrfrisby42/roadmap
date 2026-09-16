"""FIELDS-CARRY-2 regression guard: the FIELDS-2 chips are carried across views.

The three FIELDS-2 filter params (location/resolutionType/blockedReason) were in neither syncURL's
cross-view carry set nor _selfSer, so a cross-view round trip DELETED them (case a) - destroying the
value and dropping any saved filter's anchor. Fix (approved option 1): add all three to BOTH the carry
set AND _selfSer-where-offered, mirroring priority/dept exactly. Carry-without-_selfSer would resurrect a
just-cleared chip (the original warning); both-sets is the safe change.

SOURCE-SHAPE guards: they prove both sets carry the three and the misleading comment was rewritten. They
do NOT prove the value survives a live round trip or that a cleared chip stays cleared - those are
tests/fields_carry_checks.js + the live pass. Each fails when the fix is reverted (the three are absent
from both sets at HEAD). server.py is untouched by this stage.
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src() -> str:
    return ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")


def test_carry_set_includes_fields2():
    # the cross-view carry set (the _frzCarryQS set in syncURL) must list all three
    src = _src()
    m = re.search(r"\[('owner','type','status','assignee','sprint','priority','dept'[^\]]*)\]\.forEach\(function\(pk\)", src)
    assert m, "the cross-view carry set was not found"
    body = m.group(1)
    for p in ("location", "resolutionType", "blockedReason"):
        assert f"'{p}'" in body, f"the carry set must include {p}"


def test_frzcarryqs_keys_include_fields2():
    # _frzCarryQS is the cross-view carry path a view-tab / rail click uses; its key list must carry the three
    # too, else the param is dropped before navigation and syncURL never sees it (the 6.37.4 miss).
    src = _src()
    # ITEM-VIEWNAV-1 extracted the carry key list into the shared constant _FRZ_CARRY_KEYS (one list, two
    # readers: _frzCarryQS reads the URL, _frzCarryQSFromState reads state). The invariant is unchanged - the
    # carry set must include the three FIELDS-2 params - it just lives in the constant now.
    m = re.search(r"var _FRZ_CARRY_KEYS = \[('project','owner'[^\]]*)\];", src)
    assert m, "_FRZ_CARRY_KEYS constant was not found"
    body = m.group(1)
    for p in ("location", "resolutionType", "blockedReason"):
        assert f"'{p}'" in body, f"_FRZ_CARRY_KEYS must include {p} (the tab-click carry path)"
    assert "var keys = includeChips ? _FRZ_CARRY_KEYS : ['project'];" in src, "_frzCarryQS must read the shared constant"


def test_view_expresses_saved_includes_fields2():
    # _frzViewExpressesSaved is the anchor DISPLAY gate: a saved filter shows active only when the view
    # "expresses" every identifying param. Its appliedP set mirrors priority/dept and must include the three,
    # else a FIELDS-2 saved filter rests (no anchor) even on the List that offers it.
    src = _src()
    m = re.search(r"if\(CFG\[view\]\)\{ \[('priority','dept'[^\]]*)\]\.forEach\(function\(x\)\{ if\(\(CFG\[view\]\.chips", src)
    assert m, "_frzViewExpressesSaved appliedP list was not found"
    body = m.group(1)
    for p in ("location", "resolutionType", "blockedReason"):
        assert f"'{p}'" in body, f"_frzViewExpressesSaved must express {p} (else the anchor rests on the List)"


def test_selfser_includes_fields2():
    # the _selfSer-where-offered list must list all three, alongside priority/dept
    src = _src()
    m = re.search(r"\[('priority','dept'[^\]]*)\]\.forEach\(function\(x\)\{ if\(CFG\[view\]\.chips", src)
    assert m, "the _selfSer-where-offered list was not found"
    body = m.group(1)
    for p in ("location", "resolutionType", "blockedReason"):
        assert f"'{p}'" in body, f"_selfSer must include {p} (else carry resurrects a cleared chip)"


def test_both_sets_paired_not_carry_only():
    # the naive half-fix (carry set only) is the resurrection bug - both must be present together
    src = _src()
    # match the three in each set without assuming blockedReason is the LAST element (REPORTER-TEXT-1 appended reporter)
    carry = bool(re.search(r"'location','resolutionType','blockedReason'[^\]]*\]\.forEach\(function\(pk\)", src))
    selfser = bool(re.search(r"'location','resolutionType','blockedReason'[^\]]*\]\.forEach\(function\(x\)", src))
    assert carry and selfser, "both the carry set AND _selfSer must carry the three (paired, not carry-only)"


def test_comment_rewritten_not_the_old_prohibition():
    # the old comment said these are NOT carried and "Do not paper over it" - that would now be misleading
    src = _src()
    assert "are NOT carried - they are non-auto-revealing" not in src, "the stale KNOWN-GAP prohibition must be gone"
    assert "Do not paper over" not in src, "the stale 'do not add them' warning must be gone"
    # the new comment must document the two-sets-in-sync rule (substrings, since the comment wraps lines)
    assert "TWO SETS MUST STAY" in src, "the comment must state the two sets stay in sync"
    assert "MUST be added to the other" in src, "the comment must require a future chip be added to both sets"
    assert "carried AND self-serialized" in src, "the comment must state the three are now carried + self-serialized"


def test_matcher_and_specifiedness_unchanged():
    # SAVED-ANCHOR-2's matcher is generic over qs params and must NOT be touched
    src = _src()
    assert "function _frzSavedStillMatches(s){" in src, "the matcher must still exist unchanged"
    # _frzHasFilterParams derives its allow-set from CHIPS (which already includes the three) - unchanged
    assert "Object.keys(CHIPS).forEach(function(k){ allow[CHIPS[k].param]=1; });" in src, \
        "_frzHasFilterParams must remain CHIPS-derived (the three already count, no change needed)"
