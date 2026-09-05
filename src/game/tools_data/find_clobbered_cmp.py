"""Find deferred comparisons whose operands are clobbered before the jcc.

The lifter emits an x86 `cmp A, B` as a no-op comment and re-evaluates the
comparison inside the following conditional jump.  That is correct only while
neither operand changes in between.  When the original code placed an
instruction between the `cmp` and the `jcc` that writes A or B -- which x86
allows, since those instructions do not touch the flags -- the generated C
compares the new value instead of the one the flags were set from.

Measured instance: sub_001F09D0 at loc_001F0AA9 compares `esi` where the
original compares `MEM32(eax)`, because `ecx = esi` sits between the two.
"""
import glob
import os
import re
import sys

CMP = re.compile(r'/\* (cmp|test|sub) (\w+), (\w+)')
JCC = re.compile(r'\bif \((?:CMP|TEST)_\w+\(([^,]+), ')
ASSIGN = re.compile(r'^\s*(\w+) = ')
REGS = {"eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp"}

hits = []
for path in sorted(glob.glob(os.path.join("src", "recomp", "gen", "*.c"))):
    lines = open(path, encoding="utf-8").read().split("\n")
    for i, line in enumerate(lines):
        m = CMP.search(line)
        if not m:
            continue
        ops = {m.group(2), m.group(3)} & REGS
        if not ops:
            continue
        # Walk forward to the jcc that consumes these flags.
        for j in range(i + 1, min(i + 12, len(lines))):
            nxt = lines[j]
            if JCC.search(nxt):
                break
            a = ASSIGN.match(nxt)
            if a and a.group(1) in ops:
                hits.append((path, j + 1, m.group(0).strip(), nxt.strip()))
                break
            if nxt.strip().startswith("/*") or not nxt.strip():
                continue
            if "goto " in nxt or nxt.strip().startswith("loc_"):
                break

for path, ln, cmp_txt, clob in hits:
    print("%s:%d  %s  clobbered by: %s" % (path, ln, cmp_txt, clob))
print("\n%d clobbered comparison(s)" % len(hits), file=sys.stderr)
