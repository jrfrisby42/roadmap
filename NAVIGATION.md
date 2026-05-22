# NAVIGATION.md

Quick `grep` recipes for getting around the two big files fast.

---

## `server.py` (~3,100 lines)

```bash
# All API routes
grep -n '^@app\.' server.py

# Major section headers
grep -n '^# ── ' server.py

# Find a specific endpoint
grep -n 'def jira_pull_sync\|def commit_planning_session\|def update_project' server.py

# Where is X stored as config?
grep -n '"statusIsReleased"\|statusIsApproved' server.py

# DB schema
sed -n '/CREATE TABLE/,/);/p' server.py
```

---

## `roadmap.html` (~13,200 lines)

```bash
# Major render functions
grep -n '^function render' roadmap.html

# Find a function definition (broad)
grep -n 'function commitPlanningSession\|function loadActivities' roadmap.html

# Find a modal or major DOM id
grep -n 'id="capCalModalBg"\|id="planningView"\|id="dashboardView"' roadmap.html

# All API methods on the wrapper object
grep -n '^const API = {' roadmap.html
# Then read the object — every endpoint the frontend hits is in there.

# Top-level globals (state vars)
sed -n '/^const SK = /,/^let typeIgnoreConflicts/p' roadmap.html

# Status flag map usage — see which features check which flag
grep -n 'statusIsReleased\|statusIsApproved\|statusIsTesting' roadmap.html | head -30
```

---

## Approximate section anchors in `roadmap.html`

| Around line | Section |
|---|---|
| 1–800 | CSS |
| 800–1900 | HTML body + modals |
| 1900–2050 | Constants, `API` object, global state |
| 2050–2860 | Filtering, grouping, view dispatch |
| 2860–4310 | Gantt `render()` |
| 4311–4560 | Jira admin tab |
| 4560–5430 | Status / type / conflict admin |
| 5430–5760 | Capacity calendar |
| 5760–6630 | Admin tabs (devs, users, statuses, reasons) |
| 6630–7270 | Activity Center |
| 7270–7960 | Auth & login |
| 7960–8480 | View switching + Dashboard |
| 8480–9000 | Kanban |
| 9000–9700 | Planning view |
| 9700–end | Item modal/page + remaining modals |

These shift as the file grows — re-grep when in doubt.

---

## Syntax checks (run after any programmatic edit)

```bash
# Python
python3 -c "import ast; ast.parse(open('server.py').read()); print('server.py OK')"

# JS inside the HTML — assumes a single <script> block (current truth)
python3 -c "
import re
c = open('roadmap.html').read()
matches = re.findall(r'<script>(.*?)</script>', c, re.DOTALL)
print(f'{len(matches)} script block(s)')
open('/tmp/chk.js','w').write('\n'.join(matches))
" && node --check /tmp/chk.js && echo 'JS OK'
```

---

## Quick smoke test after a deploy

```bash
curl -s https://roadmap.frazil.app/api/version
# → {"version":"3.1.0", ...}

# Hit the HTML (Caddy + static serve)
curl -sI https://roadmap.frazil.app/ | head -3
```
