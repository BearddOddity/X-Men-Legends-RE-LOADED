#!/usr/bin/env python3
"""fix_sbb_carry.py - give `sbb reg, reg` the carry flag that `neg` just set.

The bug
-------
`neg r; sbb r, r; inc r` is the standard x86 idiom for "was r zero?". It works
because `neg` sets CF to 1 exactly when its operand was non-zero, so `sbb r, r`
leaves 0 or -1 and `inc` turns that into 1 or 0.

The lifter emits the `neg` and the `sbb` but never the flag between them:

    eax = eax - esi;
    eax = (uint32_t)(-(int32_t)eax);
    eax = _cf ? 0xFFFFFFFF : 0;   /* sbb self (CF extend) */
    eax++;

`_cf` is a per-function local initialised to 0 and, in 353 of the 354 functions
that read it, never assigned anywhere. So the conditional always takes the
zero branch and the whole idiom collapses to a constant: `eax = 1`, whatever
the comparison actually said. Every predicate built this way silently returns
"equal".

That is not a corner case. sub_001FBA90 is a pointer-identity test on the
engine's boot path, and this defect made it answer yes to everything.

The fix
-------
Restore the one missing assignment, immediately before the `neg`, reading the
operand while it still holds its pre-negation value:

    _cf = (eax != 0);             /* neg sets CF when the operand is non-zero */
    eax = (uint32_t)(-(int32_t)eax);
    eax = _cf ? 0xFFFFFFFF : 0;   /* sbb self (CF extend) */

Scope, deliberately narrow
--------------------------
Only a `neg` on the line IMMEDIATELY above the `sbb` is rewritten, with no
label between them. That pairing is the complete idiom in one basic block, so
the carry is knowable with certainty.

The other ~135 sites read `_cf` after a `cmp` or `test`, or after a label - a
different basic block can reach them, and the carry then depends on which path
arrived. Those need a per-site reading and are left alone: writing a plausible
value there would be inventing data to satisfy a check, which this project does
not do.

8- and 16-bit negs are handled at their own width, because `neg al` sets CF
from the low byte alone.

Scope note
----------
This rewrites gen/*.c, which is generated. Like fix_stale_flags.py, run it as a
pipeline step after a regeneration - the edits carry no manual-edit marker
because they replace generator output rather than sit beside it.

Usage (from src/game/):
    py -3 tools_data/fix_sbb_carry.py            # report only
    py -3 tools_data/fix_sbb_carry.py --apply
    py -3 tools_data/fix_sbb_carry.py --self-test
"""
import argparse
import glob
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(os.path.dirname(HERE), "src", "recomp", "gen")

REG = r"(?:eax|ebx|ecx|edx|esi|edi|ebp|esp)"

# The three `neg` forms the lifter emits, each with the expression whose
# non-zeroness is the carry.
NEG_FORMS = (
    (re.compile(r"^(\s*)(%s) = \(uint32_t\)\(-\(int32_t\)(%s)\);$" % (REG, REG)),
     "{r}"),
    (re.compile(r"^(\s*)SET_LO8\((%s), \(uint32_t\)\(-\(int32_t\)LO8\((%s)\)\)\);$" % (REG, REG)),
     "LO8({r})"),
    (re.compile(r"^(\s*)SET_LO16\((%s), \(uint32_t\)\(-\(int32_t\)LO16\((%s)\)\)\);$" % (REG, REG)),
     "LO16({r})"),
)

SBB_RE = re.compile(r"^\s*(?:%s = _cf \?|SET_LO8\(%s, _cf \?)" % (REG, REG))
ALREADY_RE = re.compile(r"^\s*_cf = \(")
# This tool's signature, and the only thing --revert is allowed to delete.
MARK = "see fix_sbb_carry.py"


def carry_line(neg_line):
    """The `_cf = ...` line a neg needs, or None if this is not a neg."""
    for pat, operand in NEG_FORMS:
        m = pat.match(neg_line)
        if not m:
            continue
        indent, dst, src = m.group(1), m.group(2), m.group(3)
        if dst != src:                 # not a self-neg; not this idiom
            return None
        return ("%s_cf = (%s != 0); /* neg sets CF when the operand is "
                "non-zero - see fix_sbb_carry.py */"
                % (indent, operand.format(r=src)))
    return None


def fix_lines(lines):
    """Return (new_lines, fixed, skipped). Pure, so the self-test can drive it."""
    out, fixed, skipped = [], 0, 0
    for i, line in enumerate(lines):
        if SBB_RE.match(line):
            prev = out[-1] if out else ""
            cl = carry_line(prev)
            if cl is not None and not (len(out) > 1 and ALREADY_RE.match(out[-2])):
                out.insert(len(out) - 1, cl)
                fixed += 1
            elif cl is None:
                skipped += 1
        out.append(line)
    return out, fixed, skipped


def self_test():
    src = [
        "    eax = eax - esi;",
        "    eax = (uint32_t)(-(int32_t)eax);",
        "    eax = _cf ? 0xFFFFFFFF : 0; /* sbb self (CF extend) */",
        "    eax++;",
        "    SET_LO8(ecx, (uint32_t)(-(int32_t)LO8(ecx)));",
        "    SET_LO8(ecx, _cf ? 0xFFFFFFFF : 0); /* sbb self (CF extend) */",
        "",
        "loc_00065585: ;",
        "    edx = _cf ? 0xFFFFFFFF : 0; /* sbb self (CF extend) */",
        "    (void)0; /* cmp LO8(edx), LO8(ecx) - flags set for next jcc */",
        "    esi = _cf ? 0xFFFFFFFF : 0; /* sbb self (CF extend) */",
        "    ebx = (uint32_t)(-(int32_t)edx);",
        "    ebx = _cf ? 0xFFFFFFFF : 0; /* sbb self (CF extend) */",
    ]
    out, fixed, skipped = fix_lines(src)
    assert fixed == 2, fixed
    # A label above, a cmp above, and a neg of a DIFFERENT register are all left alone.
    assert skipped == 3, skipped
    assert out[1] == "    _cf = (eax != 0); /* neg sets CF when the operand is non-zero - see fix_sbb_carry.py */", out[1]
    assert out[5] == "    _cf = (LO8(ecx) != 0); /* neg sets CF when the operand is non-zero - see fix_sbb_carry.py */", out[5]
    # Idempotent: a second pass changes nothing.
    again, f2, _ = fix_lines(out)
    assert f2 == 0 and again == out
    print("self-test ok: 2 fixed, 3 correctly left for a human, idempotent")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", action="store_true",
                    help="remove the carry assignments this tool inserted. The "
                         "fix is correct, but it changes which branch real code "
                         "takes, so being able to take it back out in one "
                         "command is what makes it measurable.")
    ap.add_argument("--file", action="append", default=[],
                    help="restrict to these gen file basenames (repeatable), to "
                         "measure one file's worth of the fix on its own")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--gen-dir", default=GEN)
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()

    tot_fixed = tot_skipped = 0
    for path in sorted(glob.glob(os.path.join(a.gen_dir, "recomp_*.c"))):
        if a.file and os.path.basename(path) not in a.file:
            continue
        raw = io.open(path, encoding="utf-8", errors="ignore", newline="").read()
        crlf = "\r\n" in raw
        lines = raw.replace("\r\n", "\n").split("\n")
        if a.revert:
            # Key on THIS tool's own marker, never on the `_cf = (` shape.
            # Keying on the shape once deleted a hand-written carry fix that
            # ledger #136 had put at sub_001FBA90 - a correct edit this tool
            # deliberately skips, and then silently removed.
            out = [l for l in lines if MARK not in l]
            fixed, skipped = len(lines) - len(out), 0
        else:
            out, fixed, skipped = fix_lines(lines)
        tot_fixed += fixed
        tot_skipped += skipped
        if fixed:
            print("  %-26s %4d fixed  %4d left for a human" %
                  (os.path.basename(path), fixed, skipped))
        if fixed and (a.apply or a.revert):
            text = "\n".join(out)
            if crlf:
                text = text.replace("\n", "\r\n")
            io.open(path, "w", encoding="utf-8", newline="").write(text)

    print()
    print("%d %s" % (tot_fixed, "carry assignment(s) removed" if a.revert
                     else "sbb site(s) follow a neg in the same block - carry restored"))
    print("%d sbb site(s) take their carry from a cmp/test or another block "
          "- left alone on purpose" % tot_skipped)
    if not (a.apply or a.revert):
        print("\nreport only; pass --apply to rewrite")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
