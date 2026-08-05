#!/usr/bin/env python3
"""Find lifted functions that destroy a callee-saved register.

Why this exists
---------------
ebx, esi and edi are callee-saved. In this port they are globals, so the
save/restore contract is not enforced by C scoping - it is carried entirely by
the PUSH32/POP32 pairs the lifter emits. If a function writes one of them
without a matching pair, the value is gone for every caller above it, and the
damage lands arbitrarily far from the cause.

That is not hypothetical. `_initterm` walks the CRT initialiser table with the
table pointer in esi; one initialiser clobbered it, so the loop left the table
and began calling dwords off the stack - including the address of
_except_handler3, which is how a boot with no exception in it ended up inside
the SEH unwinder.

What it reports
---------------
For each function, per register:
  clobber   - written, but never saved and restored. The caller's value is lost.
  unbalanced- saved and restored a different number of times.
Both are reported with the writing lines so the claim can be checked.

Fragments are excluded by default. The lifter splits some functions at
branch targets, and a fragment legitimately inherits a half-finished frame -
its pushes are matched by a pop in a sibling. They are recognised by ending in
a tail call (`sub_XXXX(); return;`) or by carrying the fpo_leaf inheritance
line. Pass --include-fragments to see them anyway.

Run from src/game/:
    py -3 tools_data/find_reg_clobbers.py
    py -3 tools_data/find_reg_clobbers.py --only sub_003556E0 --callees
    py -3 tools_data/find_reg_clobbers.py --reg esi --limit 40
"""
import argparse
import os
import re
import sys

GEN = os.path.join("src", "recomp", "gen")
SAVED = ("ebx", "esi", "edi")

FUNC_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*$")
CALL_RE = re.compile(r"\b(sub_[0-9A-Fa-f]+)\(\)")
TAILCALL_RE = re.compile(r"^\s*(?:g_seh_ebp = ebp; )?sub_[0-9A-Fa-f]+\(\); return;")


def push_re(r):
    return re.compile(r"\bPUSH32\(esp,\s*%s\s*\)" % r)


def pop_re(r):
    return re.compile(r"\bPOP32\(esp,\s*%s\s*\)" % r)


def write_re(r):
    # An assignment to the bare register: `esi = ...`, `esi += ...`, `esi++`.
    # Excludes MEM32(esi) = ... , which writes memory, not the register.
    return re.compile(r"^\s*%s\s*(?:=[^=]|\+=|-=|\*=|/=|\|=|&=|\^=|<<=|>>=|\+\+|--)" % r)


PUSH = {r: push_re(r) for r in SAVED}
POP = {r: pop_re(r) for r in SAVED}
WRITE = {r: write_re(r) for r in SAVED}


def parse(path):
    """Yield (name, [lines]) for every function definition in a file."""
    lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
    i = 0
    while i < len(lines):
        m = FUNC_RE.match(lines[i])
        if not m:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j] != "}":
            j += 1
        yield m.group(1), lines[i:j]
        i = j + 1


def analyse(body):
    """Return {reg: (pushes, pops, [write lines])} and whether it is a fragment."""
    out = {}
    for r in SAVED:
        pushes = pops = 0
        writes = []
        for l in body:
            if l.lstrip().startswith("/*") or l.lstrip().startswith("*"):
                continue
            if PUSH[r].search(l):
                pushes += 1
            if POP[r].search(l):
                pops += 1
                continue          # a pop is a restore, not a clobber
            if WRITE[r].match(l):
                writes.append(l.strip())
        out[r] = (pushes, pops, writes)

    fragment = any(TAILCALL_RE.match(l) for l in body) or \
        any("fpo_leaf: inherit caller's frame" in l for l in body)
    return out, fragment


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reg", choices=SAVED, help="restrict to one register")
    ap.add_argument("--only", help="start from this function")
    ap.add_argument("--callees", action="store_true",
                    help="with --only, walk everything it can reach")
    ap.add_argument("--depth", type=int, default=6,
                    help="how deep --callees walks (default 6)")
    ap.add_argument("--include-fragments", action="store_true")
    ap.add_argument("--limit", type=int, default=30)
    a = ap.parse_args()

    if not os.path.isdir(GEN):
        sys.exit("run from src/game/ - no %s" % GEN)

    bodies, calls = {}, {}
    for fn in sorted(os.listdir(GEN)):
        if not fn.endswith(".c"):
            continue
        for name, body in parse(os.path.join(GEN, fn)):
            bodies[name] = (fn, body)
            calls[name] = {c for l in body for c in CALL_RE.findall(l)}

    print("scanned %d functions" % len(bodies))

    wanted = set(bodies)
    if a.only:
        if a.only not in bodies:
            sys.exit("%s not found" % a.only)
        if a.callees:
            wanted, frontier = {a.only}, {a.only}
            for _ in range(a.depth):
                nxt = set()
                for f in frontier:
                    nxt |= calls.get(f, set()) - wanted
                if not nxt:
                    break
                wanted |= nxt
                frontier = nxt
            print("reachable from %s within depth %d: %d functions"
                  % (a.only, a.depth, len(wanted)))
        else:
            wanted = {a.only}

    regs = [a.reg] if a.reg else list(SAVED)
    findings = []
    frags = 0
    for name in sorted(wanted):
        if name not in bodies:
            continue
        fn, body = bodies[name]
        res, fragment = analyse(body)
        if fragment and not a.include_fragments:
            frags += 1
            continue
        for r in regs:
            pushes, pops, writes = res[r]
            if writes and pushes == 0 and pops == 0:
                findings.append(("clobber", name, fn, r, pushes, pops, writes))
            elif pushes != pops:
                findings.append(("unbalanced", name, fn, r, pushes, pops, writes))

    # Clobbers first: an unbalanced pair is often a fragment the heuristic
    # missed, but a plain clobber is always a lost caller value.
    findings.sort(key=lambda f: (f[0] != "clobber", f[1]))

    if frags:
        print("skipped %d fragment(s) (--include-fragments to see them)" % frags)
    if not findings:
        print("no callee-save violations found")
        return

    print("\n%d finding(s):\n" % len(findings))
    for kind, name, fn, r, pushes, pops, writes in findings[:a.limit]:
        print("%-11s %s  [%s]  %s   push=%d pop=%d"
              % (kind, name, fn, r, pushes, pops))
        for w in writes[:3]:
            print("      %s" % w)
        if len(writes) > 3:
            print("      ... %d more write(s)" % (len(writes) - 3))
        print("")
    if len(findings) > a.limit:
        print("... %d more (raise --limit)" % (len(findings) - a.limit))


if __name__ == "__main__":
    main()
