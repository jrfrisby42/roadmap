"""JIRA-PULL-1 Stage 4 - item construction. Pure-logic guards for the fields a pulled item carries.

The WRITE path (insert of 81 items) is exercised in the live two-phase acceptance, not here (needs Jira +
a real DB). These cover the deterministic construction: reverse type/status maps + the jiraPullStatusMap
overlay, the Pod- label matcher, the lossy-safe ADF->HTML converter (the stored-XSS surface), and that an
unmapped type is skipped while an unmapped status falls to the Org default (nothing silently dropped).
"""
import server


def _iss(key, itype="Story", status="NEW", labels=None, acct=None, desc=None,
         proj="FRAZ", summary="S", start=None, due=None):
    f = {"summary": summary, "issuetype": {"name": itype}, "status": {"name": status}, "project": {"key": proj}}
    if labels is not None: f["labels"] = labels
    if acct: f["assignee"] = {"accountId": acct}
    if desc is not None: f["description"] = desc
    if start: f["customfield_10025"] = start
    if due: f["duedate"] = due
    return {"key": key, "fields": f}


def _para(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


CTX = {
    "type_rev": {"Story": "Story", "Epic": "Epic", "Roadmap Item": "Roadmap Item", "Feature Request": "Feature"},
    "status_eff": {"NEW": "New", "In Dev": "In Progress", "Code Review": "In Progress"},   # incl overlay
    "overlay": {"Code Review": "In Progress"},
    "proj_rev": {"FRAZ": "Fraznet"},
    "pods": ["Everest", "Wasatch", "Support"],
    "assignee_map": {"acc-1": "jane.doe"},
}


# ── the constructed item, field by field ──────────────────────────────────────────────────────────────
def test_construct_basic_fields():
    item, meta = server._construct_pull_item(
        _iss("FRAZ-1", labels=["phase-0", "Pod-Everest"], acct="acc-1", desc=_para("hello"),
             start="2026-08-04", due="2026-09-01"), CTX, "admin")
    assert item["product"] == "Fraznet"
    assert item["type"] == "Story"
    assert item["status"] == "New"
    assert item["dev"] == "Everest"            # Pod- matched on the PREFIX, not the first label (phase-0)
    assert item["assignee"] == "jane.doe"
    assert item["description"] == "<p>hello</p>"
    assert item["start"] == "2026-08-04" and item["due"] == "2026-09-01"
    assert item["jiraTickets"] == ["FRAZ-1"] and item["jiraSource"] == "pull"
    assert item["reporter"] == "admin"


def test_status_via_overlay():
    item, meta = server._construct_pull_item(_iss("FRAZ-2", status="Code Review"), CTX, "admin")
    assert item["status"] == "In Progress" and meta["statusViaOverlay"] is True


def test_unmapped_type_is_skipped():
    item, meta = server._construct_pull_item(_iss("FRAZ-3", itype="Bug"), CTX, "admin")
    assert item is None and meta["skip"] == "unmapped-type" and meta["jiraType"] == "Bug"


def test_unmapped_status_falls_to_org_default():
    # "" is left so _insert_project stamps the Org default - the item still ARRIVES, nothing dropped.
    item, meta = server._construct_pull_item(_iss("FRAZ-4", status="Some New Jira Status"), CTX, "admin")
    assert item["status"] == "" and meta["statusUnmapped"] is True


def test_assignee_only_from_map():
    resolved, m1 = server._construct_pull_item(_iss("FRAZ-5", acct="acc-1"), CTX, "admin")
    assert resolved["assignee"] == "jane.doe" and m1["assigneeResolved"] is True
    unresolved, m2 = server._construct_pull_item(_iss("FRAZ-6", acct="acc-unknown"), CTX, "admin")
    assert unresolved["assignee"] == "" and m2["hadJiraAssignee"] is True and m2["assigneeResolved"] is False
    none, m3 = server._construct_pull_item(_iss("FRAZ-7"), CTX, "admin")
    assert none["assignee"] == "" and m3["hadJiraAssignee"] is False


# ── the Pod- label matcher ────────────────────────────────────────────────────────────────────────────
def test_pod_matcher():
    assert server._jira_pull_pod(["Pod-Everest", "sap"], CTX["pods"]) == ("Everest", "matched")
    assert server._jira_pull_pod(["Pod-Everest", "Pod-Wasatch"], CTX["pods"]) == ("", "multiple-pod-labels")
    assert server._jira_pull_pod(["Pod-Nope"], CTX["pods"]) == ("", "pod-not-in-flow")
    assert server._jira_pull_pod(["day-one", "external-dependency"], CTX["pods"]) == ("", "no-pod-label")
    assert server._jira_pull_pod([], CTX["pods"]) == ("", "no-pod-label")


# ── the lossy-safe ADF converter (the stored-XSS surface) ─────────────────────────────────────────────
def test_adf_escapes_and_flattens():
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "<script>alert(1)</script>"}]},
        {"type": "bulletList", "content": [{"type": "listItem", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "buy milk"}]}]}]},
        {"type": "blockquote", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "q"}]}]},
    ]}
    out, flat = server._adf_to_safe_html(adf)
    assert "<script>" not in out and "&lt;script&gt;" in out     # escaped - no raw markup survives
    assert "buy milk" in out                                     # list flattened to text
    assert flat.get("bulletList") == 1 and flat.get("blockquote") == 1


def test_adf_hardbreak_and_empty():
    adf = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "a"}, {"type": "hardBreak"}, {"type": "text", "text": "b"}]}]}
    assert server._adf_to_safe_html(adf) == ("<p>a<br>b</p>", {})
    assert server._adf_to_safe_html(None) == ("", {})
    assert server._adf_to_safe_html({"type": "doc", "content": []}) == ("", {})


# ── endpoint guard + gating ───────────────────────────────────────────────────────────────────────────
def test_pull_endpoint_floor_not_set(client, team, admin_headers):
    r = client.post("/api/jira/pull", json={"dryRun": True}, headers=admin_headers).json()
    assert r["ready"] is False and r["reason"] == "floor not set" and r["createdCount"] == 0


def test_pull_endpoint_admin_gated(client, team, viewer_headers):
    r = client.post("/api/jira/pull", json={}, headers=viewer_headers)
    assert r.status_code in (401, 403)
