"""
strip_probes.py - remove temporary debug probes from gen/*.c.

Probes get added and removed several times per debugging session. Doing it by
hand risks leaving one behind (which quietly slows every run and pollutes
stderr.txt) or deleting a line too many - and a greedy regex already ate a
whole function in this tree once, surfacing much later as a link error.

Convention
----------
Every probe line carries a trailing `/* PROBE */` marker. Multi-line probes
open with `{ /* PROBE */` and close with `} /* PROBE */`; everything between
is removed with them. A probe's `#include <stdio.h> /* PROBE */` goes too.

Usage (from src/game/):
    py -3 tools_data/strip_probes.py            # report what would go
    py -3 tools_data/strip_probes.py --apply
"""
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(os.path.dirname(HERE), "src", "recomp", "gen")

MARK = "/* PROBE */"


def strip(lines):
    """Return (kept_lines, removed_lines). Block-aware, never greedy."""
    out, removed, depth = [], [], 0
    for line in lines:
        if depth:
            removed.append(line)
            if line.rstrip().endswith("} " + MARK) or line.rstrip() == "}" + MARK:
                depth = 0
            continue
        if MARK not in line:
            out.append(line)
            continue
        removed.append(line)
        # An opening brace on a marked line starts a block; a marked line that
        # also closes it (`{ ... } /* PROBE */`) does not.
        if "{" in line and not line.rstrip().endswith("} " + MARK):
            depth = 1
    if depth:
        raise SystemExit("unterminated probe block - refusing to write")
    return out, removed


def main(argv):
    apply = "--apply" in argv
    total = 0
    for path in sorted(glob.glob(os.path.join(GEN, "recomp_*.c"))):
        text = open(path, encoding="utf-8", errors="ignore").read()
        if MARK not in text:
            continue
        lines = text.split("\n")
        kept, removed = strip(lines)
        total += len(removed)
        print(f"{os.path.basename(path)}: {len(removed)} probe line(s)")
        for r in removed:
            print(f"    - {r.strip()[:90]}")
        if apply:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("\n".join(kept))
    if not total:
        print("no probes found - tree is clean")
    elif apply:
        print(f"\nremoved {total} line(s)")
    else:
        print(f"\n{total} line(s) would be removed; re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
