#!/usr/bin/env python3
"""Drop functions from gen/recomp_seed.c that the main generation now finds.

Why this is needed
------------------
recomp_seed.c holds functions the disassembler could not reach - they are
referenced only through data pointers, so nothing calls them directly and
sweep-based discovery misses them. seed_missing_functions.py recompiles
those separately and appends them.

But discovery improves. When a lifter or translator fix lets the main sweep
reach a function it previously missed, that function ends up defined twice:
once in recomp_00NN.c and once in recomp_seed.c. The link then fails with
LNK2005, which reads like a regression but is actually the opposite - it
means discovery got better.

The generated definition wins: it was produced with full surrounding
context, while the seeded one was recompiled in isolation from a guessed
extent. Registrations in recomp_lookup_manual() keep working either way,
because both define the same symbol name.

Run from src/game/. Use --apply to write; the default is a dry run.
"""
import argparse
import os
import re
import sys

GEN = os.path.join("src", "recomp", "gen")
SEED = os.path.join(GEN, "recomp_seed.c")

DEF_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*$")


def generated_names():
    """Every function defined by the main generation (not the seed)."""
    names = set()
    for fn in sorted(os.listdir(GEN)):
        if not fn.endswith(".c") or fn == "recomp_seed.c":
            continue
        with open(os.path.join(GEN, fn), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                m = DEF_RE.match(line.rstrip("\n"))
                if m:
                    names.add(m.group(1))
    return names


def split_seed(lines):
    """Yield (name_or_None, [lines]) chunks of the seed file.

    A chunk is one top-level function definition, or the surrounding
    non-function text. Relies on the emitter's fixed shape: the signature
    on its own line, '{' next, and a lone '}' closing at column 0.
    """
    out, i = [], 0
    pre = []
    while i < len(lines):
        m = DEF_RE.match(lines[i])
        if not m:
            pre.append(lines[i])
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j] != "}":
            j += 1
        if j >= len(lines):
            sys.exit("unterminated function %s at line %d" % (m.group(1), i + 1))
        out.append((None, pre))
        pre = []
        out.append((m.group(1), lines[i:j + 1]))
        i = j + 1
    out.append((None, pre))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the change (default is a dry run)")
    args = ap.parse_args()

    if not os.path.exists(SEED):
        print("no recomp_seed.c - nothing to do")
        return

    gen = generated_names()
    with open(SEED, encoding="utf-8", errors="replace", newline="") as f:
        lines = f.read().split("\n")

    chunks = split_seed(lines)
    kept, dropped = [], []
    for name, body in chunks:
        if name is not None and name in gen:
            dropped.append(name)
            # Leave a marker so the next reader knows why it vanished.
            kept.append("/* %s: dropped - the main generation now discovers "
                        "it (was seeded when it did not) */" % name)
            kept.append("")
            continue
        kept.extend(body)

    seeded = sum(1 for n, _ in chunks if n)
    if not dropped:
        print("no duplicates - %d seeded function(s) all still unique" % seeded)
        return

    print("%d of %d seeded functions are now generated:" % (len(dropped), seeded))
    for n in dropped:
        print("  %s" % n)

    if not args.apply:
        print("\ndry run - pass --apply to write")
        return

    with open(SEED, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(kept))
    print("\nwrote %s (%d function(s) remain)" % (SEED, seeded - len(dropped)))


if __name__ == "__main__":
    main()
