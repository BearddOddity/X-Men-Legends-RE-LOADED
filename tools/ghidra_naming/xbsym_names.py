#!/usr/bin/env python3
"""Turn XbSymbolDatabase output into meaningful names for the recompiler.

The FidDb pipeline in this directory tops out around 134 names because Ghidra's
signature databases cover the MSVC CRT and nothing else. As README.md puts it,
that is "effectively the FidDb ceiling for this title without a custom
XDK/RenderWare signature database".

XbSymbolDatabase *is* that missing database. It fingerprints the statically
linked Xbox SDK (D3D8, DSOUND, XAPILIB, XGRAPHC) and yields real names -
D3DDevice_SetRenderState_Simple, CDirectSoundStream_Release - for the very
sections FidDb cannot touch. These are signature matches, not inferences.

Produce the input with the CLI shipped in the ghidra-xbe extension:

    XbSymbolDatabaseCLI.exe path/to/default.xbe > xbsym.txt

Then:

    py -3 tools/ghidra_naming/xbsym_names.py xbsym.txt              # dry run
    py -3 tools/ghidra_naming/xbsym_names.py xbsym.txt --apply      # write names

--apply reuses merge_names.apply_to_functions_json, so it takes the same
backup and matching rules as the FidDb path. Names only land on addresses that
are a known function `start`; SDK *data* symbols (D3DRS_*, D3D_g_*) have no
function to name and are reported separately rather than silently dropped.
"""
import argparse
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import merge_names  # noqa: E402  - sibling module, reuse its rules

LINE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(0[xX][0-9A-Fa-f]+)\s*$")


def parse(path):
    """-> {normalised_addr: sanitized_name}, plus a count of unparsed lines."""
    out, bad = {}, 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            m = LINE.match(line)
            if not m:
                bad += 1
                continue
            name, addr = m.group(1), m.group(2)
            key = merge_names.norm_addr(addr)
            if key is None:
                bad += 1
                continue
            # Same sanitisation the FidDb path uses, so both sources produce
            # identifiers the C backend will accept.
            out[key] = merge_names.sanitize(name)
    return out, bad


def main():
    ap = argparse.ArgumentParser(
        prog="xbsym_names",
        description="Apply XbSymbolDatabase XDK names to the recompiler.")
    ap.add_argument("symfile", help="XbSymbolDatabaseCLI output")
    ap.add_argument("--functions-json", default=merge_names.DEFAULT_FUNCTIONS_JSON
                    if hasattr(merge_names, "DEFAULT_FUNCTIONS_JSON") else None,
                    help="recompiler functions.json to update")
    ap.add_argument("--apply", action="store_true",
                    help="write the names (backs up first)")
    args = ap.parse_args()

    fjson = args.functions_json
    if not fjson:
        fjson = os.path.normpath(os.path.join(
            _HERE, "..", "disasm", "output", "functions.json"))

    names, bad = parse(args.symfile)
    print("parsed %d symbol(s) from %s" % (len(names), args.symfile))
    if bad:
        print("  %d line(s) not in 'NAME = 0xADDR' form (ignored)" % bad)

    if not os.path.exists(fjson):
        sys.exit("functions.json not found: %s" % fjson)

    import json
    with open(fjson) as fh:
        data = json.load(fh)
    starts = {merge_names.norm_addr(e.get("start")) for e in data}

    hit = {a: n for a, n in names.items() if a in starts}
    miss = {a: n for a, n in names.items() if a not in starts}

    print("  %d land on a known function start  <- these become names" % len(hit))
    print("  %d do not (SDK data symbols, or code not detected as a function)"
          % len(miss))
    # sanitize() collapses the "LIB__Name" double underscore, so recover the
    # library from the leading token against the known set rather than splitting
    # on "__" (which no longer exists by this point).
    LIBS = ("XAPILIB", "D3D8", "DSOUND", "XGRAPHC", "D3DX8", "XONLINE")
    by_lib = {}
    for n in hit.values():
        lib = next((l for l in LIBS if n.startswith(l + "_")), "other")
        by_lib[lib] = by_lib.get(lib, 0) + 1
    for lib, n in sorted(by_lib.items(), key=lambda kv: -kv[1]):
        print("      %-10s %d" % (lib, n))

    # The misses are expected and worth stating plainly: the recompiler only
    # lifts .text, and D3D8/DSOUND/XGRAPHC live in their own sections which the
    # hand-written shim libraries replace. Those names are still useful in
    # Ghidra for RE, just not here.
    if miss:
        miss_libs = {}
        for n in miss.values():
            lib = next((l for l in LIBS if n.startswith(l + "_")), "other")
            miss_libs[lib] = miss_libs.get(lib, 0) + 1
        print("  misses by library (not lifted - replaced by the shim, or data):")
        for lib, n in sorted(miss_libs.items(), key=lambda kv: -kv[1]):
            print("      %-10s %d" % (lib, n))

    if not args.apply:
        print("\ndry run; re-run with --apply")
        print("sample:")
        for a in sorted(hit)[:8]:
            print("  %s  %s" % (a, hit[a]))
        return 0

    merge_names.apply_to_functions_json(hit, fjson)
    print("\nRegenerate to pick the names up in the emitted C.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
