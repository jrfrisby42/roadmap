"""JIRA-PULL-1 Stage 3 - the ancestry walk. Still creates nothing.

Walk upward from each candidate, admit a parent whose TYPE is in the pull set (any age), stop at a
non-selected type. Guards: a global visited set (dedupe + cross-seed cycle), a per-chain cycle guard, and
a per-run fetch cap that STOPS and reports rather than truncating. The walk is pure (fetch injected), so
every guard is exercised here; the live count runs in the report.
"""
import pytest
import server


def _iss(key, itype, parent=None, created="2026-08-01T00:00:00.000+0000"):
    f = {"issuetype": {"name": itype}, "created": created}
    if parent:
        f["parent"] = {"key": parent}
    return {"key": key, "fields": f}


PULL = {"Story", "Epic", "Roadmap Item"}


# ── the walk admits ancestors by type, with depth ─────────────────────────────────────────────────────
def test_walk_admits_ancestors_by_type_with_depth():
    cands = [_iss("S-1", "Story", parent="E-1")]
    store = {"E-1": _iss("E-1", "Epic", parent="R-1"), "R-1": _iss("R-1", "Roadmap Item")}
    anc, meta = server._jira_ancestry_walk(cands, set(), PULL, lambda k: store[k])
    assert set(anc) == {"E-1", "R-1"}
    assert anc["E-1"]["depth"] == 1 and anc["R-1"]["depth"] == 2
    assert meta["fetches"] == 2 and meta["maxDepth"] == 2


# ── acceptance 4: an ancestor whose type is deselected is not admitted ────────────────────────────────
def test_walk_stops_at_deselected_type():
    cands = [_iss("S-1", "Story", parent="E-1")]
    store = {"E-1": _iss("E-1", "Epic", parent="R-1")}
    anc, meta = server._jira_ancestry_walk(cands, set(), {"Story"}, lambda k: store[k])  # Epic deselected
    assert anc == {}                      # Epic not admitted; walk stopped
    assert meta["fetches"] == 1           # E-1 fetched to read its type, then stop


# ── the visited set dedupes a shared parent across seeds (one fetch, one admit) ────────────────────────
def test_walk_dedupes_shared_parent_across_seeds():
    cands = [_iss("S-1", "Story", parent="E-1"), _iss("S-2", "Story", parent="E-1")]
    calls, store = [], {"E-1": _iss("E-1", "Epic")}
    def fetch(k): calls.append(k); return store[k]
    anc, meta = server._jira_ancestry_walk(cands, set(), PULL, fetch)
    assert set(anc) == {"E-1"} and calls == ["E-1"] and meta["fetches"] == 1


# ── acceptance 3: a synthetic cycle terminates ────────────────────────────────────────────────────────
def test_walk_cycle_terminates():
    cands = [_iss("S-1", "Story", parent="A")]
    store = {"A": _iss("A", "Epic", parent="B"), "B": _iss("B", "Epic", parent="A")}   # A <-> B cycle
    anc, meta = server._jira_ancestry_walk(cands, set(), PULL, lambda k: store[k])
    assert set(anc) == {"A", "B"} and meta["fetches"] == 2   # terminated, no infinite loop


# ── acceptance 2: the cap trips cleanly and reports (never truncates) ─────────────────────────────────
def test_walk_cap_trips_and_reports():
    cands = [_iss("S-1", "Story", parent="A")]
    store = {"A": _iss("A", "Epic", parent="B"), "B": _iss("B", "Epic")}
    with pytest.raises(server._PullCapExceeded) as ei:
        server._jira_ancestry_walk(cands, set(), PULL, lambda k: store[k], cap=1)
    assert ei.value.cap == 1 and ei.value.fetches == 1     # stopped at the cap, did not fetch B


# ── an ancestor already in Flow is flagged but still traversed (its parent may be new) ────────────────
def test_walk_flags_already_in_flow_but_traverses_through():
    cands = [_iss("S-1", "Story", parent="E-1")]
    store = {"E-1": _iss("E-1", "Epic", parent="R-1"), "R-1": _iss("R-1", "Roadmap Item")}
    anc, meta = server._jira_ancestry_walk(cands, {"E-1"}, PULL, lambda k: store[k])
    assert anc["E-1"]["alreadyInFlow"] is True
    assert "R-1" in anc and anc["R-1"]["alreadyInFlow"] is False   # reached THROUGH the in-Flow E-1


# ── a parent that is itself a candidate is not re-admitted as an ancestor ─────────────────────────────
def test_walk_parent_that_is_a_candidate_not_admitted():
    cands = [_iss("S-1", "Story", parent="E-1"), _iss("E-1", "Epic", parent="R-1")]
    store = {"E-1": _iss("E-1", "Epic", parent="R-1"), "R-1": _iss("R-1", "Roadmap Item")}
    anc, meta = server._jira_ancestry_walk(cands, set(), PULL, lambda k: store[k])
    assert "E-1" not in anc               # E-1 is a candidate, not an ancestor
    assert "R-1" in anc                   # its true ancestor is still admitted


# ── endpoint guard + gating ───────────────────────────────────────────────────────────────────────────
def test_pull_plan_floor_not_set(client, team, admin_headers):
    r = client.get("/api/jira/pull-plan", headers=admin_headers).json()
    assert r["ready"] is False and r["reason"] == "floor not set" and r["firstRunCreateCount"] == 0


def test_pull_plan_admin_gated(client, team, viewer_headers):
    r = client.get("/api/jira/pull-plan", headers=viewer_headers)
    assert r.status_code in (401, 403)
