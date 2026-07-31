"""
find_stale_flag_tests.py - detect miscompiled deferred flag tests in gen/*.c.

The bug
-------
x86 computes flags with one instruction and branches on them later. The lifter
models this by deferring the comparison: it emits a placeholder where the flag
producing instruction was, then re-evaluates the comparison inline at the `jcc`:

    (void)0;                    /* test cl, cl - flags set for next jcc */
    ecx = esi;
    if (TEST_NZ(LO8(ecx), LO8(ecx))) goto loc_002041F8;

That is only correct while nothing between the two writes the tested register.
Real x86 at sub_002041D0 is:

    test cl, cl        ; flags come from cl HERE
    mov  ecx, esi      ; ecx is clobbered
    jne  0x2041f8      ; still branches on the flags from before the clobber

so the generated form tests the low byte of `esi` instead of the flag byte -
the branch can go the wrong way, silently. This produced a real crash: the
wrong branch reached an indirect call through a NULL vtable pointer.

This scans for that shape: a deferred flag site whose tested register is both
assigned before the `jcc` AND referenced by the `jcc`. Sites where the tested
register is untouched, or where the jcc no longer names it, are fine.

Note this reports the pattern, not a proven miscompile - a site is only truly
wrong if the branch outcome actually differs. Verify against raw disassembly
(see DEBUGGING_NOTES.md) before changing anything.

Usage (from src/game/):
    py -3 tools_data/find_stale_flag_tests.py            # summary
    py -3 tools_data/find_stale_flag_tests.py --list     # every site
"""
import glob
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(os.path.dirname(HERE), "src", "recomp", "gen")

DEFER = re.compile(
    r"^\s*\(void\)0;\s*/\* (?:test|cmp) (.+?) - flags set for next jcc \*/")
ASSIGN = re.compile(r"^\s*(e[a-z][a-z])\s*=(?!=)|^\s*SET_LO(?:8|16)\((e[a-z][a-z])\s*,")
JCC = re.compile(r"^\s*if \((?:TEST_|CMP_)\w+\((.*)\)\)")
REG = re.compile(r"\be[a-z][a-z]\b")

# Lines that end a straight-line run: past these the flags are no longer the
# ones this deferred site produced.
STOP = ("loc_", "}")


def scan():
    hits, scanned = [], 0
    for path in sorted(glob.glob(os.path.join(GEN, "recomp_*.c"))):
        lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
        for i, line in enumerate(lines):
            m = DEFER.match(line)
            if not m:
                continue
            scanned += 1
            tested = set(REG.findall(m.group(1)))
            if not tested:
                continue
            clobbered = set()
            for j in range(i + 1, min(i + 10, len(lines))):
                nxt = lines[j]
                s = nxt.strip()
                if not s:
                    continue
                jm = JCC.match(nxt)
                if jm:
                    used = set(REG.findall(jm.group(1)))
                    bad = tested & clobbered & used
                    if bad:
                        hits.append({
                            "file": os.path.basename(path),
                            "line": i + 1,
                            "regs": sorted(bad),
                            "defer": line.strip(),
                            "jcc": s,
                        })
                    break
                if s.startswith(STOP) or "goto " in s:
                    break
                a = ASSIGN.match(nxt)
                if a:
                    clobbered.add(a.group(1) or a.group(2))
    return scanned, hits


def main(argv):
    scanned, hits = scan()
    print(f"deferred flag sites scanned              : {scanned}")
    print(f"sites whose tested register is clobbered : {len(hits)}")
    if not hits:
        return 0
    print()
    print("by file:")
    for f, n in Counter(h["file"] for h in hits).most_common():
        print(f"  {f:<28} {n}")
    print()
    show = hits if "--list" in argv else hits[:10]
    print(f"{'(all sites)' if '--list' in argv else '(first 10)'}:")
    for h in show:
        print(f"  {h['file']}:{h['line']}  clobbered={','.join(h['regs'])}")
        print(f"      {h['defer']}")
        print(f"      {h['jcc']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
