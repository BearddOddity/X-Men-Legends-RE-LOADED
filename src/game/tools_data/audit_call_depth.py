"""Judge the [ABI-DEPTH] census against each callee's own epilogue.

The census in abi_stderr.txt records the esp delta every executed direct call
site returned at. It deliberately does not judge, because the correct answer is
the callee's epilogue constant N in `esp += N; return;`, which the call site
cannot see. This reads N out of gen/ and does the comparison.

    delta > N   the callee popped more than it owns -- an OVER-POP, the shape
                behind the current wall.
    delta < N   arguments the caller has not cleaned yet; normal for cdecl and
                not a defect on its own.
    delta == N  correct.

Over-pops are ranked by call depth so the ORIGIN can be told from the frames
that merely inherit it: a function that over-pops is reported alongside whether
any of its own callees also over-pop. The deepest over-popper whose callees are
all clean is the one to fix.

Run from src/game/:  python3 tools_data/audit_call_depth.py
"""
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
GEN = os.path.join(GAME, "src", "recomp", "gen")
LOG = os.path.join(GAME, "abi_stderr.txt")

DEPTH_RE = re.compile(r"^\s+([+-]\d+)\s+(sub_[0-9A-Fa-f]+)\s+at\s+(.*):(\d+)\s*$")
FUNC_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)$")
RET_RE = re.compile(r"^\s+esp \+= (\d+); return;")
CALL_RE = re.compile(r"RECOMP_ABI_CALL\((sub_[0-9A-Fa-f]+)\)")


def load_gen():
    """Return (epilogue constant per function, direct callees per function)."""
    pops, callees = {}, defaultdict(set)
    for name in sorted(os.listdir(GEN)):
        if not (name.startswith("recomp_") and name.endswith(".c")):
            continue
        cur = None
        with open(os.path.join(GEN, name), errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                m = FUNC_RE.match(line)
                if m:
                    cur = m.group(1)
                    continue
                if cur is None:
                    continue
                m = RET_RE.match(line)
                if m:
                    n = int(m.group(1))
                    # A function has one convention; take the largest seen so a
                    # partial read cannot understate what it pops.
                    pops[cur] = max(pops.get(cur, 0), n)
                m = CALL_RE.search(line)
                if m:
                    callees[cur].add(m.group(1))
    return pops, callees


def load_census():
    rows = []
    if not os.path.exists(LOG):
        sys.exit(f"no log at {LOG}")
    seen_header = False
    with open(LOG, errors="replace") as fh:
        for line in fh:
            if line.startswith("[ABI-DEPTH]"):
                seen_header = True
                continue
            if not seen_header:
                continue
            m = DEPTH_RE.match(line.rstrip("\n"))
            if m:
                rows.append((int(m.group(1)), m.group(2),
                             os.path.basename(m.group(3)), int(m.group(4))))
            elif line.startswith("["):
                break  # next section
    return rows


def main():
    pops, callees = load_gen()
    rows = load_census()
    if not rows:
        sys.exit("no [ABI-DEPTH] section found - is this an ABI build's log?")

    over, unknown = [], []
    for delta, fn, fname, line in rows:
        n = pops.get(fn)
        if n is None:
            unknown.append((fn, fname, line))
            continue
        if delta > n:
            over.append((delta - n, fn, n, delta, fname, line))

    bad = {o[1] for o in over}
    print(f"{len(rows)} executed direct call sites in the census, "
          f"{len(pops)} callees with a known epilogue")
    print(f"{len(over)} site(s) where the callee popped MORE than it owns")
    if unknown:
        print(f"{len(unknown)} site(s) whose callee has no epilogue in gen/ "
              f"(seeded or stubbed); not judged")
    print()

    over.sort(reverse=True)
    for excess, fn, n, delta, fname, line in over:
        inner = sorted(bad & callees.get(fn, set()))
        mark = ("inherits from " + ", ".join(inner)) if inner else "ORIGIN"
        print(f"  over-pop {excess:<4} {fn}  owns {n}, returned {delta:+}  "
              f"({fname}:{line})")
        print(f"{'':>15}{mark}")

    origins = sorted({fn for _, fn, _, _, _, _ in over
                      if not (bad & callees.get(fn, set()))})
    print(f"\nORIGINS (over-pop, no over-popping direct callee): "
          f"{', '.join(origins) if origins else 'none'}")


if __name__ == "__main__":
    main()
