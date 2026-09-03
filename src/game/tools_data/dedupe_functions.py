#!/usr/bin/env python3
"""dedupe_functions.py - find and repair duplicated function bodies in gen/.

The bug this repairs
--------------------
`manual_edits.py apply` can write a second copy of a run of functions into a
file it is repairing. Observed twice, deterministically, on recomp_0014.c:
sub_001EA600, sub_001EA640 and sub_001EA6B0 each end up defined twice, 309
duplicated lines, and one of the two copies is a HYBRID - it carries labels
belonging to a neighbouring function spliced into the middle of its body.

Two definitions of one symbol in a translation unit do not compile, so this
surfaces at build time rather than silently. But the repair is not obvious:
both copies look plausible, and deleting the wrong one destroys the original.

How the right copy is chosen
----------------------------
Every generated function carries its address range in a header comment:

    * Original: 0x001EA6B0 - 0x001EA72F (127 bytes, 58 insns)

and every label inside it is `loc_<VA>`. A correct body's labels ALL fall
inside that range. The hybrid copy fails immediately - sub_001EA6B0's bad copy
contained `loc_001EA5EF`, which belongs to sub_001EA5E0.

So: score each copy by how many of its labels lie outside its own declared
range, and keep the one with none. If both score zero the copies are equally
valid and the FIRST is kept, which is what a plain de-duplication would do
anyway; if both score non-zero neither is trustworthy and the file is left
alone for a human.

Usage (from src/game/):
    py -3 tools_data/dedupe_functions.py            # report only
    py -3 tools_data/dedupe_functions.py --apply
    py -3 tools_data/dedupe_functions.py --self-test
"""
import argparse
import collections
import glob
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(os.path.dirname(HERE), "src", "recomp", "gen")

HDR_RE = re.compile(r"^void (sub_[0-9A-F]+)\(void\)$")
ORIG_RE = re.compile(r"^\s\*\sOriginal:\s0x([0-9A-F]+)\s-\s0x([0-9A-F]+)")
LABEL_RE = re.compile(r"\bloc_([0-9A-F]{8})\b")


def copies(lines):
    """Every function definition as (name, comment_start, body_end)."""
    out = []
    for i, line in enumerate(lines):
        m = HDR_RE.match(line)
        if not m:
            continue
        start = i
        while start > 0 and not lines[start].startswith("/**"):
            start -= 1
        end = i
        while end < len(lines) and lines[end] != "}":
            end += 1
        out.append((m.group(1), start, min(end, len(lines) - 1)))
    return out


def out_of_range(lines, start, end):
    """How many labels in this copy fall outside its declared address range.

    A label DEFINED here that belongs to another function is the signature of a
    spliced body. Returns None when the range header is missing, which makes the
    copy unscoreable rather than clean.
    """
    lo = hi = None
    for l in lines[start:end + 1]:
        m = ORIG_RE.match(l)
        if m:
            lo, hi = int(m.group(1), 16), int(m.group(2), 16)
            break
    if lo is None:
        return None
    bad = 0
    for l in lines[start:end + 1]:
        for va in LABEL_RE.findall(l):
            if not (lo <= int(va, 16) <= hi):
                bad += 1
    return bad


def plan(lines):
    """(spans_to_delete, report_rows). Pure, so the self-test can drive it."""
    found = copies(lines)
    by_name = collections.defaultdict(list)
    for name, s, e in found:
        by_name[name].append((s, e))

    # A duplicated run shows up as several names each appearing twice. Score
    # every copy, then delete whole spans - never interleave, or the line
    # numbers of the survivors move under us.
    doomed, rows = [], []
    for name, spans in sorted(by_name.items()):
        if len(spans) < 2:
            continue
        scored = [(out_of_range(lines, s, e), s, e) for s, e in spans]
        clean = [x for x in scored if x[0] == 0]
        if not clean:
            rows.append((name, "SKIPPED - no clean copy, left for a human",
                         [s for _, s, _ in scored]))
            continue
        keep = clean[0]
        for sc, s, e in scored:
            if (s, e) != (keep[1], keep[2]):
                doomed.append((s, e))
                rows.append((name, "delete copy at line %d (%s labels out of "
                                   "range)" % (s + 1, "no score" if sc is None
                                               else sc), None))
    return doomed, rows


def apply_plan(lines, doomed):
    """Delete the doomed spans, back to front, plus one trailing blank each."""
    for s, e in sorted(doomed, key=lambda x: -x[0]):
        end = e
        if end + 1 < len(lines) and not lines[end + 1].strip():
            end += 1
        del lines[s:end + 1]
    return lines


def self_test():
    src = (
        "/**\n * sub_00001000\n * Original: 0x00001000 - 0x00001010\n */\n"
        "void sub_00001000(void)\n{\n"
        "loc_00001000: ;\n"
        "loc_00002FFF: ;\n"          # OUT of range - this copy is the hybrid
        "}\n"
        "\n"
        "/**\n * sub_00001000\n * Original: 0x00001000 - 0x00001010\n */\n"
        "void sub_00001000(void)\n{\n"
        "loc_00001000: ;\n"
        "loc_0000100C: ;\n"          # in range - the good copy
        "}\n"
    ).split("\n")
    doomed, rows = plan(src)
    assert len(doomed) == 1, doomed
    assert doomed[0][0] == 0, doomed          # the hybrid is first; delete it
    out = apply_plan(list(src), doomed)
    names = [l for l in out if HDR_RE.match(l)]
    assert len(names) == 1, names
    assert "loc_00002FFF: ;" not in out
    assert "loc_0000100C: ;" in out
    # A file with no duplicates is untouched.
    assert plan(out)[0] == []
    print("self-test ok: hybrid copy identified by out-of-range label and "
          "removed, clean copy kept, idempotent")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--gen-dir", default=GEN)
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()

    total = 0
    for path in sorted(glob.glob(os.path.join(a.gen_dir, "recomp_*.c"))):
        raw = io.open(path, encoding="utf-8", errors="ignore", newline="").read()
        crlf = "\r\n" in raw
        lines = raw.replace("\r\n", "\n").split("\n")
        doomed, rows = plan(lines)
        if not rows:
            continue
        print(os.path.basename(path))
        for name, what, extra in rows:
            print("  %-16s %s" % (name, what))
        total += len(doomed)
        if doomed and a.apply:
            text = "\n".join(apply_plan(lines, doomed))
            if crlf:
                text = text.replace("\n", "\r\n")
            io.open(path, "w", encoding="utf-8", newline="").write(text)

    print()
    print("%d duplicate function bod%s" % (total, "y" if total == 1 else "ies"),
          "removed" if a.apply else "found")
    if not a.apply and total:
        print("report only; pass --apply to remove")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
