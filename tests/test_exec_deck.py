"""EXEC-DECK-1: the monthly exec slides.

The model mirrors the Team Board (projects only, Now / Next / Finished, x/y done over the actual work,
health for Now); the deck is real PowerPoint (python-pptx) with a title slide and, per Org, a Roadmap
Progress table, a Delivered and Next slide and a Current Roadmap Gantt; the email carries it as an
attachment; GET /api/exec-deck is admin-only. Built against python-pptx's default template here."""
import io
import json
from datetime import date

import pytest

import server

pptx = pytest.importorskip("pptx")

TODAY = date(2026, 11, 1)


def _seed(team):
    with server.db(team) as c:
        for k, v in (("statuses", ["New", "Approved", "In Progress", "Released"]), ("statusIsActive", {"In Progress": True}),
                     ("statusIsApproved", {"Approved": True}), ("statusIsTerminal", {"Released": True}),
                     ("types", [{"name": n} for n in ("Roadmap Item", "Epic", "Story")])):
            c.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (k, json.dumps(v)))
        ins = lambda **f: server._insert_project(c, {"status": "New", **f})
        rm = ins(name="Platform", type="Roadmap Item", status="In Progress", dev="Wasatch", priority="2")
        ep = ins(name="Epic", type="Epic", parent=rm, status="In Progress")
        ins(name="Done story", type="Story", parent=ep, status="Released", dev="Wasatch", start="2026-10-01", due="2026-10-10", releaseDate="2026-10-12")
        ins(name="Open story", type="Story", parent=ep, status="In Progress", dev="Everest", start="2026-10-20", due="2026-11-20")
        ins(name="Dup story", type="Story", parent=ep, status="Duplicate")
        ins(name="Late project", type="Roadmap Item", status="In Progress", dev="Everest", start="2026-09-01", due="2026-10-15")
        ins(name="Queued project", type="Roadmap Item", status="Approved", dev="Wasatch", start="2026-11-15", due="2026-12-15")
        ins(name="Shipped project", type="Roadmap Item", status="Released", dev="Wasatch", start="2026-09-01", due="2026-10-01", releaseDate="2026-10-05")
        ins(name="Old project", type="Roadmap Item", status="Released", dev="Wasatch", releaseDate="2026-06-01")
        ins(name="Loose story", type="Story", status="In Progress", dev="Wasatch", start="2026-10-20", due="2026-11-05")
        ins(name="Hidden project", type="Roadmap Item", status="In Progress", dev="Wasatch", hidden=True)


def test_model_matches_the_team_board(team):
    _seed(team)
    m = server._exec_model(team, TODAY)
    by = {c["name"]: c for c in m["now"] + m["next"] + m["finished"]}
    assert set(by) == {"Platform", "Late project", "Queued project", "Shipped project"}   # projects only, 45-day window, no hidden
    p = by["Platform"]
    assert p["col"] == "now" and p["teams"] == ["Everest", "Wasatch"] and p["priority"] == "High"
    assert (p["done"], p["total"]) == (1, 2)                                   # Epic + Duplicate are not work
    assert p["health"][0] == "green"
    assert by["Late project"]["health"][0] == "red"
    assert by["Queued project"]["col"] == "next" and by["Shipped project"]["col"] == "finished"


def _texts(prs):
    out = []
    for s in prs.slides:
        bits = []
        for sh in s.shapes:
            if sh.has_text_frame:
                bits.append(sh.text_frame.text)
            if getattr(sh, "has_table", False) and sh.has_table:
                bits += [c.text for r in sh.table.rows for c in r.cells]
        out.append(" | ".join(bits))
    return out


def test_deck_has_the_expected_slides(team):
    _seed(team)
    data = server.build_exec_deck([team], TODAY, template="")
    slides = _texts(pptx.Presentation(io.BytesIO(data)))
    label = server._exec_org_label(team)
    assert "Technology Update - November 2026" in slides[0]
    assert f"{label} Roadmap Progress" in slides[1] and "Platform" in slides[1] and "1/2 done" in slides[1]
    assert "Late project" in slides[1] and "Queued project" not in slides[1]
    assert "This month: 1 story done - Done story" in slides[1]                        # EXEC-DECK-2 outcome facts
    assert f"{label} - Work Delivered" in slides[2] and "items delivered" in slides[2]   # EXEC-DECK-2
    assert f"{label} - Delivered and Next" in slides[3] and "Shipped project" in slides[3] and "Queued project" in slides[3]
    assert f"{label} Current Roadmap" in slides[4] and "Platform" in slides[4]
    assert f"{label} - Shipped, Last 30 Days" in slides[5] and "Done story" in slides[5] and "Platform  (1)" in slides[5]
    assert len(slides) == 6


def test_work_delivered_counts_finished_work_not_projects(team):
    _seed(team)
    d = server._exec_model(team, TODAY)["delivered"]
    assert d["count"] == 2                                   # Done story + the childless Shipped project; not Old/Dup/open
    assert dict(d["byType"]) == {"Story": 1, "Roadmap Item": 1}
    assert dict(d["byTeam"]) == {"Wasatch": 2}
    assert [n for _, n in d["perMonth"]] == [1, 0, 0, 0, 2, 0]   # Jun (Old project) .. Nov; Oct has the two


def test_email_carries_the_deck(team, monkeypatch):
    _seed(team)
    sent = {}
    monkeypatch.setattr(server, "send_email_attachment",
                        lambda to, subj, body, fname, data, **k: sent.update(to=to, subj=subj, body=body, fname=fname, n=len(data)))
    monkeypatch.setattr(server, "EXEC_DECK_TEMPLATE", "")
    r = server.send_exec_deck([team, "no-such-org"], "jr@example.com", today=TODAY)
    assert sent["to"] == "jr@example.com" and sent["subj"] == "Technology Update slides - November 2026"
    assert sent["fname"].endswith(".pptx") and sent["n"] > 10000
    assert r["counts"] == {team: {"now": 2, "next": 1, "finished": 1}}            # unknown Orgs are skipped


def test_download_is_admin_only(client, team, admin_headers, editor_headers, monkeypatch):
    _seed(team)
    monkeypatch.setattr(server, "EXEC_DECK_TEMPLATE", "")
    r = client.get("/api/exec-deck", headers=admin_headers)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert "presentationml" in r.headers["content-type"] and ".pptx" in r.headers["content-disposition"]
    assert client.get("/api/exec-deck", headers=editor_headers).status_code == 403
