"""PARENT-ROLLUP-1: derived parent rows on the Gantt (display only, never written, never counted).
An unscoped parent (has children, lacks its own start/due) draws a dashed bar derived from its children.

Client-only. The load-bearing invariant is that derived values cannot reach capacity: the render assigns
nothing back to the item, and buildCapacityUsageMap reads the item's STORED start/dev.
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


# ── derivation rules (1.2) ───────────────────────────────────────────────────────
def test_own_dated_parent_is_untouched():
    b = _fn_body("_computeRollup")
    # GANTT-TEAM-SLICES-1: the whole-item roll-up (team == null) still leaves an own-dated parent alone;
    # only a per-team slice (Projects only, grouped by Team) derives one, and only outside its owning team.
    b = _fn_body("_computeRollup")
    assert "if(p.start && p.due && team == null) return null;" in b
    o = _fn_body("_rollupOf")
    assert "if(_rollTeam == null) return _rollupAll(p);" in o
    assert "if(p.start && p.due && _effTeam(p) === _rollTeam) return null;" in o


def test_status_follows_rule_a():
    # ROLLUP-SYNC-1 (J.R. 2026-10-19) replaced "least-progressed child" with rule A, the same rule the server
    # writes onto the parent (_rollup_plan): all leaves finished -> least-progressed finished status; any leaf
    # started -> first active status; forward-only against the parent's own status.
    b = _fn_body("_computeRollup")
    assert "!RESERVED_TERMINAL_STATUSES.has(s)" in b       # Duplicate/Transferred are not countable work
    assert "_cnt.every(s => statusIsTerminal[s])" in b     # finished only when every leaf is
    assert "statuses.findIndex(s => statusIsActive[s])" in b   # "started" = at/after the first active status
    assert "!statusIsParked[s] && !statusIsDeferred[s]" in b   # parked/deferred never count as started
    assert "statuses.indexOf(_tgt)>statuses.indexOf(_own)" in b  # forward-only
    assert "const _own = p.status || '';" in b             # no opinion -> the parent's own status


def test_dates_earliest_latest_revised_wins():
    b = _fn_body("_computeRollup")
    assert "c.revised||c.due" in b                          # latest due, revised wins
    assert "starts[0]" in b                                 # earliest child start


def test_release_only_if_all_released():
    b = _fn_body("_computeRollup")
    assert "kids.every(c => statusIsReleased[c.status])" in b


def test_team_dominant_pod_plus_n_label():
    b = _fn_body("_computeRollup")
    assert "podCt[b]-podCt[a]" in b and "localeCompare" in b   # most children wins, alpha tie-break
    assert "' +'+(pods.length-1)" in b                         # "Wasatch +N" label
    assert "'Multiple'" in b                                   # assignee Multiple


# ── placement uses the derived dev in filter + grouping only ─────────────────────
def test_placement_in_filter_and_grouping():
    h = _html()
    assert "fDev.includes(p.dev || _rollupDev(p) || '')" in h            # pod filter placement
    assert "k = p.dev || _rollupDev(p) || 'Unassigned';" in h            # grouping placement


# ── render: dashed derived bar + empty marker, distinct from scoped ──────────────
def test_render_branch_draws_derived_or_marker():
    h = _html()
    assert "const _ru = _rollupOf(p);" in h
    assert "if(_ru){" in h                             # PARENT-ROLLUP-2: mid-tier parents (isChild) now draw too
    assert 'class="bar derived${' in h                      # ROLLUP-PROGRESS-1: + derived-prog / derived-done
    assert "rollup-noschedule" in h                          # Decision 4 empty marker
    assert "statusCls(_ru.status)" in h                      # derived status badge
    assert "rollup-team" in h                                # multi-team chip


def test_derived_bar_progress_fill():
    # ROLLUP-PROGRESS-1: the dashed outline is the track; the fill = finished share of child work, solid at
    # 100%. Display only - the progress comes from _computeRollup, which writes nothing back.
    h, b = _html(), _fn_body("_computeRollup")
    assert "const _done = _cnt.filter(s => statusIsTerminal[s]).length;" in b
    assert "progress, done:_done, total:_cnt.length" in b
    assert '<div class="derived-fill" style="width:${_pct}%"></div>' in h
    assert ".bar.derived .derived-fill {" in h and ".bar.derived.derived-done {" in h


def test_derived_bar_css_no_new_fill():
    h = _html()
    assert ".bar.derived { background: var(--surface2); border: 1.5px dashed var(--muted); opacity: 0.6;" in h


# ── the load-bearing invariant: no write path can reach capacity ─────────────────
def test_rollup_has_no_write_path():
    b = _fn_body("_computeRollup")
    for w in ("API.", "saveConfig", "putConfig", "p.start =", "p.due =", "p.status =", "p.dev ="):
        assert w not in b, w


def test_capacity_reads_stored_fields_unchanged():   # INVARIANT
    b = _fn_body("buildCapacityUsageMap")
    assert "if(p.hidden || !p.start || !p.dev) return;" in b   # a parent with no stored start/dev is skipped
    ic = _fn_body("itemCapacityOnDay")
    assert "if(!p.start || !p.dev) return 0;" in ic
