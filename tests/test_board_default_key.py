"""SHELL-DEBT-1 Part 2 (Addendum A4) regression guard: the Kanban default-board key is TEAM-SCOPED.

`_defaultBoardKey()` lives in roadmap.html's beta module (closure-scoped frontend JS), so the
backend TestClient suite cannot reach it behaviorally. This is a SOURCE-PRESENCE guard: it asserts
the fix's SHAPE is present in roadmap.html - the default-board key is built from BOTH the team slug
(`_teamSlug()`) AND the user (`uname()`), not the user alone. Reverting the fix (back to
`'frazil_beta_defaultboard_'+uname()`) makes this FAIL, which is exactly the regression it guards:
a team-blind key let a default set on one Organization apply on all five.

LIMITATION, stated plainly per A4.1: this guards the key SHAPE in source, not runtime behavior. It
would still pass if _teamSlug() were wired to return "" (collapsing the scope) - so it is a tripwire
for a straight revert, not a full behavioral assertion. The behavioral assertions (a foreign default
resolves to __all__ via validate-on-read; the legacy key is read once then lapses) require a live
two-Organization session and live as a browser-console snippet, tests/board_default_checks.js. They
are NOT expressible in the pytest/backend harness (no JS engine, no committed jsdom harness, and the
functions are closure-scoped). This file is deliberately not a substitute for those.
"""
import re
import pathlib

ROADMAP_HTML = pathlib.Path(__file__).resolve().parent.parent / "roadmap.html"


def _default_board_key_body() -> str:
    src = ROADMAP_HTML.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"function _defaultBoardKey\(\)\s*\{([^}]*)\}", src)
    assert m, "could not locate function _defaultBoardKey() in roadmap.html"
    return m.group(1)


def test_default_board_key_is_team_scoped():
    body = _default_board_key_body()
    assert "_teamSlug()" in body, (
        "default-board key is NOT team-scoped: _defaultBoardKey() must include _teamSlug() so a "
        "default set on one Organization cannot apply on another. Reverting to "
        "'frazil_beta_defaultboard_'+uname() restores the cross-Organization leak."
    )


def test_default_board_key_is_still_per_user():
    body = _default_board_key_body()
    assert "uname()" in body, (
        "default-board key must still be per-user: _defaultBoardKey() must include uname()."
    )
