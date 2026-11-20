"""GANTT-VIEW-MENU-1 + GANTT-PROJ-FINISH-1 (J.R. 2026-11-20: Gantt toolbar buttons were moving off the screen; "1 looks
good" for a projected finish). The Gantt's own settings (Time, Items, Group by, Sprints band, Capacity, Projected finish)
live in ONE View menu on the right of the filter row; filter chips that don't fit fold into a "+N more" chip. Parent bars
get a projected-finish marker from the pace of the last 6 weeks. Display only. Source guards (behaviour verified headless
with seeded parents at different paces, light + dark, 1440 down to 700px)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def test_gantt_toolbar_has_one_view_menu():
    assert "gantt:    { chips:['owner','assignee','type','status'], extra:['priority','dept'], opts:['view'] }" in HTML
    assert "if (o==='view') return viewMenu(anchor);" in HTML
    body = HTML.split("function _frzBuildViewMenu(pop, focusKey){", 1)[1].split("\n  function _frzFoldChips(", 1)[0]
    for k in ("_frzSetGanttSpan", "_frzSetGanttItems", "'_ganttToggleSprints'", "'toggleCapacityView'", "'_ganttToggleProj'", "Reset to default"):
        assert k in body, k
    assert "byTeam ? 'Team load under each group' : 'Only when grouped by Team'" in body   # capacity needs Team grouping


def test_view_button_counts_settings_changed_from_default():
    body = HTML.split("function _frzGanttViewState(){", 1)[1].split("\n  }\n", 1)[0]
    assert "g!=='developer'" in body and "'Sprints band off'" in body and "'Capacity on'" in body and "'Projected finish off'" in body
    assert "'<span class=\"frz-vm-badge\">+'+_vs.changed.length+'</span>'" in HTML


def test_chips_fold_into_more_and_the_first_chip_stays():
    assert "_frzFoldChips(bar);" in HTML
    body = HTML.split("function _frzFoldChips(bar){", 1)[1].split("\n  }\n", 1)[0]
    assert "['board','scope','saved'].indexOf(c.getAttribute('data-chip'))<0" in body
    assert "for (var i=chips.length-1; i>=1; i--)" in body
    assert "if (moreB){ moreChipsMenu(moreB); return; }" in HTML
    assert "_frzFoldTimer=setTimeout(function(){ if(!state.onItem) renderBar(); }, 150);" in HTML


def test_projection_uses_the_last_six_weeks_and_needs_three_finished():
    body = HTML.split("function _computeRollup(", 1)[1].split("\n  }\n", 1)[0]
    assert "const _recentFrom = addDays(TODAY, -42);" in body
    assert "(_left > 0 && progress < 1 && _recent >= 3) ? addDays(TODAY, Math.ceil(_left * 42 / _recent)) : null" in body
    assert "projEnd:_projEnd, projRecent:_recent" in body


def test_marker_draws_on_derived_and_own_dated_parents_and_turns_red_when_late():
    assert "const _pm = _projMark(p, _ru, parseD(_ru.due), _dd);" in HTML
    assert "_bar += _pm.html;   // GANTT-PROJ-FINISH-1" in HTML
    assert "if(_ks && s && d) cH += _projMark(p, _ks, parseD(p.revised || p.due), _ownEnd).html;" in HTML
    body = HTML.split("function _projMark(p, r, dueD, barEnd){", 1)[1].split("\n  }\n", 1)[0]
    assert "if(!_ganttShowProj || !r || !r.projLeft" in body
    assert "const late = !!(dueD && r.projEnd > dueD);" in body
    assert "No projected finish yet: fewer than 3 items finished in the last 6 weeks" in body
    assert ".proj-mark.is-late { border-left-color: #c0362c; }" in HTML


def test_projected_finish_switch_is_per_person_default_on():
    assert "localStorage.getItem('frazil_gantt_proj') !== '0'" in HTML
    assert "localStorage.setItem('frazil_gantt_proj', _ganttShowProj ? '1' : '0')" in HTML
