#!/usr/bin/env python3
"""Resolve the [PAGE0] census RVAs to recompiled function names.

The census (src/kernel/xbox_page_zero_trap.c, built with
-DRECOMP_TRAP_PAGE_ZERO=ON) prints one line per distinct faulting RIP with an
`rva=` field. This turns those into `sub_XXXXXXXX + 0xN`.

Resolve against the map of the SAME build that produced the log - RVAs differ
between build/ and build_page0/. The map's THIRD address column is `Rva+Base`;
RVA = that column minus the image base. Parsing the FIRST column instead is
off by the section VA and silently shifts every lookup (ledger #59).

    py -3 tools_data/resolve_page0_census.py page0_stderr.txt \\
        --map build_page0/xmen_legends_recomp.map
"""

import argparse
import bisect
import re
import sys

MAP_ROW = re.compile(
    r"^\s*[0-9a-fA-F]{4}:[0-9a-fA-F]{8}\s+(\S+)\s+([0-9a-fA-F]{16})\s"
)
CENSUS_LINE = re.compile(r"rva=0x([0-9A-Fa-f]+)")


def load_map(path, image_base):
    syms = []
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            m = MAP_ROW.match(line)
            if not m:
                continue
            name, rva_base = m.group(1), int(m.group(2), 16)
            if rva_base < image_base:
                continue
            syms.append((rva_base - image_base, name))
    syms.sort()
    if not syms:
        sys.exit(f"no symbols parsed from {path} - wrong map format?")
    return [s[0] for s in syms], [s[1] for s in syms]


def resolve(addrs, names, rva):
    i = bisect.bisect_right(addrs, rva) - 1
    if i < 0:
        return None, 0
    return names[i], rva - addrs[i]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", help="stderr log containing [PAGE0] lines")
    ap.add_argument("--map", required=True, help="linker map of the SAME build")
    ap.add_argument("--image-base", default="0x140000000")
    args = ap.parse_args()

    image_base = int(args.image_base, 16)
    addrs, names = load_map(args.map, image_base)

    printed = 0
    with open(args.log, "r", errors="replace") as fh:
        for line in fh:
            if "[PAGE0]" not in line:
                continue
            m = CENSUS_LINE.search(line)
            if not m:
                continue
            rva = int(m.group(1), 16)
            sym, off = resolve(addrs, names, rva)
            text = line.rstrip("\n")
            if sym is None or rva > addrs[-1] + 0x100000:
                print(f"{text}   -> (outside this image)")
            else:
                print(f"{text}   -> {sym} + 0x{off:X}")
            printed += 1

    if printed == 0:
        print("no [PAGE0] lines with an rva= field found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
