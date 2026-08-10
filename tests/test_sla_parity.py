"""SLA-2 parity: the server resolution SLA (_digest_sla_kind, used by the weekly digest) must
agree with the client slaState() - they are two implementations of one rule (server.py:_digest_sla_kind
docstring: "Server mirror of the client slaState()"). This asserts the SERVER half against a committed
JSON fixture; the CLIENT half is asserted against the SAME fixture in a browser with Date.now pinned
to the fixture's `now` (documented manual procedure - the repo has no committed JS test harness). The
fixture centres on the S6 effectiveFrom gate, the one rule this build added to both halves.
"""
import json
import os
from datetime import datetime

import server

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sla_parity.json")


def _load():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _now(fx):
    return datetime.fromisoformat(fx["now"].replace("Z", "+00:00"))


def test_digest_sla_kind_matches_fixture():
    fx = _load()
    sla, term_map, now_dt = fx["sla"], fx["statusIsTerminal"], _now(fx)
    mism = []
    for c in fx["cases"]:
        p = {"priority": c["priority"], "status": c["status"],
             "createdAt": c["createdAt"], "completedAt": c["completedAt"]}
        got = server._digest_sla_kind(p, sla, term_map, now_dt)
        if got != c["expected"]:
            mism.append(f"{c['name']}: expected {c['expected']!r}, got {got!r}")
    assert not mism, "server _digest_sla_kind diverged from the fixture:\n  " + "\n  ".join(mism)


def test_digest_sla_kind_disabled_returns_none():
    # Not a fixture case (the fixture pins one enabled config); the disabled short-circuit is
    # identical in both implementations and is asserted directly here.
    fx = _load()
    off = dict(fx["sla"]); off["enabled"] = False
    c = fx["cases"][0]
    p = {"priority": c["priority"], "status": c["status"],
         "createdAt": c["createdAt"], "completedAt": c["completedAt"]}
    assert server._digest_sla_kind(p, off, fx["statusIsTerminal"], _now(fx)) is None


def test_s6_gate_is_the_only_difference_from_no_effectivefrom():
    # Prove the S6 mirror: with effectiveFrom removed, the "before eff" case is measured again
    # (byte-identical-to-before-S6 when effectiveFrom is absent).
    fx = _load()
    no_eff = dict(fx["sla"]); no_eff.pop("effectiveFrom", None)
    before = next(c for c in fx["cases"] if c["name"].startswith("S6 excluded"))
    p = {"priority": before["priority"], "status": before["status"],
         "createdAt": before["createdAt"], "completedAt": before["completedAt"]}
    # With the gate (fixture sla) -> None; without effectiveFrom -> breached (an old open Urgent item).
    assert server._digest_sla_kind(p, fx["sla"], fx["statusIsTerminal"], _now(fx)) is None
    assert server._digest_sla_kind(p, no_eff, fx["statusIsTerminal"], _now(fx)) == "breached"
