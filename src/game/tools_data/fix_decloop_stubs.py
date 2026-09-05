"""fix_decloop_stubs.py - restore the table-fill loops the lifter truncates.

The defect
----------
The compiler emits this shape for "fill a table, then step back one and fill the
last slot":

    jmp   .+3               ; skips the dec on the normal path
    dec   %eax
    mov   %edx,OFFSET(%ecx,%eax,4)
    inc   %edx
    cmp   $LIMIT,%edx
    jl    <top of loop>
    pop   %edi ; pop %esi ; ret

The `dec %eax` is reachable ONLY through that short jump, never through a call,
so the lifter does not recognise it as the start of anything and emits an
unresolved stub for it - a body of `g_esp += 4;`, which simulates a `ret`.

The caller then reads as:

    if (CMP_L(eax, LIMIT)) { g_seh_ebp = ebp; sub_<A>(); return; }

so on the branch that should step back and keep filling, the routine RETURNS
instead. The table is never filled, and the caller's own `pop %edi / pop %esi`
never run, so two saved registers escape holding whatever the loop left in them.
One of those escaping values was a loop counter of 4, which travelled on as a
pointer and produced a wall that cost days (ledger #219, #220, #221).

The repair
----------
Transcribe the two instructions the original actually has:

    if (CMP_L(eax, LIMIT)) { eax = eax - 1; g_seh_ebp = ebp; sub_<A+1>(); return; }

`sub_<A+1>` is the fall-through target and is already a lifted function in every
case, which this tool requires before it will touch a site.

Evidence
--------
Seven of these were repaired one at a time, each read from the real game in xemu
first, and six more were sampled across six different generated files. Thirteen
for thirteen, no exceptions: every one is `dec %eax` followed by
`mov %edx,OFFSET(%ecx,%eax,4)`.

That is evidence about a CLASS, not proof about each remaining site. Verify
before you widen further:

    python3 oracle.py --disas 0x<addr>:3 --stop-at 0x001E8E20

Usage (from src/game/):
    py -3 tools_data/fix_decloop_stubs.py                    # list what matches
    py -3 tools_data/fix_decloop_stubs.py --only 000147EC --apply
    py -3 tools_data/fix_decloop_stubs.py --apply            # the whole class

Staging is deliberate: --only exists so a site can be proven on its own first.
Idempotent - a repaired site no longer matches.
"""
import argparse
import glob
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(os.path.dirname(HERE), "src", "recomp", "gen")
STUBS = os.path.join(GEN, "recomp_stubs_unresolved.c")

SITE = re.compile(
    # The limit is emitted as decimal when it is small (`5`, not `0x5`), so an
    # 0x-only pattern silently skips those sites. One was missed that way.
    r"^(?P<ind>\s*)if \(CMP_L\(eax, (?P<lim>0x[0-9A-F]+|\d+)\)\) \{ g_seh_ebp = ebp; "
    r"sub_(?P<addr>[0-9A-F]{8})\(\); return; \}(?P<tail>.*)$")

MARK = "/* Manual fix (restores original x86)"


def stub_addresses():
    if not os.path.exists(STUBS):
        return set()
    txt = io.open(STUBS, encoding="utf-8", errors="replace").read()
    return set(re.findall(r"^void sub_([0-9A-F]{8})\(void\) \{ (?:recomp_stub_hit|g_esp)",
                          txt, re.M))


def lifted_functions():
    out = set()
    for p in glob.glob(os.path.join(GEN, "*.c")):
        out |= set(re.findall(r"^void (sub_[0-9A-F]{8})\(void\)$",
                              io.open(p, encoding="utf-8", errors="replace").read(), re.M))
    return out


def find(only):
    stubs, lifted, hits = stub_addresses(), lifted_functions(), []
    for path in sorted(glob.glob(os.path.join(GEN, "*.c"))):
        if path.endswith("recomp_stubs_unresolved.c"):
            continue
        lines = io.open(path, encoding="utf-8", errors="replace").read().split("\n")
        for i, line in enumerate(lines):
            m = SITE.match(line)
            if not m:
                continue
            addr = m.group("addr")
            if addr not in stubs:
                continue
            nxt = "sub_%08X" % (int(addr, 16) + 1)
            if nxt not in lifted:
                continue          # no fall-through target: not this idiom
            if only and addr not in only:
                continue
            hits.append((path, i, m, nxt))
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated stub addresses")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    only = {x.strip().upper().replace("0X", "") for x in a.only.split(",") if x.strip()}

    hits = find(only)
    if not hits:
        print("no matching sites (already repaired, or --only matched nothing)")
        return 0

    by_file = {}
    for path, i, m, nxt in hits:
        by_file.setdefault(path, []).append((i, m, nxt))

    for path, items in by_file.items():
        print(os.path.basename(path))
        lines = io.open(path, encoding="utf-8", errors="replace").read().split("\n")
        for i, m, nxt in items:
            ind, lim, addr, tail = (m.group("ind"), m.group("lim"),
                                    m.group("addr"), m.group("tail"))
            new = ("%s%s: 0x%s is \"dec %%eax\" falling into 0x%s,\n"
                   "%s   NOT a return - the table-fill idiom. Verified as a class\n"
                   "%s   against the original in xemu; see the module docstring. */\n"
                   "%sif (CMP_L(eax, %s)) { eax = eax - 1; g_seh_ebp = ebp; %s(); return; }%s"
                   % (ind, MARK, addr, nxt[4:], ind, ind, ind, lim, nxt, tail))
            print("  0x%s -> %s  (limit %s)" % (addr, nxt, lim))
            if a.apply:
                lines[i] = new
        if a.apply:
            io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines))

    print("\n%d site(s)%s" % (len(hits), "" if a.apply else " - dry run, pass --apply"))
    if a.apply:
        print("applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
