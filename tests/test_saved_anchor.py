"""SAVED-ANCHOR-2 (SAVED-ANCHOR-1 Part C) regression guard: prune both sides before comparing identity.

A saved filter stores chip values by name. When a value is later removed from config (IT's status
"Planned"), applyPanel silently drops it on apply (correct), so the live URL is pruned - but the matcher
(_frzSavedStillMatches) compared the pruned live set against the UNPRUNED saved record, so it could never
equal, and _frzActiveSaved then nulled the correctly-set anchor id: the filter became permanently
un-anchorable. The fix prunes the SAVED side to the same source applyPanel prunes against (the chip
panel's checkboxes, via CHIPS[param].panel) before comparing, for every chip param.

These are closure-scoped frontend functions with no JS runtime in this harness (no node on PATH, `state`
not console-readable), so this is a SOURCE-SHAPE guard - it asserts the prune is wired and reads the same
panel source. The DECISION is demonstrated separately (a Python reimplementation reproduced IT's fault and
the fix + the case-4 no-loosening guard), and the behavioural cases live in tests/saved_paint_checks.js.
Each assertion below FAILS when SAVED-ANCHOR-2 is reverted (demonstrated at build vs HEAD = 6.36.0).
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src() -> str:
    return ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")


def test_prune_helpers_exist_and_read_the_panel_source():
    src = _src()
    m = re.search(r"function _frzChipPanelValues\(param\)\{(.*?)\n  \}", src, re.DOTALL)
    assert m, "_frzChipPanelValues() is missing"
    body = m.group(1)
    # must draw valid values from the SAME source applyPanel uses: CHIPS[param].panel's checkboxes
    assert "CHIPS[param]" in body and ".panel" in body, "prune must read CHIPS[param].panel (applyPanel's source)"
    assert "input[type=checkbox]" in body, "prune must read the panel's checkbox options, as applyPanel does"
    assert "function _frzPruneChipVal(param" in src, "_frzPruneChipVal() is missing"


def test_matcher_prunes_the_saved_side():
    # _frzSavedStillMatches must prune the saved value via _frzPruneChipVal before the normalized compare.
    m = re.search(r"function _frzSavedStillMatches\(s\)\{(.*?)\n  \}", _src(), re.DOTALL)
    assert m, "_frzSavedStillMatches() not found"
    body = m.group(1)
    assert "_frzPruneChipVal(" in body, "the matcher must prune the saved side (the SAVED-ANCHOR-1 fault)"
    assert "_frzNormMulti(" in body, "order-insensitive multi-value matching (6.35.1) must be preserved"


def test_seed_identity_uses_pruned_value():
    # _frzSavedHasIdentity must count identity by pruned value, so a values-all-removed filter doesn't
    # auto-seed as "matches everything".
    m = re.search(r"function _frzSavedHasIdentity\(s\)\{(.*?)\n  \}", _src(), re.DOTALL)
    assert m, "_frzSavedHasIdentity() not found"
    assert "_frzPruneChipVal(" in m.group(1), "the seed-identity check must prune too (no auto-seed of an empty filter)"


def test_ignore_set_unchanged():
    # from/to/board/sprint must remain ignored by the matcher; project stays separately handled.
    m = re.search(r"function _frzSavedStillMatches\(s\)\{(.*?)\n  \}", _src(), re.DOTALL)
    body = m.group(1)
    assert "IGNORE={ from:1, to:1, board:1, sprint:1 }" in body, "the IGNORE set must be unchanged"
    assert "sp.has('project')" in body, "project keeps its existing separate handling"
