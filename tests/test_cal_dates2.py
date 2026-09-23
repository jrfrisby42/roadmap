"""CAL-DATES-2: Calendar milestone badges take the Gantt colour vocabulary, treat overdue distinctly,
and gain two URL-backed layer toggles (Availability / Milestones).

Client-only change (roadmap.html). Source-assertions use the string/comment-aware _fn_body walker
shared with the other cal-dates / flag-blocked tests.
"""
import os

BS = chr(92)
_HTML = None


def _html():
    global _HTML
    if _HTML is None:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "roadmap.html")
        with open(p, encoding="utf-8") as f:
            _HTML = f.read()
    return _HTML


def _fn_body(name):
    h = _html(); i = h.find("function " + name); assert i >= 0, name + " not found"
    b = h.find("{", i); depth, j, instr = 0, b, None
    while j < len(h):
        ch = h[j]
        if instr:
            if ch == BS:
                j += 2; continue
            if ch == instr:
                instr = None
            j += 1; continue
        if ch in ("'", '"', "`"):
            instr = ch; j += 1; continue
        if ch == "/" and j + 1 < len(h) and h[j+1] == "/":
            k = h.find("\n", j); j = k if k >= 0 else len(h); continue
        if ch == "/" and j + 1 < len(h) and h[j+1] == "*":
            k = h.find("*/", j + 2); j = (k + 2) if k >= 0 else len(h); continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return h[i:j + 1]
        j += 1
    raise AssertionError(name + " unclosed")


# ── Part 1: match the Gantt palette, read the token, keep the outline ────────────
def test_palette_reads_gantt_green_token():
    h = _html()
    # handoff reads the EXISTING Gantt green token (not a re-declared literal)
    assert "--frz-mile-ho: var(--bar-green)" in h
    # due mirrors the test-phase blue literal into a token (no Gantt token exists for it)
    assert "--frz-mile-due: #0090d4" in h
    # overdue = the Gantt delay maroon
    assert "--frz-mile-over: #8a3040" in h


def test_badges_use_tokens_not_amber():
    h = _html()
    assert "color:var(--frz-mile-ho)" in h and "border-color:var(--frz-mile-ho)" in h
    assert "color:var(--frz-mile-due)" in h
    # the CAL-DATES-1 amber is fully gone
    for amber in ("#8a5a00", "#c8860a", "#e6c07a", "#e6b866"):
        assert amber not in h


def test_badges_still_outline_not_filled():
    h = _html()
    # base badge keeps app background (outline treatment) - never a solid colour fill
    assert ".frz-beta .frz-cal-mile { display:inline-flex" in h and "background:var(--frz-bg-app)" in h
    # no solid green/blue/maroon fill declared on the badge
    assert "background:var(--frz-mile-ho)" not in h
    assert "background:var(--frz-mile-due)" not in h
    assert "background:var(--frz-mile-over)" not in h


def test_dark_remaps_present():
    h = _html()
    assert "body.dark-mode .frz-beta { --frz-mile-due: #3ba7e0; --frz-mile-over: #e0677a; }" in h


# ── Part 2: overdue ─────────────────────────────────────────────────────────────
def test_overdue_uses_mt_key_not_raw_date():
    b = _fn_body("_calMileOverdue")
    assert "_frzTodayMTKey()" in b
    assert "new Date(" not in b          # must not compare against a raw Date at the day boundary


def test_overdue_excludes_terminal():
    b = _fn_body("_calMileOverdue")
    assert "isTerminalStatus(p.status)" in b      # nothing completed renders red (invariant)


def test_overdue_handoff_enabled_uses_handoff_date():
    # J.R. enabled handoff overdue: a slipped handoff is judged against the handoff date, due/collapsed
    # against the effective due (revised folded into m.due). No early handoff short-circuit remains.
    b = _fn_body("_calMileOverdue")
    assert "kind==='handoff') ? m.handoff : m.due" in b
    assert "kind==='handoff') return false" not in b


def test_overdue_uses_revised_due():
    b = _fn_body("_calMileOverdue")
    # m.due already folds revised-wins-over-due, so a due/collapsed badge is judged against the
    # effective due date
    assert "_calItemMilestones(p)" in b and "m.due" in b


def test_badge_applies_overdue_class():
    b = _fn_body("_calMileBadge")
    assert "_calMileOverdue(p, kind)" in b
    assert "frz-cal-mile-overdue" in b
    assert "(overdue)" in b               # tooltip marks it


def test_overdue_css_wins():
    h = _html()
    assert ".frz-beta .frz-cal-mile-overdue { color:var(--frz-mile-over)" in h
    assert ".frz-beta .frz-cal-tl-mile.frz-cal-mile-overdue { border-left-color:var(--frz-mile-over)" in h


def test_timeline_marker_gets_overdue():
    b = _fn_body("_calTimeline")
    assert "_calMileOverdue(p, kind)" in b
    assert "frz-cal-mile-overdue" in b


# ── Part 3: layer toggles, URL-backed ───────────────────────────────────────────
def test_layer_params_in_url_map():
    h = _html()
    assert "avail:'cav'" in h and "miles:'cms'" in h    # ride _CAL_FILTER_PARAMS


def test_layer_helpers():
    assert "avail!=='0'" in _fn_body("_calShowAvail")
    assert "miles!=='0'" in _fn_body("_calShowMiles")


def test_toggle_controls_rendered():
    b = _fn_body("renderCalFilters")
    assert "layerChip('avail','Availability')" in b
    assert "layerChip('miles','Milestones')" in b
    assert "data-layer" in b
    assert "is-on" in b and "is-off" in b


def test_clear_preserves_layers():
    b = _fn_body("renderCalFilters")
    # Clear resets filters but keeps the layer toggles as-is (they are switches, not filters)
    assert "avail:f.avail,miles:f.miles" in b


def test_default_state_includes_layers():
    assert "avail:'',miles:''" in _fn_body("_calFilterState")


# ── Part 3.3: defaults / empty cases / all four builders ─────────────────────────
def test_both_off_deliberate_state():
    h = _html()
    assert "function _calBothOffHtml(" in h
    assert "Both layers are hidden" in h
    for fn in ("_calMonthGrid", "_calWeek", "_calTimeline"):
        assert "_calBothOffHtml()" in _fn_body(fn)
    assert "_calBothOffHtml()" in _fn_body("_calMobileAgenda")


def test_all_four_builders_honor_toggles():
    for fn in ("_calMonthGrid", "_calWeek", "_calTimeline", "_calMobileAgenda"):
        b = _fn_body(fn)
        assert "_calShowAvail()" in b, fn
        assert "_calShowMiles()" in b, fn


def test_milestones_off_guards_the_lane():
    # Milestones off must restore pre-6.54.0: the milestone build is gated by showMiles in each builder.
    assert "showMiles && !_calMilesSuppressed()" in _fn_body("_calMonthGrid")
    assert "showMiles && !u.pod" in _fn_body("_calWeek")
    assert "showMiles && !u.pod" in _fn_body("_calTimeline")
    assert "showMiles && !u.pod" in _fn_body("_calMobileAgenda")


def test_availability_off_gates_avail_content():
    # Availability off drops assignment chips + capacity, but keeps rows that carry a milestone.
    assert "if(!showAvail) visible=[]" in _fn_body("_calMonthGrid")
    wk = _fn_body("_calWeek")
    assert "if(showAvail){" in wk and "_calItemHasMileInWin(p, win)" in wk


# ── Invariants ──────────────────────────────────────────────────────────────────
def test_availability_builders_intact():
    h = _html()
    assert "function _calAsgChip(" in h
    assert "function _calMoChip(" in h
    assert "function _calCapRow(" in h


def test_no_indigo():
    h = _html()
    for frag in ("#5b4fff", "rgb(91,79,255)", "#7b6fff", "#4a3de0"):
        assert frag not in h
