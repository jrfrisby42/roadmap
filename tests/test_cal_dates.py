"""CAL-DATES-1: item milestone dates (handoff + due) as badges on the Calendar.

Server surface: the two per-Organization label configs (handoffLabel/dueLabel) mirror metricLabel -
VALID_KEYS, defaults, /api/all coalescing, migration seed, empty->default fallback.

Client build (source-assertion against roadmap.html, using the string/comment-aware _fn_body walker
shared with test_flag_blocked2 / test_caphist_display): the qualifying chain, the milestone arithmetic
(absent testWeeks -> collapse, revised wins), the badge markup (click-through reuse), the per-tab
lanes + own overflow, timeline milestone-only rows, and the availability invariants.
"""
import json
import os

import server

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


def _cfg(client, headers, key):
    return client.get("/api/all", headers=headers).json().get(key)


# ── Server: the two label configs (mirror metricLabel) ──────────────────────────
def test_labels_in_valid_keys():
    assert "handoffLabel" in server.VALID_KEYS
    assert "dueLabel" in server.VALID_KEYS


def test_new_team_label_defaults(client, team, admin_headers):
    assert _cfg(client, admin_headers, "handoffLabel") == "Handoff"
    assert _cfg(client, admin_headers, "dueLabel") == "Due"


def test_default_handoff_is_neutral_not_dev_wording(client, team, admin_headers):
    # 1.1 decision: the default is neutral ("Handoff"), NOT the development-specific "Code complete".
    assert _cfg(client, admin_headers, "handoffLabel") != "Code complete"


def test_put_persists_custom_labels(client, team, admin_headers):
    assert client.put("/api/config/handoffLabel", json="Goes to print", headers=admin_headers).status_code == 200
    assert client.put("/api/config/dueLabel", json="Delivered", headers=admin_headers).status_code == 200
    assert _cfg(client, admin_headers, "handoffLabel") == "Goes to print"
    assert _cfg(client, admin_headers, "dueLabel") == "Delivered"


def test_put_requires_admin(client, team, editor_headers):
    assert client.put("/api/config/handoffLabel", json="X", headers=editor_headers).status_code == 403
    assert client.put("/api/config/dueLabel", json="X", headers=editor_headers).status_code == 403


def test_empty_label_falls_back_to_default(client, team, admin_headers):
    with server.db(team) as c:
        for k in ("handoffLabel", "dueLabel"):
            c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps("")))
    assert _cfg(client, admin_headers, "handoffLabel") == "Handoff"
    assert _cfg(client, admin_headers, "dueLabel") == "Due"


def test_missing_keys_seeded_on_migrate(client, team, admin_headers):
    with server.db(team) as c:
        c.execute("DELETE FROM config WHERE key IN ('handoffLabel','dueLabel')")
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    with server.db(team) as c:
        rows = {r["key"]: json.loads(r["value"]) for r in
                c.execute("SELECT key,value FROM config WHERE key IN ('handoffLabel','dueLabel')").fetchall()}
    assert rows.get("handoffLabel") == "Handoff"
    assert rows.get("dueLabel") == "Due"


def test_custom_label_survives_restart(client, team, admin_headers):
    client.put("/api/config/handoffLabel", json="Goes to print", headers=admin_headers)
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    assert _cfg(client, admin_headers, "handoffLabel") == "Goes to print"


# ── Client: module wiring ───────────────────────────────────────────────────────
def test_labels_declared_and_applied():
    h = _html()
    assert "let handoffLabel = 'Handoff';" in h
    assert "let dueLabel = 'Due';" in h
    assert "handoffLabel  = data.handoffLabel  || handoffLabel;" in h
    assert "dueLabel      = data.dueLabel      || dueLabel;" in h


# ── Client: qualifying chain (1.5) ──────────────────────────────────────────────
def test_qualify_chain():
    b = _fn_body("_calItemQualifies")
    assert "p.hidden" in b                      # hidden excluded
    assert "isTerminalStatus(p.status)" in b    # terminal excluded (J.R.'s decision)
    assert "isScheduledType(p.type" in b        # scheduled types only
    assert "!p.due" in b                        # due required
    assert "inScope(p)" in b                    # shared Space scope


# ── Client: milestone arithmetic (1.4 collapse, revised-wins, read-only) ────────
def test_milestone_absent_testweeks_collapses():
    b = _fn_body("_calItemMilestones")
    # absent testWeeks -> 0 (single collapsed badge), NOT the Gantt ?? 2 default
    assert "parseFloat(p.testWeeks) || 0" in b
    assert "p.testWeeks ?? 2" not in b          # deliberately NOT the Gantt/capacity default
    assert "tw>0 ? nextMonday(addDays(effDue, -tw*7)) : effDue" in b


def test_milestone_revised_wins():
    b = _fn_body("_calItemMilestones")
    assert "p.revised ? nextMonday(parseD(p.revised)) : dueD" in b


def test_collapse_yields_single_badge():
    b = _fn_body("_calItemBadgesOnDay")
    # handoff == due -> one 'both' badge, never two stacked
    assert "hd===dd" in b
    assert "kind:'both'" in b


# ── Client: badge markup reuses the existing click-through ───────────────────────
def test_badge_reuses_click_through_and_labels():
    b = _fn_body("_calMileBadge")
    assert "frz-cal-chip-item" in b                      # so the existing handler wires openItem
    assert "data-item=" in b
    assert "handoffLabel" in b and "dueLabel" in b       # labels come from config
    assert "frz-cal-mile-ho" in b and "frz-cal-mile-due" in b


def test_click_handler_untouched_and_covers_badges():
    # The existing delegated handler opens the item for ANY .frz-cal-chip-item (badges included).
    b = _fn_body("renderCalendar")
    assert "body.querySelectorAll('.frz-cal-chip-item')" in b
    assert "openItem(id)" in b


# ── Client: Month lane + its OWN overflow, availability lane untouched ───────────
def test_month_own_milestone_lane():
    b = _fn_body("_calMonthGrid")
    assert "mileByDay" in b
    assert "_CAL_MO_MILE_MAX" in b
    assert "frz-cal-mo-miles" in b
    assert "frz-cal-mile-more" in b
    assert "_calMilesSuppressed()" in b               # assignment-type focus suppresses badges
    # the availability chips + their own "+N more" are still built exactly as before
    assert "items.slice(0,_CAL_MO_MAX)" in b
    assert 'data-more="' in b


def test_month_empty_cell_guard_excludes_badges():
    b = _fn_body("_calMonthGrid")
    assert ".frz-cal-chip-item,.frz-cal-mile-more" in b


# ── Client: Week lane ───────────────────────────────────────────────────────────
def test_week_own_milestone_lane():
    b = _fn_body("_calWeek")
    assert "_calUserItems(o.owner, u.user)" in b
    assert "frz-cal-wk-miles" in b
    assert "_CAL_WK_MILE_MAX" in b
    # availability entries + their existing overflow untouched
    assert "entries.slice(0,_CAL_WK_MAX)" in b


# ── Client: Timeline markers + milestone-only rows ──────────────────────────────
def test_timeline_markers_and_rows():
    b = _fn_body("_calTimeline")
    assert "frz-cal-tl-mile" in b
    assert "frz-cal-chip-item" in b                    # click-through on the marker
    # a user with a milestone but no assignment still earns a row
    assert "_calUserItems(o.owner, u.user).length" in b
    # invariant: timeline still adds NO ticket/span chips for items (thin markers only, no bars)
    assert "_calTicketChip" not in b


# ── Client: Mobile agenda ───────────────────────────────────────────────────────
def test_mobile_agenda_badges():
    b = _fn_body("_calMobileAgenda")
    assert "_calUserItems(sec.owner, u.user)" in b
    assert "_calMileBadge(p," in b


# ── Client: CSS present + dark handling ─────────────────────────────────────────
def test_css_present():
    h = _html()
    assert ".frz-beta .frz-cal-mile {" in h
    assert ".frz-beta .frz-cal-tl-mile {" in h
    assert "body.dark-mode .frz-beta .frz-cal-mile-ho" in h        # amber needs a dark remap


# ── Invariants: availability builders + no write path ───────────────────────────
def test_availability_chip_builders_intact():
    h = _html()
    # the availability pill builders are unchanged (still present, unscoped by CAL-DATES-1)
    assert "function _calAsgChip(" in h
    assert "function _calMoChip(" in h
    assert "var _CAL_MO_MAX=3;" in h


def test_no_indigo_in_milestone_css():
    # No legacy indigo accent anywhere in the new badge styling.
    for frag in ("#5b4fff", "rgb(91,79,255)", "#7b6fff", "#4a3de0"):
        assert frag not in _html()
