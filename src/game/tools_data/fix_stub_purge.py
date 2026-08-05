#!/usr/bin/env python3
"""Make every unresolved stub pop the fake return address, and add missing ones.

Every call site in the generated code pushes a fake return address that the
callee is responsible for popping. A stub with an empty body pops nothing, so
each call leaks 4 bytes of simulated stack. That is the floor of the damage -
a stub also owes any `ret N` argument purge, and one that terminates a lifter
tail-call chain swallows the register restores for the whole chain.

This does two things:

  1. Rewrites existing empty stub bodies to `g_esp += 4;`.
  2. Adds a stub for any name declared in recomp_funcs.h that nothing defines,
     which happens after seeding: a newly seeded body introduces call targets
     no earlier pass had ever seen.

Both are stopgaps. The real repair is seed_missing_functions.py --stubs.
The translator now emits the purge itself, so (1) is only needed on a tree
generated before that change.

Run from src/game/:
    py -3 tools_data/fix_stub_purge.py            # report
    py -3 tools_data/fix_stub_purge.py --apply
"""
import argparse
import os
import re
import sys

GEN = os.path.join("src", "recomp", "gen")
STUBS = os.path.join(GEN, "recomp_stubs_unresolved.c")
FUNCS_H = os.path.join(GEN, "recomp_funcs.h")
SRC = "src"

EMPTY_RE = re.compile(
    r"^void (sub_[0-9A-Fa-f]+)\(void\) \{ (/\* 0x[0-9A-Fa-f]+: not detected \*/) \}$")
ANY_STUB_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\) \{")
DECL_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\);")
DEF_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*$")
ONELINE_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*\{")


def defined_names():
    """Every function with a body anywhere in the tree."""
    out = set()
    for d in (GEN, SRC):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".c"):
                continue
            for line in open(os.path.join(d, fn), encoding="utf-8",
                             errors="replace"):
                m = DEF_RE.match(line) or ONELINE_RE.match(line)
                if m:
                    out.add(m.group(1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(STUBS):
        sys.exit("run from src/game/ - no %s" % STUBS)

    lines = open(STUBS, encoding="utf-8", errors="replace").read().split("\n")

    fixed = 0
    for i, l in enumerate(lines):
        m = EMPTY_RE.match(l)
        if m:
            lines[i] = "void %s(void) { g_esp += 4; %s }" % (m.group(1), m.group(2))
            fixed += 1

    # Anything declared but never defined needs a stub, or the link fails.
    declared = set()
    if os.path.exists(FUNCS_H):
        for line in open(FUNCS_H, encoding="utf-8", errors="replace"):
            m = DECL_RE.match(line)
            if m:
                declared.add(m.group(1))
    missing = sorted(declared - defined_names())

    if missing:
        lines.append("")
        lines.append("/* Declared but never defined - discovered after seeding "
                     "introduced new call targets. */")
        for n in missing:
            lines.append("void %s(void) { g_esp += 4; /* %s: no body found */ }"
                         % (n, n))
        lines.append("")

    print("stub bodies given a return-address purge: %d" % fixed)
    print("declared-but-undefined names stubbed:     %d" % len(missing))
    for n in missing[:12]:
        print("    %s" % n)
    if len(missing) > 12:
        print("    ... %d more" % (len(missing) - 12))

    if not a.apply:
        print("\ndry run - pass --apply to write")
        return

    if fixed or missing:
        with open(STUBS, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines))
        print("\nwrote %s" % STUBS)
    else:
        print("\nnothing to do")


if __name__ == "__main__":
    main()
