"""ADMIN-PAGE-1: Admin as one page per section with a grouped admin menu replacing the left rail.

J.R.'s decisions (2026-11-13): the menu replaces the rail while in Admin, /admin lands on Users, groups
People / Work setup / Integrations / Organization. Client-only; these guard the wiring. The page itself
was driven headlessly (every section, old links, back/forward, add forms, autosave, phone + hidden rail).
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def _menu():
    return HTML.split("var _ADM_MENU = [", 1)[1].split("\n  ];", 1)[0]


def test_menu_groups_in_order():
    groups = re.findall(r"\['([A-Za-z ]+)', \[", _menu())
    assert groups == ["People", "Work setup", "Integrations", "Organization"]


def test_menu_sections_and_panes():
    m = _menu()
    pairs = re.findall(r"slug:'([a-z-]+)', (?:pane:'([A-Za-z_]+)'|href:'([^']+)')", m)
    got = [(s, p or h) for s, p, h in pairs]
    assert got == [
        ("users", "usersTab"), ("teams", "devTab"), ("departments", "deptTab"), ("availability", "assignmentsTab"),
        ("spaces", "productTab"), ("types", "typesTab"), ("statuses", "statusTab"), ("reasons", "delayTab"),
        ("terminology", "frzAdmTermsPane"),
        ("jira", "teamSettingsTab"), ("assethub", "frzAdmAssetPane"), ("slack", "frzAdmSlackPane"),
        ("views-access", "frzAdmAccessPane"), ("portal", "frzAdmPortalPane"), ("audit", "/audit"),
        ("data", "dataTab"), ("system", "systemTab"),
    ]
    assert "sys:true" in m.split("slug:'system'", 1)[1], "System stays operator-only"


def test_lands_on_users_and_old_links_still_resolve():
    assert "var _ADM_DEFAULT = 'usersTab';" in HTML
    alias = HTML.split("var _ADM_ALIAS = {", 1)[1].split("};", 1)[0]
    for old, new in (("owners", "teams"), ("projects", "spaces"), ("assignments", "availability"),
                     ("team-settings", "jira"), ("integrations", "assethub")):
        assert f"'{old}':'{new}'" in alias, f"old /admin/{old} links must keep working"


def test_no_tab_rows_left_in_the_shell():
    assert "frzAdminTabs" not in HTML and "data-admtab" not in HTML
    assert "_setAdminTopTab" not in HTML
    assert ".frz-beta .frz-adminpage .admin-tabs" in HTML


def test_rail_swaps_to_admin_menu():
    rr = HTML.split("  function renderRail(){", 1)[1][:400]
    assert "root.classList.toggle('frz-admin-active', state.view==='admin')" in rr
    assert ".frz-beta.frz-admin-active .frz-scroll:not(.frz-adm-nav) { display:none; }" in HTML
    assert 'id="frzAdmNav"' in HTML


def test_settings_cards_are_distributed_after_render():
    ts = HTML.split("  function renderTeamSettings(){", 1)[1].split("\n  }", 1)[0]
    assert ts.index("_frzWireTeamSettings(page)") < ts.index("_frzAdmDistribute()"), "move cards only after wiring"


def test_moved_sections_keep_their_init():
    init = HTML.split("var _ADM_INIT = {", 1)[1].split("};", 1)[0]
    for pane in ("teamSettingsTab", "deptTab", "assignmentsTab", "systemTab", "typesTab", "frzAdmPortalPane", "frzAdmTermsPane"):
        assert pane + ":" in init


def test_dark_mode_buttons_and_classic_phone_rules():
    clobber = [l for l in HTML.splitlines() if l.strip().startswith("body.dark-mode button:not(.primary)")][0]
    assert ":not(.btn-add-dev)" in clobber and ":not(.frz-adm-pill)" in clobber
    assert "body:not(.frz-beta-active) .admin-modal { width: 98vw !important; }" in HTML
    assert ".modal:not(.frz-composer):not(.frz-adminpage .admin-modal)" in HTML
