"""INTAKE-TEMPLATE-1: normalize every stored per-Type description template to HTML, once.

After this runs, the intake portal appends a Type's template VERBATIM (no server-side conversion),
relying on the invariant "every stored template is HTML." Templates authored via the rich editor
(since 6.43.1) are already HTML; legacy TYPE-TEMPLATE-1 templates are plain newline-separated label
lines and are converted here to <p> paragraphs - the SAME shape roadmap.html's _frzTemplateToHTML
produces client-side (this is a one-time normalizer, NOT a second live conversion path).

Idempotent + re-runnable: a template already in HTML form is left untouched; running twice converts
nothing the second time. Reports per-Organization counts. If a template does not convert cleanly
(the conversion is not idempotent, or yields empty from non-empty), it STOPS without writing that
team, rather than guessing at the template's intent.

Usage (on the prod host):
    python3 tools/migrate_normalize_templates.py            # dry-run: report only, write nothing
    python3 tools/migrate_normalize_templates.py --apply    # write conversions (WAL-safe backup per team first)

TENANTS dir: $FRAZIL_TENANTS_DIR or /data/tenants.
"""
import sys
import os
import json
import re
import sqlite3
from datetime import datetime

TENANTS = os.environ.get("FRAZIL_TENANTS_DIR", "/data/tenants")

# Must stay in lockstep with roadmap.html _frzTemplateToHTML's tag set (test_type_template.py guards it).
_TAG_RE = re.compile(r"<(p|div|ul|ol|li|h[1-6]|strong|em|s|a|br|blockquote|code|pre|span|mark)\b", re.I)


def to_html(tmpl):
    """Mirror of _frzTemplateToHTML: an already-HTML template passes through; a legacy plain-line
    template converts to <p> paragraphs (no escaping - frzSanitize runs on the client save path)."""
    tmpl = (tmpl or "").strip()
    if not tmpl:
        return ""
    if _TAG_RE.search(tmpl):
        return tmpl
    return "".join("<p>" + (line if line.strip() else "<br>") + "</p>" for line in tmpl.split("\n"))


def normalize_types(types):
    """Return (new_types, converted_count). Raises ValueError if a template won't convert cleanly."""
    converted = 0
    for t in types:
        if not isinstance(t, dict):
            continue
        tv = t.get("template")
        if not (isinstance(tv, str) and tv.strip()):
            continue
        html = to_html(tv)
        if to_html(html) != html or not html.strip():        # clean = idempotent + non-empty
            raise ValueError("template for type %r does not convert cleanly (head=%r)" % (t.get("name"), tv[:80]))
        if html != tv:
            t["template"] = html
            converted += 1
    return types, converted


def main(apply):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    teams = sorted(t for t in os.listdir(TENANTS)
                   if os.path.isfile(os.path.join(TENANTS, t, "roadmap.db")))
    total = 0
    for team in teams:
        dbp = os.path.join(TENANTS, team, "roadmap.db")
        c = sqlite3.connect(dbp)
        row = c.execute("SELECT value FROM config WHERE key='types'").fetchone()
        if not row:
            c.close()
            continue
        types = json.loads(row[0])
        try:
            types, converted = normalize_types(types)
        except ValueError as e:
            print("STOP: %s / %s - not written." % (team, e))
            c.close()
            sys.exit(2)
        if converted:
            total += converted
            print("%-16s converted=%d" % (team, converted))
            if apply:
                bak = dbp + ".tmplnorm-bak-" + ts
                s = sqlite3.connect(dbp); d = sqlite3.connect(bak); s.backup(d); d.close(); s.close()
                c.execute("UPDATE config SET value=? WHERE key='types'", (json.dumps(types),))
                c.commit()
        else:
            print("%-16s converted=0 (already HTML / none)" % team)
        c.close()
    print("TOTAL converted:", total, "(applied)" if apply else "(dry-run - re-run with --apply to write)")


if __name__ == "__main__":
    main("--apply" in sys.argv)
