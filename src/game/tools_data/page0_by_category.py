#!/usr/bin/env python3
"""page0_by_category.py - who reads guest page zero, grouped by what they are.

The page-zero trap census names sites by host RVA. On its own that is a list of
addresses; joined against the linker map and the lifter's own Category
annotation it becomes a question you can act on - is the null-pointer traffic
coming from code we intend to keep, or from a subsystem we plan to replace?

    python3 tools_data/page0_by_category.py
    python3 tools_data/page0_by_category.py --list

Reads page0_stderr.txt, build/page0.map and the gen/ headers.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import resolve_rva          # noqa: E402
import census_categories    # noqa: E402

LOG = os.path.join(GAME, "page0_stderr.txt")
MAP = os.path.join(GAME, "build", "page0.map")

SITE = re.compile(r"\[PAGE0\] site #(\d+): first read of guest VA 0x([0-9A-Fa-f]+)"
                  r"\s+rip=\S+\s+rva=0x([0-9A-Fa-f]+)")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)

    if not os.path.exists(LOG):
        sys.exit("no page0_stderr.txt - run run_page0.bat first")
    rvas, names, objs, base = resolve_rva.load_map(MAP)
    funcs = census_categories.scan()

    rows = []
    for m in SITE.finditer(open(LOG, errors="replace").read()):
        site, va, rva = int(m.group(1)), int(m.group(2), 16), int(m.group(3), 16)
        got = resolve_rva.resolve(rvas, names, objs, rva)
        fn = got[0] if got else "?"
        cat = funcs.get(fn, {}).get("category", "(unknown)")
        rows.append((site, va, fn, cat))

    by = {}
    for site, va, fn, cat in rows:
        by.setdefault(cat, []).append((site, va, fn))

    print(f"{len(rows)} page-zero read site(s)\n")
    print(f"{'category':<24}{'sites':>7}{'first':>8}")
    print("-" * 39)
    for cat, items in sorted(by.items(), key=lambda kv: -len(kv[1])):
        print(f"{cat:<24}{len(items):>7}{min(i[0] for i in items):>8}")

    if a.list:
        print()
        for site, va, fn, cat in rows:
            print(f"  #{site:<4} guest 0x{va:08X}  {fn:<22} [{cat}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
