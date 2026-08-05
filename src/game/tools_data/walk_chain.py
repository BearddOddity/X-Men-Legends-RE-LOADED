#!/usr/bin/env python3
"""Follow a lifter tail-call chain and account for the callee-saved registers.

Why this exists
---------------
The lifter splits some functions at branch targets. The pieces are joined by
tail calls (`sub_XXXX(); return;`), so a `PUSH32(esp, esi)` in one fragment is
matched by a `POP32(esp, esi)` in another. Looking at either piece alone tells
you nothing - which is why find_reg_clobbers.py skips fragments by default,
and why RECOMP_CHECK_ABI reports them as violations whether or not they are.

This walks the chain from a starting fragment and sums the pushes and pops
across every reachable piece, per path. A path that ends with more pushes than
pops has genuinely lost the caller's value.

Run from src/game/:
    py -3 tools_data/walk_chain.py sub_001183A0
    py -3 tools_data/walk_chain.py sub_001183A0 --reg esi --max-depth 40
"""
import argparse
import os
import re
import sys

GEN = os.path.join("src", "recomp", "gen")
SRC = "src"
SAVED = ("ebx", "esi", "edi")

FUNC_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*$")
TAIL_RE = re.compile(
    r"^\s*(?:g_seh_ebp = ebp; )?(?:RECOMP_ABI_CALL\()?(sub_[0-9A-Fa-f]+)\)?\(?\)?; return;")
CALL_RE = re.compile(r"(?:RECOMP_ABI_CALL\(|\b)(sub_[0-9A-Fa-f]+)[()]")


def load():
    bodies = {}
    for d in (GEN, SRC):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".c"):
                continue
            path = os.path.join(d, fn)
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
                bodies[m.group(1)] = (fn, lines[i:j])
                i = j + 1
    return bodies


def frag_info(body, reg):
    """(pushes, pops, [tail successors]) for one fragment."""
    push = re.compile(r"\bPUSH32\(esp,\s*%s\s*\)" % reg)
    pop = re.compile(r"\bPOP32\(esp,\s*%s\s*\)" % reg)
    p = q = 0
    tails = []
    for l in body:
        s = l.strip()
        if s.startswith("/*") or s.startswith("*"):
            continue
        if push.search(l):
            p += 1
        if pop.search(l):
            q += 1
        m = TAIL_RE.match(l)
        if m:
            tails.append(m.group(1))
    return p, q, tails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start")
    ap.add_argument("--reg", default="esi", choices=SAVED)
    ap.add_argument("--max-depth", type=int, default=30)
    a = ap.parse_args()

    bodies = load()
    if a.start not in bodies:
        sys.exit("%s not found (it may be a one-line stub or an override)"
                 % a.start)

    print("chain from %s, tracking %s\n" % (a.start, a.reg))

    # Depth-first over tail successors, carrying the running balance.
    # Cycles are cut rather than followed - a loop cannot change the net.
    worst = []
    stack = [(a.start, 0, [])]
    seen_paths = 0
    while stack:
        name, bal, path = stack.pop()
        if name not in bodies:
            print("  %s%s -> NOT FOUND (stub or override) "
                  "- balance %+d at this point" % ("  " * len(path), name, bal))
            worst.append((bal, path + [name]))
            continue
        if name in path or len(path) > a.max_depth:
            continue
        fn, body = bodies[name]
        p, q, tails = frag_info(body, a.reg)
        bal2 = bal + p - q
        mark = ""
        if p or q:
            mark = "  push=%d pop=%d -> balance %+d" % (p, q, bal2)
        print("  %s%s [%s]%s" % ("  " * len(path), name, fn, mark))
        if not tails:
            seen_paths += 1
            if bal2 != 0:
                print("  %sEND with balance %+d  <-- %s NOT RESTORED"
                      % ("  " * (len(path) + 1), bal2, a.reg))
                worst.append((bal2, path + [name]))
            else:
                print("  %sEND, balanced" % ("  " * (len(path) + 1)))
            continue
        for t in tails:
            stack.append((t, bal2, path + [name]))

    print("\n%d terminating path(s); %d unbalanced" % (seen_paths, len(worst)))
    for bal, path in worst:
        print("  %+d via %s" % (bal, " -> ".join(path)))


if __name__ == "__main__":
    main()
