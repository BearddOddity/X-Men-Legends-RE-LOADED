#!/usr/bin/env python3
"""neuter_seed.py - turn one seeded function back into a do-nothing stub.

Why this exists
---------------
seed_missing_functions.py re-translates every recorded address on each --apply,
so bisecting "which of these five new seeds regressed the boot?" by re-running
it costs about five minutes per candidate. That is slow enough that the honest
answer becomes "revert them all", which throws away the good ones with the bad.

This edits recomp_seed.c in place instead: the seeded body is renamed out of
the way and a stub with the original symbol's name is put in front of it, so
one rebuild (seconds) restores exactly the pre-seed behaviour for exactly one
function. --restore puts it back.

Nothing is deleted - the real body stays in the file under <sym>__seeded - so a
neutered seed can always be restored without re-running the seeder.

Usage (from src/game/):
    py -3 tools_data/neuter_seed.py sub_001995AD [sub_...]
    py -3 tools_data/neuter_seed.py --restore sub_001995AD
    py -3 tools_data/neuter_seed.py --list
"""
import argparse
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.join(os.path.dirname(HERE), "src", "recomp", "gen", "recomp_seed.c")
TAG = "/* NEUTERED by neuter_seed.py - real body follows as __seeded */"


# recomp_seed.c is rewritten by several tools, and they do not agree on line
# endings: seed_missing_functions.py writes LF, manual_edits.py writes CRLF.
# Matching a bare newline therefore silently found nothing after a manual_edits
# run and reported "no body found" for a function that was plainly there.
# Normalise on read, and put the file's own ending back on write.
_CRLF = [False]

LF = chr(10)
CRLF = chr(13) + chr(10)


def read():
    raw = io.open(SEED, encoding="utf-8", newline="").read()
    _CRLF[0] = CRLF in raw
    return raw.replace(CRLF, LF)


def write(s):
    if _CRLF[0]:
        s = s.replace(LF, CRLF)
    io.open(SEED, "w", encoding="utf-8", newline="").write(s)


def neuter(s, sym):
    if "void %s__seeded(void)" % sym in s:
        return s, "already neutered"
    m = re.search(r"^void %s\(void\)\n\{" % sym, s, re.M)
    if not m:
        return s, "no body found in recomp_seed.c"
    stub = "void %s(void) { g_esp += 4; } %s\nvoid %s__seeded(void)\n{" % (sym, TAG, sym)
    return s[:m.start()] + stub + s[m.end():], "neutered"


def restore(s, sym):
    pat = re.compile(r"^void %s\(void\) \{ g_esp \+= 4; \} %s\nvoid %s__seeded\(void\)\n\{"
                     % (sym, re.escape(TAG), sym), re.M)
    if not pat.search(s):
        return s, "not neutered"
    return pat.sub("void %s(void)\n{" % sym, s, count=1), "restored"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    s = read()
    if a.list:
        for m in re.finditer(r"^void (sub_[0-9A-F]+)__seeded\(void\)", s, re.M):
            print(m.group(1))
        return 0
    if not a.symbols:
        ap.error("give at least one symbol, or --list")
    for sym in a.symbols:
        s, what = (restore if a.restore else neuter)(s, sym)
        print("%-16s %s" % (sym, what))
    write(s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
