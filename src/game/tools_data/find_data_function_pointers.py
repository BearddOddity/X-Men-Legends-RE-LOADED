#!/usr/bin/env python3
"""find_data_function_pointers.py - function pointers that only exist in data.

Why this exists
---------------
The recompiler discovers functions by following calls from an entry point. A
function whose only reference is a dword sitting in a table - a vtable, an
initialiser list, a factory or callback array - is never reached, so it is never
lifted, so whatever it was supposed to initialise stays NULL for the whole run.

BLOCKER_005BB700.md documents one instance end to end: the subsystem table at
0x005BB700 is never written because its writer, sub_00216210, is referenced only
from 0x003f4478 as DATA. docs/PAGE_ZERO_CENSUS.md measures the scale - of 1,770
globals in 0x5Bxxxx that lifted code reads, 613 are never written by any lifted
code, and roughly 500 of those sit in structures the binary *does* write from
code that was never lifted.

This finds those entry points so they can be seeded.

How it decides
--------------
A dword anywhere outside .text that points into .text, at an address that is
not already a known function, and that begins with a plausible prologue. The
prologue test is what keeps this from seeding noise: any 4 bytes that happen to
look like a code address will pass the range check, and a table of floats or
string offsets produces a lot of those.

Alignment matters too. Real pointer tables are 4-byte aligned, and requiring
that removes most coincidental matches at the cost of missing packed structures.

This reports candidates, not proven functions. Seed them with
seed_missing_functions.py --from-list and check the result moved something.

Usage (from src/game/):
    py -3 tools_data/find_data_function_pointers.py
    py -3 tools_data/find_data_function_pointers.py --out cands.txt
    py -3 tools_data/find_data_function_pointers.py --min-refs 2
"""
import argparse
import json
import os
import sys
import bisect
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(GAME_DIR))
FUNCS_JSON = os.path.join(REPO, "tools", "disasm", "output", "functions.json")

sys.path.insert(0, HERE)
from whatis import load_sections  # noqa: E402

# MSVC x86 function openings. Kept deliberately narrow: a loose test turns a
# float table into hundreds of "functions".
PROLOGUES = (
    b"\x55\x8b\xec",          # push ebp; mov ebp, esp
    b"\x53\x8b\xdc",          # push ebx; mov ebx, esp
    b"\x56\x8b\xf1",          # push esi; mov esi, ecx
    b"\x8b\xff\x55\x8b\xec",  # mov edi, edi; push ebp; mov ebp, esp (hotpatch)
    b"\x53\x56\x57",          # push ebx; push esi; push edi
    b"\x56\x57",              # push esi; push edi
    b"\x83\xec",              # sub esp, imm8
    b"\x81\xec",              # sub esp, imm32
    b"\x55",                  # push ebp
    b"\x53",                  # push ebx
    b"\x56",                  # push esi
    b"\x57",                  # push edi
    b"\x33\xc0",              # xor eax, eax
    b"\x8b\x44\x24",          # mov eax, [esp+N]
    b"\x8b\x4c\x24",          # mov ecx, [esp+N]
    b"\xa1",                  # mov eax, [imm32]
)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", metavar="FILE",
                    help="write a seed_list-shaped JSON file, ready for "
                         "seed_missing_functions.py --from-list")
    ap.add_argument("--min-refs", type=int, default=1,
                    help="only report targets referenced at least N times from "
                         "data (default 1). Raising it favours real tables.")
    ap.add_argument("--limit", type=int, default=40,
                    help="how many to print (default 40)")
    args = ap.parse_args(argv)

    data, secs = load_sections()
    # load_sections returns hex strings; normalise once so the arithmetic below
    # is not silently doing string concatenation.
    def _i(v):
        return int(v, 16) if isinstance(v, str) else int(v)
    for s in secs:
        for k in ("va", "vsize", "raw", "rawsize"):
            s[k] = _i(s[k])
    text = next(s for s in secs if s["name"] == ".text")
    tlo, thi = text["va"], text["va"] + text["vsize"]

    known = json.load(open(FUNCS_JSON, encoding="utf-8"))
    known_vas = {int(f["start"], 16) for f in known}
    # Extents, so a pointer into the MIDDLE of a known function can be
    # rejected. This is the filter that matters: without it, UTF-16 text read
    # as pointers (0x00230022 is '"' and '#') lands on an arbitrary byte that
    # happens to match a one-byte prologue like 0x55, and the run fills with
    # addresses that are not function starts at all.
    extents = sorted((int(f["start"], 16), int(f["end"], 16)) for f in known)
    ext_starts = [e[0] for e in extents]

    def inside_known(va):
        i = bisect.bisect_right(ext_starts, va) - 1
        return i >= 0 and extents[i][0] < va < extents[i][1]

    refs = defaultdict(list)
    scanned = 0
    for s in secs:
        if s["name"] == ".text":
            continue
        raw, size, va = s["raw"], min(s["rawsize"], s["vsize"]), s["va"]
        for off in range(0, max(0, size - 3), 4):     # 4-byte aligned only
            word = int.from_bytes(data[raw + off:raw + off + 4], "little")
            scanned += 1
            if not (tlo <= word < thi):
                continue
            if word in known_vas or inside_known(word):
                continue
            toff = text["raw"] + (word - text["va"])
            head = data[toff:toff + 5]
            if not any(head.startswith(p) for p in PROLOGUES):
                continue
            refs[word].append(va + off)

    cands = sorted(refs.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    cands = [(t, sites) for t, sites in cands if len(sites) >= args.min_refs]

    print(f"scanned {scanned:,} aligned dwords outside .text")
    print(f"known functions: {len(known_vas):,}")
    print(f"data-only function-pointer candidates: {len(cands):,} "
          f"(min-refs={args.min_refs})")
    print()
    for target, sites in cands[:args.limit]:
        where = ", ".join(f"0x{a:08X}" for a in sites[:3])
        more = f" +{len(sites) - 3} more" if len(sites) > 3 else ""
        print(f"  0x{target:08X}  {len(sites):>3} data ref(s)  from {where}{more}")
    if len(cands) > args.limit:
        print(f"  ... {len(cands) - args.limit:,} more")

    if args.out:
        # seed_missing_functions.py --from-list reads {"addresses": [int, ...]}
        addrs = sorted(t for t, _ in cands)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"count": len(addrs), "addresses": addrs}, fh, indent=1)
        print(f"\nwrote {len(cands):,} addresses to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
