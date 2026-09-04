#!/usr/bin/env python3
"""guard_bulk_writes.py - catch the bulk write that clobbers a guest address.

Why this exists
---------------
A guest global went to zero and the page-protection watchpoint reported MISSED
WRITES - it cannot see a write that lands while the page is unprotected, which
is what a native memcpy/memset does. The culprit turned out to be a rep stosd
zeroing 54 MB from address 0 because its base pointer was NULL.

Finding it took two passes, and the first one was the lesson. The lifter emits
these operations in TWO shapes:

    memcpy((void*)XBOX_PTR(edi), (void*)XBOX_PTR(esi), ecx * 4);   ~910 sites
    { uint32_t _i; for (_i = 0; _i < ecx; _i++) MEM32(edi + _i*4) = eax; }
                                                                   ~463 sites

Guarding only the first gave a confident ZERO after a full build-and-run, which
is worse than no guard at all. This tool covers both, and refuses to run if it
finds neither - a silent no-op is the failure mode it exists to prevent.

    python3 tools_data/guard_bulk_writes.py 0x5BC528 0x5BC548 --apply
    python3 tools_data/strip_probes.py --apply     # to remove afterwards

Every line it inserts carries /* PROBE */ so strip_probes.py removes it.

Self-check:  python3 tools_data/guard_bulk_writes.py --selftest
"""
import argparse
import glob
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
GEN = os.path.join(GAME, "src", "recomp", "gen")
TAG = "bulk-clobber"

CALL_FORM = re.compile(
    r"^(?P<ind>\s*)(?:memcpy|memset)\(\(void\*\)XBOX_PTR\((?P<dst>[^)]+)\),"
    r".*?,\s*(?P<len>[^;]+)\);\s*$")
LOOP_FORM = re.compile(
    r"^(?P<ind>\s*)\{ uint32_t _i; for \(_i = 0; _i < (?P<n>\w+); _i\+\+\) "
    r"MEM32\((?P<dst>\w+) \+ _i\*4\) = \w+; \}\s*$")


def guard_lines(ind, dst, length_expr, lo, hi):
    return [
        f"{ind}{{ /* PROBE */ uint32_t _bd = (uint32_t)({dst}), "
        f"_bl = (uint32_t)({length_expr});\n",
        f"{ind}  if (_bd < {hex(hi)}u && _bd + _bl > {hex(lo)}u)\n",
        f'{ind}    recomp_where("{TAG}", 12, _bd, _bl, 0, 0);\n',
        f"{ind}}} /* PROBE */\n",
    ]


def process(text, lo, hi):
    out, n_call, n_loop = [], 0, 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        # Idempotent: a bulk line already preceded by a guard block is
        # skipped. Without this a second pass double-guards every site,
        # which the selftest caught.
        already = bool(out) and out[-1].strip() == "} /* PROBE */"
        if TAG not in line and not already:
            m = CALL_FORM.match(stripped)
            if m:
                out += guard_lines(m.group("ind"), m.group("dst"),
                                   m.group("len"), lo, hi)
                n_call += 1
            else:
                m = LOOP_FORM.match(stripped)
                if m:
                    out += guard_lines(m.group("ind"), m.group("dst"),
                                       m.group("n") + " * 4u", lo, hi)
                    n_loop += 1
        out.append(line)
    return "".join(out), n_call, n_loop


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("lo", nargs="?", help="low guest address, e.g. 0x5BC528")
    ap.add_argument("hi", nargs="?", help="high guest address, exclusive")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if not (a.lo and a.hi):
        ap.error("give a low and high guest address, or --selftest")

    lo, hi = int(a.lo, 16), int(a.hi, 16)
    if lo >= hi:
        ap.error("low address must be below high")

    tot_call = tot_loop = 0
    for src in sorted(glob.glob(os.path.join(GEN, "recomp_*.c"))):
        text = io.open(src, encoding="utf-8", errors="replace").read()
        new, nc, nl = process(text, lo, hi)
        tot_call += nc
        tot_loop += nl
        if (nc or nl) and a.apply:
            io.open(src, "w", encoding="utf-8", newline="\n").write(new)

    print(f"call-form sites : {tot_call}")
    print(f"loop-form sites : {tot_loop}")
    if not (tot_call or tot_loop):
        print("\nNO bulk sites matched either shape. That is almost certainly a\n"
              "changed emitter, not a tree without bulk writes - fix the patterns\n"
              "before trusting any result.")
        return 2
    if a.apply:
        print(f"\nguarded {tot_call + tot_loop} site(s); build, run, then\n"
              f"grep the log for {TAG} and strip with strip_probes.py --apply")
    else:
        print(f"\n{tot_call + tot_loop} site(s) would be guarded; re-run with --apply")
    return 0


def selftest():
    """Both shapes must be matched. The loop form is the regression."""
    sample = (
        "    memcpy((void*)XBOX_PTR(edi), (void*)XBOX_PTR(esi), ecx * 4);\n"
        "    { uint32_t _i; for (_i = 0; _i < ecx; _i++) MEM32(edi + _i*4) = eax; }\n"
        "    memset((void*)XBOX_PTR(edi), (uint8_t)eax, ecx);\n"
        "    eax = ecx;\n"
    )
    out, nc, nl = process(sample, 0x5BC528, 0x5BC548)
    assert nc == 2, f"call form: expected 2, got {nc}"
    assert nl == 1, f"loop form: expected 1, got {nl}"
    assert out.count("/* PROBE */") == 6, out.count("/* PROBE */")
    # the plain line is untouched
    assert "    eax = ecx;\n" in out
    # idempotent: a second pass adds nothing
    out2, nc2, nl2 = process(out, 0x5BC528, 0x5BC548)
    assert (nc2, nl2) == (0, 0), (nc2, nl2)
    print("selftest ok - both shapes matched, idempotent, plain lines untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
