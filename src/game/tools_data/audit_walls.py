"""Audit every wall in the recorded history.

A wall is a distinct crash site. It counts as PASSED when the boot later stops
somewhere else. It counts as a REGRESSION when it is passed and then returned
to - the fix stopped holding, or a later change undid it.

Also flags walls whose only exit was a diagnostic bypass, since those are
passed in the counter but not in the code.
"""
import json
import os
import re
import collections

rows = json.load(open(os.path.join("tools_data", "progress.json"), encoding="utf-8"))
if isinstance(rows, dict):
    rows = rows.get("entries", rows)

# Collapse consecutive entries at the same site into one "visit".
visits = []           # (site, first_index, last_index)
for i, r in enumerate(rows):
    site = r.get("crash_in") or "(none)"
    if visits and visits[-1][0] == site:
        visits[-1][2] = i
    else:
        visits.append([site, i, i])

seen = collections.defaultdict(list)
for site, a, b in visits:
    seen[site].append((a, b))

print("entries: %d   distinct crash sites: %d   visit-blocks: %d"
      % (len(rows), len(seen), len(visits)))
print()

REVISIT = {s: v for s, v in seen.items() if len(v) > 1 and s != "(none)"}
print("=" * 74)
print("REGRESSIONS - a wall left and later returned to (%d sites)" % len(REVISIT))
print("=" * 74)
for site, vs in sorted(REVISIT.items(), key=lambda kv: -len(kv[1])):
    span = " ".join("#%d-%d" % (a + 1, b + 1) for a, b in vs)
    gap = vs[-1][0] - vs[0][1]
    print("  %-34s %d visits   %s" % (site, len(vs), span))
    print("      %s  ..  %s   (%d entries between first exit and last return)"
          % (rows[vs[0][0]].get("date"), rows[vs[-1][1]].get("date"), gap))

print()
print("=" * 74)
print("CURRENT AND RECENT")
print("=" * 74)
for site, a, b in visits[-8:]:
    print("  #%-4d-%-4d %-34s %s" % (a + 1, b + 1, site, rows[b].get("date")))

# A wall whose exit entry mentions a guard/bypass rather than a fix.
BYPASS = re.compile(r"guard|bypass|skip|clamp|widen", re.I)
FIX = re.compile(r"seed|fix|restore|_icall_esp|lifter|carry|fall-?through", re.I)
print()
print("=" * 74)
print("HOW EACH WALL WAS LEFT - the entry that moved off it")
print("=" * 74)
soft = 0
for site, vs in sorted(seen.items(), key=lambda kv: kv[1][0][0]):
    if site == "(none)":
        continue
    last_exit = vs[-1][1]
    if last_exit + 1 >= len(rows):
        continue                       # still here
    e = rows[last_exit]
    txt = (e.get("message") or "") + " " + (e.get("note") or "")
    tag = "bypass" if BYPASS.search(txt) and not FIX.search(txt) else \
          "mixed" if BYPASS.search(txt) and FIX.search(txt) else \
          "fix" if FIX.search(txt) else "unclear"
    if tag in ("bypass", "unclear"):
        soft += 1
        print("  %-34s left at #%-4d %-8s %s"
              % (site, last_exit + 1, tag, (e.get("message") or "")[:60]))
print()
print("%d wall(s) left by a bypass or an unclear change" % soft)
