"""Which 'broken' walls are actually still proven broken?

A wall is only demonstrably passed if the boot still runs PAST the point it
sits at. Today the boot stops at 230 kernel calls. Any wall that was last seen
at a deeper point than that is not passed today - it is simply not reached,
and its repair is untested by the current build.
"""
import json
import os
import collections

rows = json.load(open(os.path.join("tools_data", "progress.json"), encoding="utf-8"))
if isinstance(rows, dict):
    rows = rows.get("entries", rows)

def kc(r):
    v = r.get("kernel_calls")
    return v if isinstance(v, int) else 0

CUR = kc(rows[-1])
CUR_SITE = rows[-1].get("crash_in")
CLIP = 4000

# depth of each wall = the highest NON-CLIPPED kernel count recorded while at it
depth = collections.defaultdict(int)
last_at = {}
for i, r in enumerate(rows):
    s = r.get("crash_in") or "(none)"
    k = kc(r)
    if k < CLIP:
        depth[s] = max(depth[s], k)
    last_at[s] = i

print("current: %s at %d kernel calls" % (CUR_SITE, CUR))
print()
deeper = [(s, d, last_at[s]) for s, d in depth.items()
          if s not in ("(none)", CUR_SITE) and d > CUR]
shallow = [(s, d, last_at[s]) for s, d in depth.items()
           if s not in ("(none)", CUR_SITE) and 0 < d <= CUR]

print("=" * 74)
print("NOT PROVEN BROKEN - sat deeper than where we stop today (%d)" % len(deeper))
print("=" * 74)
print("These were passed once, at a depth the current build never reaches.")
print("Whether the repair still holds is untested.")
print()
for s, d, i in sorted(deeper, key=lambda x: -x[1]):
    print("  %-34s depth %-6d last seen #%-4d %s" % (s, d, i + 1, rows[i].get("date")))

print()
print("=" * 74)
print("STILL PROVEN BROKEN - the boot runs past these every run (%d)" % len(shallow))
print("=" * 74)
for s, d, i in sorted(shallow, key=lambda x: -x[1])[:20]:
    print("  %-34s depth %-6d last seen #%-4d %s" % (s, d, i + 1, rows[i].get("date")))
if len(shallow) > 20:
    print("  ... and %d more" % (len(shallow) - 20))

clipped = sorted({s for s, d in depth.items() if d == 0 and s != "(none)"})
if clipped:
    print()
    print("walls only ever seen on a clipped/spin run (depth unknown): %d" % len(clipped))
    for s in clipped[:8]:
        print("   ", s)
