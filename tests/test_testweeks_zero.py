"""TESTWEEKS-ZERO-1: absent `testWeeks` derives as 0 (no fabricated 2-week test phase), aligning the
Gantt/capacity with the Calendar (CAL-DATES-1 already treats absent as 0).

Client-only, derivation sites only. Source-assertions against roadmap.html: the derivation sites now
use `parseFloat(p.testWeeks) || 0`; the FORM default and the SAVE path are deliberately unchanged.
"""
import os

_HTML = None


def _html():
    global _HTML
    if _HTML is None:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "roadmap.html")
        with open(p, encoding="utf-8") as f:
            _HTML = f.read()
    return _HTML


# ── derivation sites changed to absent -> 0 ──────────────────────────────────────
def test_capacity_map_absent_is_zero():
    h = _html()
    assert "const testWks   = parseFloat(p.testWeeks) || 0;" in h        # buildCapacityUsageMap
    assert "const testWks = parseFloat(p.testWeeks) || 0;" in h          # itemCapacityOnDay


def test_gantt_render_absent_is_zero():
    h = _html()
    assert "const testWeeks = parseFloat(p.testWeeks) || 0;" in h        # Gantt row + buildTooltipHTML


def test_schedule_helpers_absent_is_zero():
    h = _html()
    assert "const tw = parseFloat(p.testWeeks) || 0;" in h               # parentWorkEnd + the dead-alias site
    assert "const ccTestWks = parseFloat(p.testWeeks) || 0;" in h        # Code Complete badge
    assert "const ipTestWks = parseFloat(p.testWeeks) || 0;" in h        # item page code-complete


def test_kanban_duration_absent_adds_zero():
    assert "(p.dueWeeks||0)+(parseFloat(p.testWeeks)||0)}wk" in _html()


def test_every_changed_site_is_commented():
    # the prompt requires a comment at each changed site (a bare 0 reads as a bug otherwise)
    assert _html().count("TESTWEEKS-ZERO-1") >= 8


# ── invariants: no derivation default remains; form + save unchanged ─────────────
def test_no_derivation_default_remains():
    h = _html()
    # No derivation site keeps `p.testWeeks ?? 2`; the only surviving `?? 2` is the composer form
    # pre-fill, which reads `p?.testWeeks` (optional chaining). And zero `testWeeks||2`.
    assert h.count("p.testWeeks ?? 2") == 0
    assert h.count("p?.testWeeks ?? 2") == 1
    assert h.count("testWeeks||2") == 0 and h.count("testWeeks || 2") == 0


def test_form_default_still_suggests_two():   # INVARIANT
    assert "document.getElementById('fTestWeeks').value = String(p?.testWeeks ?? 2);" in _html()


def test_new_item_form_reset_still_two():      # INVARIANT
    assert "document.getElementById('fTestWeeks').value = '2';" in _html()


def test_save_path_unchanged():                # INVARIANT (reported, not fixed)
    assert ("testWeeks:  isEditor ? (existingProject?.testWeeks  ?? "
            "parseFloat(document.getElementById('fTestWeeks').value))") in _html()


def test_calendar_already_absent_zero_untouched():  # INVARIANT (Proof 7)
    # CAL-DATES-1's badge arithmetic already treats absent as 0 and must not move
    assert "var tw=parseFloat(p.testWeeks) || 0;" in _html()
