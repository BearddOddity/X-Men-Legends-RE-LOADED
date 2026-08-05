#!/usr/bin/env python3
"""Rewrite named call targets in recomp_seed.c to their sub_ADDRESS form.

seeded_functions.json carries real names for the XDK functions that
XbSymbolDatabase identified (XAPILIB_SetLastError and friends). The seeder
translates one function at a time against that file, so its output calls those
names directly - but the main generation defines every function as
sub_XXXXXXXX, so the link fails:

    recomp_seed.c.obj : error LNK2019: unresolved external symbol
                        XAPILIB_SetLastError referenced in function sub_0019E096

Both names denote the same address, so this is purely cosmetic drift between
two generation paths. Rewriting the seed to the sub_ form is the smaller,
safer direction: it touches one generated file instead of renaming 26,000
definitions, and it keeps the readable names available in the JSON.

Run from src/game/:
    py -3 tools_data/normalise_seed_names.py --apply
"""
import argparse
import json
import os
import re
import sys

SEED = os.path.join("src", "recomp", "gen", "recomp_seed.c")
FUNCS = "seeded_functions.json"


def name_map():
    """{ non-sub name -> sub_ADDRESS } for every named entry."""
    with open(FUNCS, encoding="utf-8") as f:
        data = json.load(f)
    entries = data["functions"] if isinstance(data, dict) else data
    out = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        start = e.get("start")
        if not name or not start or name.startswith("sub_"):
            continue
        va = int(start, 16) if isinstance(start, str) else start
        out[name] = "sub_%08X" % va
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(SEED):
        sys.exit("no %s - nothing seeded yet" % SEED)

    names = name_map()
    if not names:
        print("no named entries in %s - nothing to normalise" % FUNCS)
        return

    text = open(SEED, encoding="utf-8", errors="replace").read()

    # Plain whole-word replacement. Matching "identifier followed by (" is the
    # obvious rule and the wrong one: calls are emitted as
    # RECOMP_ABI_CALL(name), so the name is followed by ')'. Occurrences in
    # doc comments get rewritten too, which is what we want anyway - the
    # comment should name the same symbol as the code.
    total, hits = 0, {}
    for name, sub in names.items():
        pat = re.compile(r"\b%s\b" % re.escape(name))
        text, n = pat.subn(sub, text)
        if n:
            hits[name] = (sub, n)
            total += n

    if not total:
        print("no named call targets in the seed - nothing to do")
        return

    print("%d reference(s) across %d name(s):" % (total, len(hits)))
    for name, (sub, n) in sorted(hits.items()):
        print("  %-40s -> %s   x%d" % (name, sub, n))

    if not a.apply:
        print("\ndry run - pass --apply to write")
        return

    with open(SEED, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print("\nwrote %s" % SEED)


if __name__ == "__main__":
    main()
