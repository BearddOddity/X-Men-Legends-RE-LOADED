#!/usr/bin/env python3
"""probe_struct.py - dump many fields of one object in a single probe.

Why this exists
---------------
"What is actually in this object right now?" is the most common question in a
static recompile, and answering it has cost one build-and-run cycle per FIELD.
On 2026-08-05 the engine allocator was diagnosed across roughly eight separate
probes - manager+0x18, +0x30, +0x70, +0x74, +0x98, +0x9C, +0xAC, +0xB4, +0xC0 -
each hand-written, each a full rebuild, and several of them only to learn the
field was fine. Batching them turns a day of cycles into one.

It is a thin wrapper over add_probe.py rather than a second implementation:
add_probe owns the escaping and the /* PROBE */ markers that strip_probes.py
depends on, and duplicating either would be a second thing to keep correct.
Everything here just builds the --args and --fmt strings.

Usage (from src/game/):
    py -3 tools_data/probe_struct.py recomp_0015.c \\
        --after "loc_0020F209: ;" --tag pool --base esi \\
        --offsets 0x38,0x88,0x8C,0xA8,0xB4,0xC8

    # name the fields when you know them - the labels appear in the output
    ... --offsets 0xA8:limit,0xC8:blocksize,0x88:total

    # a second dereference: dump fields of the object POINTED TO by base+0x70
    py -3 tools_data/probe_struct.py recomp_0015.c --after "..." --tag inner \\
        --base "MEM32(esi + 0x70)" --offsets 0x2C:cursor,0x48:sentinel

Reads naturally against a hexdump, and prints the base itself first so a null
or obviously-wrong object is visible immediately rather than being inferred
from a row of zeroes.

Portability
-----------
Nothing here is title-specific. Any recompile that models guest memory with a
MEM32-style accessor can use it by changing ACCESSOR below.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
ACCESSOR = "MEM32"          # how this project reads a guest dword
MAX_FIELDS = 12             # add_probe caps printf args in practice; keep it sane


def parse_offsets(spec):
    """'0xA8:limit,0xC8' -> [(0xA8, 'limit'), (0xC8, '+0xC8')]"""
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            off, name = item.split(":", 1)
        else:
            off, name = item, None
        off = off.strip()
        val = int(off, 16) if off.lower().startswith("0x") else int(off, 0)
        out.append((val, (name or "").strip() or ("+0x%X" % val)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="probe_struct", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="gen/*.c basename or path")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--after")
    grp.add_argument("--before")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--base", required=True,
                    help="C expression for the object pointer, e.g. esi or "
                         "MEM32(esi + 0x70)")
    ap.add_argument("--offsets", required=True,
                    help="comma-separated, each OFF or OFF:name")
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the add_probe command without running it")
    a = ap.parse_args(argv)

    fields = parse_offsets(a.offsets)
    if not fields:
        sys.exit("--offsets parsed to nothing")
    if len(fields) > MAX_FIELDS:
        sys.exit(f"{len(fields)} fields is too many for one probe "
                 f"(max {MAX_FIELDS}); split it")

    # The base itself first: a null or nonsense object should be obvious at a
    # glance instead of being deduced from a row of zeroes.
    args = [a.base]
    fmt = ["base=0x%08X"]
    for off, name in fields:
        args.append(f"{ACCESSOR}({a.base} + 0x{off:X})")
        fmt.append(f"{name}=0x%08X")

    path = a.file
    if not os.path.dirname(path):
        path = os.path.join("src", "recomp", "gen", path)

    cmd = [sys.executable or "py", os.path.join("tools_data", "add_probe.py"),
           path, "--tag", a.tag, "--limit", str(a.limit),
           "--fmt", " ".join(fmt), "--args", ",".join(args)]
    cmd += (["--after", a.after] if a.after else ["--before", a.before])

    print("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if a.dry_run:
        return 0
    return subprocess.run(cmd, cwd=GAME).returncode


if __name__ == "__main__":
    sys.exit(main())
