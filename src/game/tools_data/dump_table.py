#!/usr/bin/env python3
"""Dump a range of Xbox VAs as dwords, naming each one.

Function-pointer tables - CRT initialiser lists, vtables, jump tables - are
where a lot of this port's mysteries live, because the recompiled code calls
through them and a single bad entry lands somewhere absurd. Reading them by
hand meant converting VA to file offset, seeking, and then looking each value
up separately.

Every entry is classified the same way an indirect call would see it: a real
function, an address inside a function, data, or nothing mapped at all.

Run from src/game/:
    py -3 tools_data/dump_table.py 0x0044A590 0x0044B0A4
    py -3 tools_data/dump_table.py 0x0044A590 --count 32
    py -3 tools_data/dump_table.py 0x0044A590 0x0044B0A4 --find 0x003432FC
"""
import argparse
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(GAME))
sys.path.insert(0, REPO)

from tools.recomp import config  # noqa: E402

XBE = os.path.join(GAME, "game", "default.xbe")
FUNCS = os.path.join(GAME, "seeded_functions.json")


def load_functions():
    """Return a sorted list of (start, end, name)."""
    if not os.path.exists(FUNCS):
        return []
    with open(FUNCS, encoding="utf-8") as f:
        data = json.load(f)
    entries = data["functions"] if isinstance(data, dict) else data
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        a = e.get("address") or e.get("start") or e.get("va")
        if a is None:
            continue
        if isinstance(a, str):
            a = int(a, 16)
        size = e.get("size") or e.get("length") or 0
        name = e.get("name") or ("sub_%08X" % a)
        out.append((a, a + size, name))
    out.sort()
    return out


def classify(va, funcs, lo, hi):
    if va == 0:
        return "null"
    if va == 0xFFFFFFFF:
        return "-1 (end marker)"
    if not (lo <= va < hi):
        return "NOT IN IMAGE"
    import bisect
    i = bisect.bisect_right(funcs, (va, 0xFFFFFFFF, "")) - 1
    if i >= 0:
        start, end, name = funcs[i]
        if va == start:
            return name
        if start < va < end:
            return "%s+0x%X  <-- MID-FUNCTION" % (name, va - start)
    return "data or unknown code"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start")
    ap.add_argument("end", nargs="?")
    ap.add_argument("--count", type=int, default=0,
                    help="number of dwords (used when end is omitted)")
    ap.add_argument("--find", help="only show entries equal to this value")
    ap.add_argument("--summary", action="store_true",
                    help="counts per class instead of every entry")
    a = ap.parse_args()

    start = int(a.start, 16 if a.start.lower().startswith("0x") else 10)
    if a.end:
        end = int(a.end, 16 if a.end.lower().startswith("0x") else 10)
    else:
        end = start + 4 * (a.count or 16)

    want = None
    if a.find:
        want = int(a.find, 16 if a.find.lower().startswith("0x") else 10)

    blob = open(XBE, "rb").read()
    funcs = load_functions()

    config.configure_from_xbe(XBE)
    lo = min(va for _, va, _, _ in config.SECTIONS)
    hi = max(va + size for _, va, size, _ in config.SECTIONS)

    print("table 0x%08X .. 0x%08X   (%d entries)"
          % (start, end, (end - start) // 4))
    print("image 0x%08X .. 0x%08X   (%d known functions)" % (lo, hi, len(funcs)))
    print()

    counts, shown = {}, 0
    for va in range(start, end, 4):
        off = config.va_to_file_offset(va)
        if off is None or off + 4 > len(blob):
            val, cls = None, "UNMAPPED VA"
        else:
            val = struct.unpack("<I", blob[off:off + 4])[0]
            cls = classify(val, funcs, lo, hi)
        key = cls.split("+")[0] if "MID-FUNCTION" not in cls else "MID-FUNCTION"
        key = cls if cls in ("null", "-1 (end marker)", "NOT IN IMAGE",
                             "data or unknown code", "UNMAPPED VA") else key
        counts[key] = counts.get(key, 0) + 1

        if want is not None and val != want:
            continue
        if a.summary:
            continue
        shown += 1
        print("  [%4d] 0x%08X -> %s   %s"
              % ((va - start) // 4, va,
                 "????????" if val is None else "0x%08X" % val, cls))

    if a.summary or want is not None:
        print()
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print("  %-40s %5d" % (k, v))
    if want is not None and shown == 0:
        print("\n0x%08X does not appear in this table" % want)


if __name__ == "__main__":
    main()
