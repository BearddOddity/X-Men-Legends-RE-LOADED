"""
add_probe.py - insert a debug probe into gen/*.c correctly, every time.

Why this exists
---------------
Probes are the main debugging tool on this project (Rule #5), and they were
being hand-written or pasted through shell heredocs. The shell mangles
backslash escapes in heredocs, so `\\n` inside a C string literal arrives as a
real newline and the file no longer compiles - that happened four times in one
day. Twice is a class (Rule #9), so this builds the probe instead.

It also enforces the `/* PROBE */` convention that `strip_probes.py` relies on:
a multi-line probe opens with `{ ... /* PROBE */` and closes with
`} /* PROBE */`, so removal is exact and brace-balanced.

Usage (from src/game/):
    py -3 tools_data/add_probe.py src/recomp/gen/recomp_0015.c \
        --after "loc_00209683: ;" \
        --tag VCALLA --fmt "obj=%08X vtbl=%08X" --args eax,edx

    # print only the first N hits (default 20; 0 = unlimited)
    ... --limit 5

    # insert before the anchor instead of after
    ... --before "PUSH32(esp, edi);"

The anchor must match exactly one line, otherwise nothing is written.
Values are emitted verbatim as C expressions, so `MEM32(edx + 0xFC)` works.
"""
import argparse
import os
import sys

MARK = "/* PROBE */"


def build(tag, fmt, args, limit):
    """Emit the probe as C. The format string is written here, in Python, so
    the escapes are correct by construction - no shell involved."""
    body = f'      fprintf(stderr, "[{tag}] {fmt}\\n"'
    if args:
        body += ", " + ", ".join(args)
    body += ");"

    if limit:
        guard = f"_p_{tag}"
        return (f"    {{ static int {guard}; if ({guard} < {limit}) {{ {guard}++; {MARK}\n"
                f"{body}\n"
                f"    }} }} {MARK}")
    return f"    {{ {MARK}\n{body}\n    }} {MARK}"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--after", help="anchor line to insert after (exact match)")
    g.add_argument("--before", help="anchor line to insert before (exact match)")
    ap.add_argument("--tag", required=True, help="label, e.g. VCALLA")
    ap.add_argument("--fmt", default="", help='printf body, e.g. "obj=%%08X"')
    ap.add_argument("--args", default="",
                    help="comma-separated C expressions matching --fmt")
    ap.add_argument("--limit", type=int, default=20,
                    help="print at most N times (0 = unlimited)")
    a = ap.parse_args(argv)

    if not os.path.exists(a.file):
        raise SystemExit(f"{a.file} not found")
    if not a.tag.isalnum():
        raise SystemExit("--tag must be alphanumeric (it becomes a C identifier)")

    args = [x.strip() for x in a.args.split(",") if x.strip()]
    if a.fmt.count("%") != len(args):
        raise SystemExit(
            f"--fmt has {a.fmt.count('%')} conversion(s) but --args has "
            f"{len(args)} value(s); they must match or the probe prints garbage")

    lines = open(a.file, encoding="utf-8", errors="ignore").read().split("\n")
    anchor = a.after if a.after is not None else a.before
    hits = [i for i, l in enumerate(lines) if l.rstrip() == anchor.rstrip()]
    if len(hits) != 1:
        raise SystemExit(
            f"anchor matched {len(hits)} lines, need exactly 1: {anchor!r}"
            + (f"\n  first few: {hits[:5]}" if hits else ""))

    at = hits[0] + 1 if a.after is not None else hits[0]
    lines[at:at] = build(a.tag, a.fmt, args, a.limit).split("\n")

    src = "\n".join(lines)
    if f"#include <stdio.h> {MARK}" not in src:
        if "#include <math.h>" in src:
            src = src.replace("#include <math.h>",
                              f"#include <math.h>\n#include <stdio.h> {MARK}", 1)
        else:
            raise SystemExit("could not find an include anchor to add <stdio.h>")

    with open(a.file, "w", encoding="utf-8", newline="") as f:
        f.write(src)
    print(f"probe [{a.tag}] inserted at {a.file}:{at + 1}")
    print("remove with: py -3 tools_data/strip_probes.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
