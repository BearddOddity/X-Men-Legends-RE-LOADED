#!/usr/bin/env python3
"""fix_stale_flags.py - repair the deferred-flag miscompiles that its sibling finds.

The bug
-------
find_stale_flag_tests.py explains the shape in full; the short version is that
the lifter turns

    cmp ecx, eax        ; flags computed HERE
    mov ecx, esi        ; ecx clobbered
    je  target          ; still branches on the flags from before the clobber

into

    (void)0;                    /* cmp ecx, eax - flags set for next jcc */
    ecx = esi;
    if (CMP_EQ(ecx, eax)) goto target;

which re-reads `ecx` after it was overwritten and so tests the wrong value.

The fix
-------
Flags are a snapshot taken at the `cmp`. Reproduce that literally: copy the
clobbered registers into temporaries on the line where the flags were set, and
have the `jcc` test the temporaries.

    uint32_t _sf21439_ecx = ecx;
    (void)0;                    /* cmp ecx, eax - flags set for next jcc */
    ecx = esi;
    if (CMP_EQ(_sf21439_ecx, eax)) goto target;

Only registers the detector flags as clobbered are substituted, and only inside
the `if (...)` condition - never in the branch body, which legitimately wants
the current value. Registers that are not clobbered read the same either way
and are left alone, so the diff stays small enough to eyeball.

Why the temp is named after the line
------------------------------------
Line numbers are unique within a file and a function never spans files, so the
name cannot collide with another site in the same scope, and the name says
where to look when one shows up in a debugger.

Scope
-----
This rewrites gen/*.c, which is regenerated. Run it as a pipeline step after
seeding (alongside stub_overridden.py), not as a one-off: it is idempotent, so
re-running it on an already-fixed tree is a no-op.

Usage (from src/game/):
    py -3 tools_data/fix_stale_flags.py                      # what would change
    py -3 tools_data/fix_stale_flags.py --only loc_001F0AA9 --apply
    py -3 tools_data/fix_stale_flags.py --apply              # the whole class

Staging is deliberate. 447 sites is too many to land in one step and still know
which one moved the needle; --only exists so a site can be proven on its own
first. The project log records a 13-at-once seed batch that had to be reverted
wholesale for exactly this reason.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_stale_flag_tests as det

GEN = det.GEN
JCC = det.JCC
LABEL = re.compile(r"^(loc_[0-9A-Fa-f]{8}|sub_[0-9A-Fa-f]{8})\b")
DECL = "uint32_t "


def temp_name(line_no, reg):
    return f"_sf{line_no}_{reg}"


def label_for(lines, idx):
    """Nearest preceding loc_/sub_ label - how a site is named in the log."""
    for j in range(idx, -1, -1):
        m = LABEL.match(lines[j])
        if m:
            return m.group(1)
    return "?"


def plan(only=None):
    """Return {path: [edit, ...]} for every site that still needs fixing.

    `only` is a label, a "file.c:line", or a collection of either. A bisect
    needs to select arbitrary subsets, not just one site at a time.
    """
    if isinstance(only, str):
        only = {only}
    elif only is not None:
        only = set(only)
    _, hits = det.scan()
    by_file, cache = {}, {}
    for h in hits:
        path = os.path.join(GEN, h["file"])
        if path not in cache:
            cache[path] = open(path, encoding="utf-8",
                               errors="ignore").read().split("\n")
        lines = cache[path]
        i = h["line"] - 1                      # detector reports 1-based
        label = label_for(lines, i)

        if only and not ({label, f"{h['file']}:{h['line']}"} & only):
            continue

        # Idempotency: the marker is the declaration we ourselves inserted.
        if i > 0 and temp_name(h["line"], h["regs"][0]) in lines[i - 1]:
            continue

        # Find the jcc line the detector paired with this site.
        jcc_idx = None
        for j in range(i + 1, min(i + 10, len(lines))):
            if JCC.match(lines[j]):
                jcc_idx = j
                break
        if jcc_idx is None:
            continue

        m = JCC.match(lines[jcc_idx])
        cond, start, end = m.group(1), m.start(1), m.end(1)
        new_cond = cond
        for reg in h["regs"]:
            new_cond = re.sub(rf"\b{reg}\b", temp_name(h["line"], reg), new_cond)
        if new_cond == cond:
            continue                            # nothing to substitute

        indent = re.match(r"\s*", lines[i]).group(0)
        decls = indent + " ".join(
            f"{DECL}{temp_name(h['line'], r)} = {r};" for r in h["regs"])
        by_file.setdefault(path, []).append({
            "defer_idx": i,
            "jcc_idx": jcc_idx,
            "decls": decls,
            "jcc_new": lines[jcc_idx][:start] + new_cond + lines[jcc_idx][end:],
            "label": label,
            "line": h["line"],
            "regs": h["regs"],
            "defer": h["defer"],
            "jcc_old": lines[jcc_idx].strip(),
        })
    return by_file, cache


def apply(by_file, cache):
    """Rewrite bottom-up so earlier edits do not shift later line indexes."""
    for path, edits in by_file.items():
        lines = cache[path]
        for e in sorted(edits, key=lambda e: e["defer_idx"], reverse=True):
            lines[e["jcc_idx"]] = e["jcc_new"]
            lines.insert(e["defer_idx"], e["decls"])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="fix_stale_flags", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", metavar="LABELS",
                    help="comma-separated sites: loc_001F0AA9,recomp_0014.c:21439")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    sel = set(a.only.replace(" ", "").split(",")) if a.only else None
    by_file, cache = plan(sel)
    total = sum(len(v) for v in by_file.values())
    if not total:
        print("nothing to fix" + (f" matching {a.only}" if a.only else "")
              + " - already applied, or the detector reports none")
        return 0

    for path, edits in sorted(by_file.items()):
        print(f"{os.path.basename(path)}")
        for e in sorted(edits, key=lambda e: e["line"]):
            print(f"  {e['label']} (line {e['line']})  snapshot "
                  f"{', '.join(e['regs'])}")
            print(f"    - {e['jcc_old']}")
            print(f"    + {e['jcc_new'].strip()}")
    print(f"\n{total} site(s)")

    if not a.apply:
        print("dry run - pass --apply to write")
        return 0
    apply(by_file, cache)
    print("applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
