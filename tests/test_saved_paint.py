"""SAVED-PAINT-1 (SHELL-DEBT-1 Part 1) regression guard: the single saved-state paint entry point.

_frzPaintSavedState() reads saved-filter state once and paints every standing region (R1 bar chip,
R2 rail fdot/aria, R3 rail star, R4 crumb), replacing per-caller paint calls. These are closure-scoped
frontend functions and there is no JS runtime in this harness (no node on PATH, no jsdom; `state` is not
console-readable), so this is a SOURCE-SHAPE guard - it asserts the refactor's structure is present, which
a straight revert removes. The behavioural 2x2 matrix (flag-off/on x expanded/collapsed) and the hydration
case live in tests/saved_paint_checks.js for Claude's post-deploy pass; the hydration case needs a no-default
account (a known blocker). Each assertion below FAILS when SAVED-PAINT-1 is reverted (demonstrated at build
time against HEAD = 6.35.6 pre-refactor).
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _src() -> str:
    return ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")


def _paint_body() -> str:
    # body = everything between `function _frzPaintSavedState(){` and the next `function _frzRefreshActiveSaved(){`
    m = re.search(r"function _frzPaintSavedState\(\)\{(.*?)\n  function _frzRefreshActiveSaved\(\)\{", _src(), re.DOTALL)
    assert m, "could not locate _frzPaintSavedState() (must sit immediately before _frzRefreshActiveSaved)"
    return m.group(1)


def test_entry_point_exists_and_reads_both_inputs_once():
    body = _paint_body()
    assert body.count("_frzActiveSavedShown()") == 1, "must read `shown` (_frzActiveSavedShown) exactly once"
    assert body.count("_defaultFilter()") == 1, "must read `def` (_defaultFilter) exactly once"


def test_entry_point_writes_no_state():
    # anchor mutation stays in _frzActiveSaved / _frzSeedActiveFromURL; the entry point READS state only.
    body = _paint_body()
    writes = re.findall(r"state\.\w+\s*=(?!=)", body)   # assignment, not == / === / != / !==
    assert not writes, f"_frzPaintSavedState must not write state; found {writes}"


def test_r1_has_both_cases():
    body = _paint_body()
    assert "outerHTML=_savedChipHTML()" in body.replace(" ", ""), "R1 content-update (surgical swap) missing"
    assert "renderBar()" in body, "R1 presence-transition branch (renderBar) missing"
    assert "_frzRailCollapsed()" in body and "state.saved" in body, "R1 presence test must read _railCollapsed and saved.length"


def test_all_six_call_sites_present():
    src = _src()
    # 6 calls: definition + _frzRefreshActiveSaved + applySaved + saveCurrent + rename + delete + hydration
    assert src.count("_frzPaintSavedState()") >= 7, (
        "expected the definition plus 6 call sites (_frzRefreshActiveSaved, applySaved, saveCurrent, rename, "
        f"delete, _frzSyncSavedFromServer); found {src.count('_frzPaintSavedState()')} occurrences"
    )
    # hydration call must be inside _frzSyncSavedFromServer, after renderSavedNav
    m = re.search(r"function _frzSyncSavedFromServer\(\)\{.*?renderSavedNav\(\);\s*\n\s*_frzPaintSavedState\(\);", _src(), re.DOTALL)
    assert m, "hydration path must call _frzPaintSavedState() right after renderSavedNav() (1.4 ordering)"


def test_dead_boardnav_block_removed_from_refresh():
    # Part 4: the dead #frzBoardNav block inside _frzRefreshActiveSaved is removed; the renderRail one stays.
    m = re.search(r"function _frzRefreshActiveSaved\(\)\{(.*?)\n  \}", _src(), re.DOTALL)
    assert m, "could not locate _frzRefreshActiveSaved()"
    # target the dead-CODE signature, not the word (the Part-4 comment legitimately names #frzBoardNav)
    assert "getElementById('frzBoardNav')" not in m.group(1), "the dead #frzBoardNav block must be removed from _frzRefreshActiveSaved"


def test_rail_star_has_hook_class():
    # renderSavedNav's default star carries .frz-saved-star so the entry point can sync it surgically.
    assert 'class="frz-saved-star"' in _src(), "renderSavedNav default star must carry .frz-saved-star"
