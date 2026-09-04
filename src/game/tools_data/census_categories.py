#!/usr/bin/env python3
"""census_categories.py - what the 28k lifted functions ARE, and which run.

Why this exists
---------------
The question "is there code we do not need to boot, that we could stub and
document instead of debugging?" needs an inventory before it needs a decision.
The lifter already annotates every function with a Category, a size and an
original address range, and nothing was reading that. This turns it into a table
of what exists, how big it is, and how much of it the current boot touches.

    python3 tools_data/census_categories.py
    python3 tools_data/census_categories.py --category game_network --list

READ THE WARNING IT PRINTS before stubbing anything. This project has reverted
three "faithful" repairs that removed a placeholder, because the placeholder was
load-bearing: code that returns early is survivable, and the same code running
properly then reaches its own defect. A category being unreached on THIS boot
means it is unreached before the boot dies, not that it is unnecessary.

Self-check:  python3 tools_data/census_categories.py --selftest
"""
import argparse
import glob
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
GEN = os.path.join(GAME, "src", "recomp", "gen")
LOG = os.path.join(GAME, "stderr.txt")

# /**
#  * sub_001A06E8
#  * Original: 0x001A06E8 - 0x001A0A97 (943 bytes, 297 insns)
#  * Category: game_network
HDR = re.compile(r"^\s*\*\s*(sub_[0-9A-Fa-f]+)\s*$")
ORIG = re.compile(r"^\s*\*\s*Original: 0x([0-9A-Fa-f]+) - 0x([0-9A-Fa-f]+) "
                  r"\((\d+) bytes")
CAT = re.compile(r"^\s*\*\s*Category: (\S+)")


def scan(gen_dir=GEN):
    """(name -> {va, size, category}) for every documented function."""
    out = {}
    for src in sorted(glob.glob(os.path.join(gen_dir, "recomp_*.c"))):
        name = va = size = cat = None
        with open(src, errors="replace") as fh:
            for line in fh:
                m = HDR.match(line)
                if m:
                    name, va, size, cat = m.group(1), None, None, None
                    continue
                if name is None:
                    continue
                m = ORIG.match(line)
                if m:
                    va, size = int(m.group(1), 16), int(m.group(3))
                    continue
                m = CAT.match(line)
                if m:
                    cat = m.group(1)
                    continue
                if line.startswith("void ") and va is not None:
                    out[name] = {"va": va, "size": size,
                                 "category": cat or "(none)"}
                    name = None
    return out


def reached_vas(log=LOG):
    if not os.path.exists(log):
        return set()
    text = open(log, errors="replace").read()
    return {int(v, 16) for v in re.findall(r"\[COVERAGE-VA\] 0x([0-9A-Fa-f]+)", text)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--category")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()

    funcs = scan()
    hit = reached_vas()
    by = defaultdict(lambda: {"n": 0, "bytes": 0, "reached": 0})
    for name, f in funcs.items():
        b = by[f["category"]]
        b["n"] += 1
        b["bytes"] += f["size"] or 0
        if f["va"] in hit:
            b["reached"] += 1

    print(f"{len(funcs)} documented functions; {len(hit)} VAs reached in the "
          f"current run\n")
    print(f"{'category':<24}{'funcs':>8}{'KB':>10}{'reached':>9}")
    print("-" * 51)
    for cat, b in sorted(by.items(), key=lambda kv: -kv[1]["bytes"]):
        print(f"{cat:<24}{b['n']:>8}{b['bytes']/1024:>10.0f}{b['reached']:>9}")

    if a.category:
        sel = sorted((f["va"], n) for n, f in funcs.items()
                     if f["category"] == a.category)
        print(f"\n{len(sel)} function(s) in {a.category}, "
              f"{sum(1 for va, n in sel if va in hit)} reached")
        if a.list:
            for va, n in sel:
                print(f"    0x{va:08X}  {n}  "
                      f"{'REACHED' if va in hit else ''}")

    print("\nWARNING - read before stubbing anything.")
    print("  'reached' counts what this boot touches BEFORE IT DIES. A category")
    print("  at 0 is unreached, not unnecessary: the boot stops in the C runtime")
    print("  initialisers, so almost nothing has had a chance to run.")
    print("  Three faithful repairs have already been reverted here because the")
    print("  placeholder they removed was load-bearing. Stub only with evidence")
    print("  that a specific function is inert, never on a category total.")
    return 0


def selftest():
    import tempfile
    sample = (
        "/**\n"
        " * sub_00001000\n"
        " * Original: 0x00001000 - 0x00001100 (256 bytes, 40 insns)\n"
        " * Category: game_network\n"
        " */\n"
        "void sub_00001000(void)\n"
        "{\n"
        "}\n"
        "/**\n"
        " * sub_00002000\n"
        " * Original: 0x00002000 - 0x00002010 (16 bytes, 4 insns)\n"
        " */\n"
        "void sub_00002000(void)\n"
        "{\n"
        "}\n"
    )
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "recomp_0000.c"), "w") as fh:
        fh.write(sample)
    f = scan(d)
    assert set(f) == {"sub_00001000", "sub_00002000"}, sorted(f)
    assert f["sub_00001000"]["category"] == "game_network"
    assert f["sub_00001000"]["size"] == 256
    assert f["sub_00001000"]["va"] == 0x1000
    # a function with no Category line must still be counted, not dropped
    assert f["sub_00002000"]["category"] == "(none)"
    print("selftest ok - category, size and VA parsed; uncategorised kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
