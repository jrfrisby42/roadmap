"""GANTT-WIP-FILL-1 (J.R. 2026-11-19: "why am i seeing outline only bars in my gantt chart again?"). A project's
roll-up bar filled only with FINISHED work, so a project with work started but nothing finished (FRZ-1246: 0 of 18
finished, 14 started) drew as an empty dashed outline. The in-progress share now fills too, in a lighter green after
the finished share; only a project with nothing finished or started keeps the faded outline. Display only. Source
guards (behaviour verified headless with seeded parents)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def test_in_progress_share_follows_the_finished_share_and_never_overflows():
    assert "const _wip = (_pct < 100 && _ru.total) ? Math.min(100 - _pct, Math.round((_ru.active || 0) / _ru.total * 100)) : 0;" in HTML
    assert '<div class="derived-wip" style="left:${_pct}%;width:${_wip}%"></div>' in HTML


def test_started_work_alone_lifts_the_faded_outline():
    assert "${(_pct>0||_wip>0)?' derived-prog':''}" in HTML
    assert ".bar.derived .derived-wip { position: absolute;" in HTML
    assert "body.dark-mode .bar.derived .derived-wip" in HTML


def test_tooltip_says_how_much_is_in_progress():
    assert "${_ru.active && _pct < 100 ? `, ${_ru.active} in progress` : ''} (${_pct}% done)" in HTML


def test_active_count_is_leaf_work_in_an_active_unfinished_status():
    body = HTML.split("function _computeRollup(", 1)[1].split("\n  }\n", 1)[0]
    assert "const _act = _leaves.filter(c => c.status && statusIsActive[c.status] && !statusIsTerminal[c.status]" in body
    assert "active:_act.length" in body
