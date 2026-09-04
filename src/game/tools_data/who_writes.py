#!/usr/bin/env python3
"""who_writes.py - which functions write a guest address, and can a given
function reach any of them?

Why this exists
---------------
Twice now the question has been "the watchpoint says this global changed across
sub_XXXX, so what under sub_XXXX writes it?", and twice it was answered by hand
with a grep and a mental call graph. The grep part is easy; the reachability
part is where it goes wrong.

It goes wrong for one specific reason, which this tool exists to get right: the
lifter emits a tail jump as a bare `sub_XXXX(); return;`, NOT as
RECOMP_ABI_CALL. A walker that only follows RECOMP_ABI_CALL misses every
fragment chain, and fragment chains are most of this codebase. The first version
of this walker reported "nothing is reachable" from a function whose entire body
is a tail call, which is the most confidently wrong answer it could have given.

    python3 tools_data/who_writes.py 0x5BC528
    python3 tools_data/who_writes.py 0x5BC528 --from sub_00202B60
    python3 tools_data/who_writes.py 0x5BC544 --from sub_00123590 --depth 20

Limits, stated because they change how the answer should be read:
  - Direct and tail calls only. An INDIRECT call is invisible here, so "not
    reachable" means "not reachable without a virtual dispatch", never "cannot
    happen".
  - Literal writes only. A store through a register holding the address -
    MEM32(eax) = 0 where eax is the global - is not found by any textual search.

Self-check:  python3 tools_data/who_writes.py --selftest
"""
import argparse
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
GEN = os.path.join(GAME, "src", "recomp", "gen")

FUNC = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)$")
# Both call shapes. Group 1 is a direct call, group 2 a tail jump.
CALL = re.compile(r"RECOMP_ABI_CALL\((sub_[0-9A-Fa-f]+)\)"
                  r"|(?:^|[;{ ])(sub_[0-9A-Fa-f]+)\(\); return;")


def load_bodies(gen_dir=GEN):
    bodies = {}
    for src in sorted(glob.glob(os.path.join(gen_dir, "recomp_*.c"))):
        cur, buf = None, []
        with open(src, errors="replace") as fh:
            for line in fh:
                m = FUNC.match(line.rstrip("\n"))
                if m:
                    if cur:
                        bodies[cur] = (os.path.basename(src), "".join(buf))
                    cur, buf = m.group(1), []
                elif cur:
                    buf.append(line)
        if cur:
            bodies[cur] = (os.path.basename(src), "".join(buf))
    return bodies


def callees(body):
    return {a or b for a, b in CALL.findall(body)}


def reach(bodies, root, max_depth):
    seen = {root: 0}
    frontier, depth = [root], 0
    while frontier and depth < max_depth:
        depth += 1
        nxt = []
        for fn in frontier:
            if fn not in bodies:
                continue
            for c in callees(bodies[fn][1]):
                if c not in seen:
                    seen[c] = depth
                    nxt.append(c)
        frontier = nxt
    return seen


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("address", nargs="?", help="guest address, e.g. 0x5BC528")
    ap.add_argument("--from", dest="root", help="only report writers this function can reach")
    ap.add_argument("--depth", type=int, default=12)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if not a.address:
        ap.error("give a guest address, or --selftest")

    addr = int(a.address, 16)
    needle = "MEM32(0x%X) =" % addr
    bodies = load_bodies()
    writers = sorted(fn for fn, (s, b) in bodies.items() if needle in b)

    print(f"{len(bodies)} lifted functions; {len(writers)} write {needle.strip(' =')}")
    for w in writers:
        print(f"    {w}  [{bodies[w][0]}]")

    if not a.root:
        return 0

    seen = reach(bodies, a.root, a.depth)
    hits = sorted((seen[w], w) for w in writers if w in seen)
    print(f"\nfrom {a.root}, following direct AND tail calls to depth "
          f"{a.depth}: {len(seen)} functions reachable")
    if hits:
        for d, w in hits:
            print(f"    depth {d}: {w}")
    else:
        print("    none of the writers is reachable.")
        print("    Read that as 'not without an indirect call' - virtual "
              "dispatch is invisible here - and note a store through a "
              "register holding the address would not be found at all.")
    return 0


def selftest():
    """The regression that matters: a body whose only call is a tail jump."""
    import tempfile
    src = (
        "void sub_00000001(void)\n"
        "{\n"
        "    sub_00000002(); return;\n"   # tail call - the case that was missed
        "}\n"
        "void sub_00000002(void)\n"
        "{\n"
        "    PUSH32(esp, 0); RECOMP_ABI_CALL(sub_00000003);\n"
        "}\n"
        "void sub_00000003(void)\n"
        "{\n"
        "    MEM32(0x5BC528) = eax;\n"
        "}\n"
        "void sub_00000004(void)\n"
        "{\n"
        "    eax = 0;\n"
        "}\n"
    )
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "recomp_0000.c"), "w") as fh:
        fh.write(src)
    bodies = load_bodies(d)
    assert set(bodies) == {"sub_00000001", "sub_00000002",
                           "sub_00000003", "sub_00000004"}, sorted(bodies)
    # tail call followed
    assert callees(bodies["sub_00000001"][1]) == {"sub_00000002"}
    # direct call followed
    assert callees(bodies["sub_00000002"][1]) == {"sub_00000003"}
    seen = reach(bodies, "sub_00000001", 12)
    assert seen["sub_00000003"] == 2, seen
    # an unrelated function is not dragged in
    assert "sub_00000004" not in seen
    print("selftest ok - tail calls followed, depth correct")
    return 0


if __name__ == "__main__":
    sys.exit(main())
