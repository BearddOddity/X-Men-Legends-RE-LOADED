#!/usr/bin/env python3
"""resolve_rva.py - turn the host RVAs in a crash stack into function names.

Why this exists
---------------
When the fault lands inside a system DLL the crash handler prints the native
call stack as bare RVAs and tells you to "resolve against build/*.map". Nothing
did that. whereis.py translates generated C locations to GUEST addresses, which
is a different question - these are HOST addresses in our own executable - so
naming the recompiled caller of a CRT fault was a manual grep through an 11 MB
map, done backwards, once per frame.

That matters because a fault inside VCRUNTIME140 or ntdll says nothing on its
own. The whole diagnostic value is in the first frame BELOW it that belongs to
us, and that frame is only ever printed as an RVA.

    python3 tools_data/resolve_rva.py 0xEAE118 0xED7692
    python3 tools_data/resolve_rva.py --stack stderr.txt
    python3 tools_data/resolve_rva.py --stack abi_stderr.txt --map build_abi/Release/xmen_legends_recomp.map

Reads the MSVC map's "Publics by Value" table:

     0001:00ecf100       sub_00209650      0000000140ed0100 f   recomp_0015.c.obj

RVA is the Rva+Base column minus the preferred load address, which is parsed
from the map rather than assumed - a rebuild that changes it would otherwise
shift every answer silently.

Self-check:  python3 tools_data/resolve_rva.py --selftest
"""
import argparse
import bisect
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
DEFAULT_MAP = os.path.join(GAME, "build", "xmen_legends_recomp.map")

BASE_RE = re.compile(r"Preferred load address is ([0-9A-Fa-f]+)")
# 0001:00ecf100       sub_00209650      0000000140ed0100 f   recomp_0015.c.obj
SYM_RE = re.compile(
    r"^\s+[0-9A-Fa-f]{4}:[0-9A-Fa-f]{8}\s+(\S+)\s+([0-9A-Fa-f]{8,16})\s+(\S+)?\s*(\S+)?\s*$"
)
STACK_RE = re.compile(r"RVA\s+0x([0-9A-Fa-f]+)")


def load_map(path):
    """Return (sorted rvas, names, objects, base). One pass, no regex on the hot path."""
    if not os.path.exists(path):
        sys.exit(f"no map at {path} - build first, or pass --map")
    base = None
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if base is None:
                m = BASE_RE.search(line)
                if m:
                    base = int(m.group(1), 16)
                    continue
            m = SYM_RE.match(line)
            if not m:
                continue
            name, rvabase, flag, obj = m.groups()
            # The 'f' column marks a function; static data has it blank and the
            # object shifts left a field. Keep both, but remember which is which.
            if flag != "f":
                obj = flag
            try:
                addr = int(rvabase, 16)
            except ValueError:
                continue
            rows.append((addr, name, obj or "?"))
    if base is None:
        sys.exit(f"{path}: no 'Preferred load address' line - not an MSVC map?")
    rows.sort()
    return [r[0] - base for r in rows], [r[1] for r in rows], [r[2] for r in rows], base


def resolve(rvas, names, objs, want):
    """Nearest symbol at or before `want`, with the offset into it."""
    i = bisect.bisect_right(rvas, want) - 1
    if i < 0:
        return None
    return names[i], want - rvas[i], objs[i]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("rvas", nargs="*", help="RVAs in hex, 0x optional")
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--stack", help="a crash log; resolves every 'RVA 0x...' in it")
    ap.add_argument("--ours-only", action="store_true",
                    help="only frames from a recomp_*.obj - the ones that are our code")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    rvas, names, objs, base = load_map(a.map)
    print(f"{len(rvas)} symbols from {os.path.basename(a.map)}, "
          f"preferred base 0x{base:X}\n")

    wanted = [int(x, 16) for x in a.rvas]
    if a.stack:
        text = open(a.stack, encoding="utf-8", errors="replace").read()
        wanted += [int(m, 16) for m in STACK_RE.findall(text)]
    if not wanted:
        sys.exit("give some RVAs, or --stack FILE")

    for w in wanted:
        got = resolve(rvas, names, objs, w)
        if not got:
            print(f"  0x{w:<10X} (below the first symbol)")
            continue
        name, off, obj = got
        if a.ours_only and not obj.startswith("recomp_"):
            continue
        print(f"  0x{w:<10X} {name}+0x{off:X}   [{obj}]")
    return 0


def selftest():
    """Parse a miniature map and check the nearest-symbol lookup at its edges."""
    import tempfile
    sample = (
        " xmen_legends_recomp\n"
        "\n"
        " Preferred load address is 0000000140000000\n"
        "\n"
        " 0001:00000000       first_fn        0000000140001000 f   a.obj\n"
        " 0001:00000100       second_fn       0000000140001100 f   b.obj\n"
        " 0001:00000200       third_fn        0000000140001200 f   c.obj\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".map", delete=False) as fh:
        fh.write(sample)
        p = fh.name
    try:
        rvas, names, objs, base = load_map(p)
        assert base == 0x140000000, base
        assert rvas == [0x1000, 0x1100, 0x1200], rvas
        # exactly on a symbol
        assert resolve(rvas, names, objs, 0x1100) == ("second_fn", 0, "b.obj")
        # inside one
        assert resolve(rvas, names, objs, 0x1108) == ("second_fn", 8, "b.obj")
        # just before the next
        assert resolve(rvas, names, objs, 0x11FF) == ("second_fn", 0xFF, "b.obj")
        # past the last symbol still resolves to it
        assert resolve(rvas, names, objs, 0x9999) == ("third_fn", 0x8799, "c.obj")
        # below the first returns nothing rather than guessing
        assert resolve(rvas, names, objs, 0x10) is None
    finally:
        os.unlink(p)
    print("selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
