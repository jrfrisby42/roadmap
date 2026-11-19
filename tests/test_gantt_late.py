"""GANTT-LATE-1 (J.R. 2026-11-19: "on the gantt chart, it looks like there is nothing happening right now").
70 unfinished Fraznet items had end dates already past (38 carried into the Active sprint with their old sprint's
dates), so their projects' roll-up bars ended before today. Unfinished work past its dates now carries its bar on,
hatched (the existing overrun segment): to the Active sprint's end when it sits in that sprint, else to today when it
has STARTED. Unstarted work that missed its dates is left alone. Display only. Source guards (behaviour verified
headless with seeded carried / late / untouched items)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def _fn(name):
    return HTML.split("function " + name + "(", 1)[1].split("\n  }\n", 1)[0]


def test_late_rule_sprint_first_then_started_work_only():
    body = _fn("_lateEndOf")
    assert "statusIsTerminal[st] || RESERVED_TERMINAL_STATUSES.has(st)" in body, "finished work never extends"
    assert "if(!e || e >= TODAY) return null;" in body, "only work whose end has passed"
    assert "sp.state === 'Active'" in body and "kind: 'sprint'" in body, "carried into the Active sprint -> its end"
    assert "statusIsActive[st] && !statusIsParked[st] && !statusIsDeferred[st]" in body, "else started work only"
    assert "end: TODAY, kind: 'late'" in body


def test_rollup_keeps_its_planned_span_and_adds_the_late_end():
    body = _fn("_computeRollup")
    assert "_lateEndOf(c, s ? s.due : (c.revised || c.due))" in body
    assert "lateEnd:_lateEnd, carried:_carried, lateN:_lateN" in body
    # the planned span is unchanged (dues still come from own dates / the sprint backstop)
    assert "const dues   = kids.map(c => { const s = _sb(c); return s ? s.due : (c.revised||c.due); })" in body


def test_bars_draw_the_late_part_hatched_and_nothing_is_written():
    assert "const _le = (_ru.lateEnd && _dd && _ru.lateEnd > _dd) ? _ru.lateEnd : null;   // GANTT-LATE-1" in HTML
    assert "const _late     = (s && d) ? _lateEndOf(p, p.revised || p.due) : null;" in HTML
    assert "const _tail    = _relOver || _kidOver || _lateOver;" in HTML, "the bar's end squares off where the hatch joins"
    seg = HTML.split("if(_lateOver){", 1)[1].split("\n        }\n", 1)[0]
    assert 'class="bar overrun"' in seg and "p.revised =" not in seg and "p.due =" not in seg


def test_running_work_stays_in_the_now_window():
    body = _fn("_inWindow")
    assert "if(_lt) dd.push(_lt);" in body
