#!/usr/bin/env python3
"""unwritten.py - guest globals the game READS but nothing ever WRITES.

Why this exists
---------------
On 2026-08-06 the boot wall was chased for a day and a half. What finally turned
up a real missing initialisation was noticing that 0x006F12E0 had four readers in
the generated code and no writer anywhere. That is the CRT's __active_heap: on
hardware __heap_init picks a heap mode before anything allocates, so the value
can never be zero. Here it sat at zero all run and every allocation took an
uninitialised code path.

That observation is mechanical, so it should not have taken a day. A global with
readers and no writer is either set by something outside the XBE - the kernel,
the CRT, firmware - or set by lifted code that never runs. Both are missing
initialisation, and both are invisible at runtime because reading zero looks
exactly like reading a legitimate zero.

This is also work that does NOT need the boot to get anywhere, which matters: the
boot currently stops early, so the usual measure-and-compare loop cannot reach
most of the game. Static checks like this one keep making progress while the
runtime is stuck behind a wall.

Ranking
-------
By reader count, then by how the value is used. A global compared against several
distinct constants is a mode or state selector, and those do the most damage when
left at zero - which is exactly the __active_heap shape. A global only ever
compared against zero is far more likely to be a legitimate "not set yet" flag.

What it cannot know
-------------------
Whether a writer exists outside the generated code: the kernel shims, the manual
overrides, and main.c all write guest memory and are searched too, but a write
through a computed pointer is invisible to any static scan. So this produces
CANDIDATES. Every one needs the same treatment __active_heap got - read the code
that uses it, work out what the value should be, and measure the change.

Usage (from src/game/):
    py -3 tools_data/unwritten.py                # ranked candidates
    py -3 tools_data/unwritten.py --min-readers 3
    py -3 tools_data/unwritten.py --addr 0x6F12E0 # everything about one global
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
SRC = os.path.join(GAME, "src")
ROOT = os.path.dirname(os.path.dirname(GAME))      # repo root, for kernel/ etc.

# MEM32(0xADDR) / MEM16 / MEM8 with a literal address and no register in it.
READ = re.compile(r"MEM(?:8|16|32)\((0x[0-9A-Fa-f]{5,8})\)")
WRITE = re.compile(r"MEM(?:8|16|32)\((0x[0-9A-Fa-f]{5,8})\)\s*=(?!=)")
# The comparison constant, so a mode selector can be told from a null check.
CMP = re.compile(r"(?:CMP_\w+|TEST_\w+)\(MEM(?:8|16|32)\((0x[0-9A-Fa-f]{5,8})\)"
                 r"\s*,\s*(0x[0-9A-Fa-f]+|\d+)")

# Guest data lives here; anything below is code and above is unmapped.
LO, HI = 0x00400000, 0x00800000


def scan_gen():
    reads, writes, cmps, sites = defaultdict(int), defaultdict(int), defaultdict(set), defaultdict(list)
    for path in sorted(glob.glob(os.path.join(GEN, "recomp_*.c"))):
        base = os.path.basename(path)
        for n, line in enumerate(open(path, encoding="utf-8", errors="ignore"), 1):
            for m in WRITE.finditer(line):
                writes[int(m.group(1), 16)] += 1
            # A write also matches READ, so subtract those below rather than
            # trying to make one regex do both jobs.
            for m in READ.finditer(line):
                a = int(m.group(1), 16)
                reads[a] += 1
                if len(sites[a]) < 6:
                    sites[a].append(f"{base}:{n}")
            for m in CMP.finditer(line):
                cmps[int(m.group(1), 16)].add(m.group(2))
    for a, w in writes.items():
        reads[a] = max(0, reads[a] - w)
    return reads, writes, cmps, sites


def scan_handwritten():
    """Writes from outside the generated tree: kernel shims, main.c, overrides.

    These matter: main.c now writes __active_heap, so without searching here the
    tool would keep reporting a global that has since been initialised.
    """
    written = set()
    pats = [
        re.compile(r"MEM(?:8|16|32)\((0x[0-9A-Fa-f]{5,8})\)\s*=(?!=)"),
        re.compile(r"MEM32_INIT\((0x[0-9A-Fa-f]{5,8})"),
        # e.g. (uint32_t *)((uint8_t *)g_xbox_mem_offset + 0x006F12E0)
        re.compile(r"g_xbox_mem_offset\s*\+\s*(0x[0-9A-Fa-f]{5,8})"),
        re.compile(r"g_memory_offset\s*\+\s*(0x[0-9A-Fa-f]{5,8})"),
        re.compile(r"ACTIVE_HEAP_VA\s*=\s*(0x[0-9A-Fa-f]{5,8})"),
    ]
    roots = [SRC, os.path.join(ROOT, "src", "kernel")]
    for root in roots:
        for dirpath, _d, files in os.walk(root):
            if os.sep + "gen" in dirpath:
                continue
            for f in files:
                if not f.endswith((".c", ".h")):
                    continue
                try:
                    txt = open(os.path.join(dirpath, f), encoding="utf-8",
                               errors="ignore").read()
                except OSError:
                    continue
                for p in pats:
                    for m in p.finditer(txt):
                        written.add(int(m.group(1), 16))
    return written


def classify(addr, nreads, consts):
    """What kind of global this looks like, and how bad zero would be."""
    nonzero = {c for c in consts if c not in ("0", "0x0")}
    if len(nonzero) >= 2:
        return ("mode selector", 3,
                "compared against " + ", ".join(sorted(nonzero)) +
                " - a state or mode value, and zero is not one of them")
    if len(nonzero) == 1:
        return ("flag vs constant", 2,
                f"compared against {next(iter(nonzero))}; zero takes the other path always")
    if nreads >= 4:
        return ("hot unset global", 1,
                f"{nreads} reads and no writer, but only ever tested against zero")
    return ("unset global", 0, f"{nreads} read(s), no writer")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="unwritten", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-readers", type=int, default=2)
    ap.add_argument("--addr", help="report one address in detail")
    ap.add_argument("--all-ranges", action="store_true",
                    help="do not restrict to the guest data range")
    a = ap.parse_args(argv)

    reads, writes, cmps, sites = scan_gen()
    hand = scan_handwritten()

    if a.addr:
        addr = int(a.addr, 16)
        print(f"0x{addr:08X}")
        print(f"  reads in gen/      : {reads.get(addr, 0)}")
        print(f"  writes in gen/     : {writes.get(addr, 0)}")
        print(f"  written by hand    : {'yes' if addr in hand else 'no'}")
        if cmps.get(addr):
            print(f"  compared against   : {', '.join(sorted(cmps[addr]))}")
        for s in sites.get(addr, []):
            print(f"  read at            : {s}")
        return 0

    rows = []
    for addr, n in reads.items():
        if n < a.min_readers or writes.get(addr):
            continue
        if addr in hand:
            continue
        if not a.all_ranges and not (LO <= addr < HI):
            continue
        kind, sev, why = classify(addr, n, cmps.get(addr, set()))
        rows.append((sev, n, addr, kind, why))
    rows.sort(key=lambda r: (-r[0], -r[1]))

    print(f"{len(rows)} global(s) read but never written "
          f"(min {a.min_readers} readers, guest data range"
          f"{'' if not a.all_ranges else ' ignored'})")
    print()
    for sev, n, addr, kind, why in rows[:40]:
        print(f"  0x{addr:08X}  {n:3d} read(s)  [{kind}]")
        print(f"      {why}")
        for s in sites[addr][:3]:
            print(f"      read at {s}")
    if len(rows) > 40:
        print(f"\n  ... and {len(rows) - 40} more")
    print("\nThese are CANDIDATES, not defects. A write through a computed "
          "pointer is invisible to a static scan. Confirm each the way "
          "__active_heap was: read the users, work out the intended value, "
          "measure the change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
