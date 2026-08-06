#!/usr/bin/env python3
"""whereis.py - translate between generated C locations and guest addresses.

Why this exists
---------------
Every diagnostic the port emits speaks one of two languages and the useful
question is always in the other one. The ICALL reject log names a call site as
`recomp_0014.c:21439`; the disassembly, the seed list and the manual-edit
records all speak `sub_001F09D0` / `0x001F0AA9`. Translating by hand means
grepping backwards for the nearest `void sub_`, which is three commands and is
easy to get subtly wrong near a fragment boundary.

Both directions, and the nearest labelled block, because a 40,000-line
generated file is not something to page through.

Usage (from src/game/):
    py -3 tools_data/whereis.py recomp_0014.c:21439    # -> which guest function
    py -3 tools_data/whereis.py 0x001F0AA9             # -> which file and line
    py -3 tools_data/whereis.py sub_001F09D0           # -> where it is defined
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(os.path.dirname(HERE), "src", "recomp", "gen")

FN_DEF = re.compile(r"^void (sub_([0-9A-Fa-f]{8}))\(")
LABEL = re.compile(r"^(loc_([0-9A-Fa-f]{8})):")


def index():
    """[(file, start_line, end_line, name, addr)] plus per-file label lists."""
    funcs, labels = [], {}
    for path in sorted(glob.glob(os.path.join(GEN, "recomp_*.c"))):
        base = os.path.basename(path)
        lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
        labels[base] = []
        open_fn = None
        for i, line in enumerate(lines, 1):
            m = FN_DEF.match(line)
            if m:
                if open_fn:
                    open_fn[2] = i - 1
                open_fn = [base, i, len(lines), m.group(1), int(m.group(2), 16)]
                funcs.append(open_fn)
                continue
            lm = LABEL.match(line)
            if lm:
                labels[base].append((i, lm.group(1), int(lm.group(2), 16)))
    return funcs, labels


def nearest_label(labels, base, line):
    """Last loc_ at or above `line` - the address the diagnostic really means."""
    best = None
    for ln, name, addr in labels.get(base, []):
        if ln <= line:
            best = (ln, name, addr)
        else:
            break
    return best


def main(argv):
    if len(argv) != 1:
        print(__doc__)
        return 2
    q = argv[0]
    funcs, labels = index()

    if ":" in q:                                    # file.c:LINE -> guest
        base, _, ln = q.partition(":")
        try:
            ln = int(ln)
        except ValueError:
            return print(f"not a line number: {ln}") or 2
        hits = [f for f in funcs if f[0] == base and f[1] <= ln <= f[2]]
        if not hits:
            print(f"{q}: inside no function (header, table, or past the end)")
            return 1
        base_, start, end, name, addr = hits[-1]
        print(f"{q}")
        print(f"  function : {name}  (0x{addr:08X})")
        print(f"  spans    : {base_}:{start}-{end}   (+{ln - start} lines in)")
        lab = nearest_label(labels, base_, ln)
        if lab:
            print(f"  block    : {lab[1]}  (0x{lab[2]:08X}) at line {lab[0]}")
            print(f"  guest    : approximately 0x{lab[2]:08X}")
        return 0

    if q.lower().startswith("0x") or re.fullmatch(r"[0-9A-Fa-f]{6,8}", q):
        want = int(q, 16)
        for base, start, end, name, addr in funcs:
            if addr == want:
                print(f"0x{want:08X} = {name} defined at {base}:{start}-{end}")
                return 0
        # not an entry point - find the owner and the matching label
        for base, start, end, name, addr in funcs:
            for ln, lname, laddr in labels.get(base, []):
                if laddr == want and start <= ln <= end:
                    print(f"0x{want:08X} = {lname} at {base}:{ln}, "
                          f"inside {name} (0x{addr:08X})")
                    return 0
        print(f"0x{want:08X}: no function entry and no label - not lifted, "
              f"or mid-instruction")
        return 1

    if q.startswith("sub_"):
        for base, start, end, name, addr in funcs:
            if name == q:
                print(f"{q} (0x{addr:08X}) at {base}:{start}-{end}")
                return 0
        print(f"{q}: not defined in gen/")
        return 1

    print(f"unrecognised query: {q}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
