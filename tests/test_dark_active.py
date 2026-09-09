"""DARK-ACTIVE-1 regression guard: active-state fills reach dark mode.

Root cause (not the prompt's hypothesis): the shell's active-state fills ARE themed in dark
(--frz-accent-soft = #15314a), but a legacy global rule
`body.dark-mode button:not(...) { background:var(--surface2)!important; color:var(--text)!important }`
clobbered every Flow chrome BUTTON not in its exclusion chain, forcing the active tab/chip/rail/scope
to the surface colour - indistinguishable from inactive. Fix (approach A): add the five Flow chrome
button families to that rule's :not() exclusion chain (the same pattern already applied to .frz-newitem
etc.), so their themed rules and the scope chip's inline Space-colour color-mix apply in dark. Light is
untouched (the rule is dark-only).

SOURCE-SHAPE guards: they prove the exclusions exist and the themed precondition holds. They do NOT prove
active differs measurably from inactive on a cold load - that is tests/dark_active_checks.js + the live
pass. Each fails when the fix is reverted (demonstrated vs HEAD). server.py is untouched by this stage.
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src() -> str:
    return ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")


def test_clobber_excludes_flow_chrome_buttons():
    src = _src()
    # the single legacy clobber selector must now exclude the five Flow chrome button families
    m = re.search(r"body\.dark-mode button:not\([^{]*?\)\s*\{", src, re.DOTALL)
    assert m, "the legacy dark-mode button clobber selector was not found"
    sel = m.group(0)
    for cls in ("frz-tab", "frz-chip", "frz-item", "frz-set-tab", "frz-pop-opt"):
        assert f":not(.{cls})" in sel, f"the clobber must exclude .{cls}"
    # the daytoggle buttons are bare children -> a complex :not on the container, not a class exclusion
    assert ":not(.frz-cal-daytoggle button)" in sel, "the clobber must exclude the bare daytoggle buttons via a complex :not"
    # the theme-seg is intentionally NOT excluded (already differentiated by rule 20165)
    assert ":not(.frz-theme-seg" not in sel, "the theme-seg was intentionally left out (differentiated via 20165)"
    # it must still be the same dark-only, !important rule (we excluded classes, not defanged the rule)
    assert "background: var(--surface2) !important" in src, "the clobber body must be unchanged for non-Flow buttons"


def test_dark_accent_soft_is_themed_precondition():
    # the fix relies on --frz-accent-soft already being themed dark; if this regresses, the exclusion
    # would expose an unthemed token instead of the intended fill
    src = _src()
    m = re.search(r"body\.dark-mode \.frz-beta \{[^}]*--frz-accent-soft:\s*#15314a", src, re.DOTALL)
    assert m, "--frz-accent-soft must be themed #15314a in body.dark-mode .frz-beta"
    m2 = re.search(r"body\.dark-mode \.frz-beta \{[^}]*--frz-accent:\s*#4A97E5", src, re.DOTALL)
    assert m2, "--frz-accent must be #4A97E5 (the working dark accent) in dark"


def test_active_rules_still_use_accent_soft():
    # the active-state rules are untouched (the fix is in the clobber, not these)
    src = _src()
    assert ".frz-beta .frz-tab.is-active { color:var(--frz-accent); background:var(--frz-accent-soft); }" in src, \
        "the tab active rule must remain accent-soft/accent"
    assert ".frz-beta .frz-chip.is-active { background:var(--frz-accent-soft); border-color:var(--frz-accent-soft-border); }" in src, \
        "the chip active rule must remain accent-soft"


def test_light_reference_unchanged():
    # light tokens must be exactly as before (light is the reference, must not regress)
    src = _src()
    assert "--frz-accent:#0059A9; --frz-accent-hover:#004A8F; --frz-accent-soft:#E7F1FA;" in src, \
        "light --frz-accent / --frz-accent-soft must be unchanged"
