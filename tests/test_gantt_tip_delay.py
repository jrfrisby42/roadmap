"""GANTT-TIP-DELAY-1 (J.R. 2026-11-20: "put a longer delay on it so it doesn't bounce around while you are moving the
mouse"). Gantt hover tooltips (bars, expected diamonds, release markers, item-name descriptions) open only after the
pointer rests ~0.5s and then stay where they opened. Source guards (verified headless with real mouse moves)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def test_tooltips_wait_for_the_pointer_to_rest_and_stay_put():
    body = HTML.split("function _gtipDefer(el, show){", 1)[1].split("\n}\n", 1)[0]
    assert "if(_gtipOn === el) return;" in body                  # open: no chasing the pointer
    assert "clearTimeout(_gtipTimer);" in body and "}, _GTIP_MS);" in body
    assert "const _GTIP_MS = 550;" in HTML


def test_every_gantt_hover_tooltip_uses_it():
    for el in ("bar", "dm", "pl"):
        assert f"{el}.addEventListener('mousemove', _gtipDefer({el}, e => {{" in HTML, el
        assert f"{el}.addEventListener('mouseleave', _gtipHide);" in HTML, el
    assert "_gdescTimer = setTimeout(() => { if(!el.isConnected) return;" in HTML
    assert "}, _GTIP_MS); });" in HTML
