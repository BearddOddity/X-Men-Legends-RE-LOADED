#!/usr/bin/env python3
"""Find lifted functions that fall off the end instead of terminating.

The lifter splits a function at internal branch targets into separate C
functions. The fragment ending just before the split point must hand control to
the fragment at the split point. When it doesn't, the C function simply ends -
so the whole remainder of the real function, including its `pop`/`ret`
epilogue, never runs.

The caller then sees:
  - a stack leak of (dummy return address + whatever the prologue pushed)
  - callee-saved registers (ebx/esi/edi/ebp) never restored

which surfaces later as garbage pointers, ICALL targets in stack memory, and
crashes nowhere near the cause.

Found via sub_00202CE1, which ends on an indirect call and drops the edge to
sub_00202D14, leaking 12 bytes and destroying the caller's esi.

    py -3 tools_data/find_dropped_fallthrough.py
    py -3 tools_data/find_dropped_fallthrough.py --list
"""
import argparse
import glob
import os
import re
import sys

GAME_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = os.path.join(GAME_DIR, "src", "recomp", "gen")

FUNC = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)$")

# A properly terminated fragment ends by modelling `ret`, tail-calling the next
# fragment, or looping. Anything else means the edge was dropped.
TERMINATORS = (
    "return;",          # covers `esp += 4; return;` and `sub_X(); return;`
    "goto ",
)


def classify(body):
    """body: list of source lines inside one function. -> None or reason"""
    for line in reversed(body):
        s = line.strip()
        # Skip blanks, comments, bare braces, labels, and the preprocessor
        # trailer the FPU macros leave behind (#undef fp_push etc.) - those
        # sit *after* the real `return` and would otherwise read as the last
        # statement.
        if (not s or s.startswith("/*") or s.startswith("*")
                or s.startswith("#") or s in ("{", "}")
                or re.match(r"^loc_[0-9A-Fa-f]+: ;$", s)):
            continue
        if any(t in s for t in TERMINATORS):
            return None
        # last meaningful statement is not a terminator
        if "RECOMP_ICALL" in s:
            return "ends on an indirect call"
        if re.match(r"^sub_[0-9A-Fa-f]+\(\);", s):
            return "ends on a direct call"
        return "ends on a plain statement"
    return "empty body"


def main():
    ap = argparse.ArgumentParser(prog="find_dropped_fallthrough")
    ap.add_argument("--list", action="store_true", help="print every hit")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(GEN, "recomp_*.c")))
    if not files:
        sys.exit(f"no generated files under {GEN}")

    hits, total = [], 0
    for path in files:
        if path.endswith("recomp_stubs_unresolved.c"):
            continue          # stubs are a separate, known class
        name, body, depth = None, [], 0
        for line in open(path, encoding="utf-8", errors="replace"):
            m = FUNC.match(line.rstrip("\n"))
            if m and depth == 0:
                name, body, depth = m.group(1), [], 0
                continue
            if name is None:
                continue
            depth += line.count("{") - line.count("}")
            body.append(line)
            if depth <= 0 and body and "}" in line:
                total += 1
                why = classify(body[:-1])
                if why:
                    hits.append((os.path.basename(path), name, why))
                name = None

    print(f"scanned {total} lifted function(s) across {len(files)-1} file(s)")
    print(f"dropped fall-through edges: {len(hits)}")
    if total:
        print(f"  = {100.0*len(hits)/total:.2f}% of functions")

    by_reason = {}
    for _, _, why in hits:
        by_reason[why] = by_reason.get(why, 0) + 1
    for why, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"    {n:>6}  {why}")

    if args.list:
        print()
        for f, n, why in hits:
            print(f"  {f:<24} {n}  ({why})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
