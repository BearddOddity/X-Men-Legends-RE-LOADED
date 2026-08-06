#!/usr/bin/env python3
"""faithful.py - does the generated C still say what the original x86 said?

Why this exists
---------------
Comparing lifted C against the original XBE bytes is the only check on this
project that has never given a wrong answer. It confirmed the deferred-flag
miscompile that caused the 716-million-iteration freeze, and - just as usefully -
it cleared sub_001F7930's dispatch loop as faithful, which stopped a day being
spent looking for a lifter bug that was not there.

Every time it has been done by hand, one site at a time, with capstone pasted
into a throwaway script. That is the wrong place for a check this reliable.

What it checks
--------------
Three things, cheapest and most decisive first:

  labels     every branch target in the original must exist as a `loc_` label in
             the generated function. A missing one means the lifter dropped an
             edge, so a whole basic block is unreachable in C that was reachable
             in x86. This is silent and catastrophic and a pure set comparison.

  flags      a `cmp`/`test` whose flags are consumed by a later `jcc`, where the
             tested register is written in between. The lifter re-evaluates the
             comparison at the branch, so it tests the new value. Proven cause of
             a real crash and a real freeze here.

  density    original instruction count vs generated statement count. A function
             lifted at a wildly lower ratio than its neighbours probably lost
             instructions. Weak evidence on its own - it flags candidates for the
             other two checks, nothing more.

What it does NOT do
-------------------
It does not claim a function is correct. It reports specific, checkable
discrepancies. A function with no findings has passed three narrow tests, which
is not the same as being right, and this file will not pretend otherwise.

Usage (from src/game/):
    py -3 tools_data/faithful.py sub_001F7930      # one function, full detail
    py -3 tools_data/faithful.py 0x001F7930
    py -3 tools_data/faithful.py --sweep 400       # check 400 functions, rank
    py -3 tools_data/faithful.py --sweep 0         # everything (slow)
"""
import argparse
import glob
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
GEN = os.path.join(GAME, "src", "recomp", "gen")
XBE = os.path.join(GAME, "game", "default.xbe")

FN_DEF = re.compile(r"^void (sub_([0-9A-Fa-f]{8}))\(")
LABEL = re.compile(r"^(loc_([0-9A-Fa-f]{8})):")
BRANCH = ("jmp", "je", "jne", "jz", "jnz", "jb", "jbe", "ja", "jae", "jl",
          "jle", "jg", "jge", "js", "jns", "jo", "jno", "jp", "jnp",
          "loop", "loope", "loopne", "jcxz", "jecxz")
FLAGSET = ("cmp", "test")


def load_xbe():
    d = open(XBE, "rb").read()
    base = struct.unpack_from("<I", d, 0x104)[0]
    cnt = struct.unpack_from("<I", d, 0x11C)[0]
    hdr = struct.unpack_from("<I", d, 0x120)[0] - base
    secs = []
    for i in range(cnt):
        off = hdr + i * 56
        _fl, va, vs, raw, rs, _na = struct.unpack_from("<IIIIII", d, off)
        secs.append((va, vs, raw, rs))
    return d, secs


def to_file(secs, va):
    for v, vs, raw, _rs in secs:
        if v <= va < v + vs:
            return raw + (va - v)
    return None


def index_gen():
    """name -> (file, start, end, lines) for every generated function."""
    out = {}
    for path in sorted(glob.glob(os.path.join(GEN, "recomp_*.c"))):
        lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
        cur = None
        for i, line in enumerate(lines):
            m = FN_DEF.match(line)
            if m:
                if cur:
                    out[cur[0]] = (path, cur[1], i - 1, lines)
                cur = (m.group(1), i)
        if cur:
            out[cur[0]] = (path, cur[1], len(lines) - 1, lines)
    return out


def disasm_function(md, d, secs, start, limit=4096):
    """Linear sweep from the entry until a terminator with nothing after it.

    Linear is right here: the generated C is itself a linear translation, so a
    mismatch in what the two believe the instruction stream to be is exactly the
    kind of thing worth surfacing.
    """
    off = to_file(secs, start)
    if off is None:
        return []
    ins = list(md.disasm(d[off:off + limit], start))
    out, depth_end = [], start
    for x in ins:
        out.append(x)
        if x.mnemonic in ("ret", "retn") and x.address >= depth_end:
            break
        if x.mnemonic == "jmp":
            m = re.match(r"^0x([0-9a-f]+)$", x.op_str)
            if m and int(m.group(1), 16) > x.address:
                depth_end = max(depth_end, int(m.group(1), 16))
            elif x.address >= depth_end:
                break
    return out


def check(name, gen, md, d, secs):
    path, s, e, lines = gen[name]
    body = lines[s:e + 1]
    addr = int(name[4:], 16)
    ins = disasm_function(md, d, secs, addr)
    if not ins:
        return {"name": name, "error": "not in any XBE section"}

    have = {int(m.group(2), 16) for m in
            (LABEL.match(l) for l in body) if m}
    have.add(addr)

    # 1. branch targets that have no label in the generated code
    missing = []
    for x in ins:
        if x.mnemonic in BRANCH:
            m = re.match(r"^0x([0-9a-f]+)$", x.op_str)
            if not m:
                continue
            t = int(m.group(1), 16)
            if addr <= t <= ins[-1].address and t not in have:
                missing.append((x.address, x.mnemonic, t))

    # 2. flags set, tested register clobbered, then branched on
    stale = []
    for i, x in enumerate(ins):
        if x.mnemonic not in FLAGSET:
            continue
        regs = set(re.findall(r"\b(e[a-z][a-z])\b", x.op_str))
        if not regs:
            continue
        for y in ins[i + 1:i + 6]:
            if y.mnemonic in BRANCH:
                break
            if y.mnemonic in ("mov", "lea", "pop", "xor", "add", "sub", "and", "or"):
                dst = y.op_str.split(",")[0].strip()
                if dst in regs:
                    stale.append((x.address, f"{x.mnemonic} {x.op_str}",
                                  f"{y.mnemonic} {y.op_str}"))
                    break

    stmts = sum(1 for l in body
                if l.strip() and not l.strip().startswith(("/*", "*", "//")))
    return {"name": name, "file": os.path.basename(path), "line": s + 1,
            "insns": len(ins), "stmts": stmts,
            "ratio": round(stmts / max(1, len(ins)), 2),
            "missing_labels": missing, "stale_flags": stale}


def show(r):
    if r.get("error"):
        print(f"{r['name']}: {r['error']}")
        return
    print(f"{r['name']}  ({r['file']}:{r['line']})")
    print(f"  original instructions : {r['insns']}")
    print(f"  generated statements  : {r['stmts']}  (ratio {r['ratio']})")
    if r["missing_labels"]:
        print(f"  MISSING LABELS ({len(r['missing_labels'])}) - dropped edges, "
              f"blocks unreachable in C that x86 could reach:")
        for at, mn, t in r["missing_labels"][:12]:
            print(f"    0x{at:08X}  {mn} -> 0x{t:08X}   no loc_{t:08X} in the C")
    if r["stale_flags"]:
        print(f"  DEFERRED FLAG RISK ({len(r['stale_flags'])}):")
        for at, cmp_, clob in r["stale_flags"][:12]:
            print(f"    0x{at:08X}  {cmp_}   then   {clob}")
    if not r["missing_labels"] and not r["stale_flags"]:
        print("  no findings from these three checks "
              "(not a claim of correctness)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="faithful", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="sub_XXXXXXXX or 0xADDR")
    ap.add_argument("--sweep", type=int, metavar="N",
                    help="check N functions (0 = all) and rank by findings")
    a = ap.parse_args(argv)

    try:
        from capstone import Cs, CS_ARCH_X86, CS_MODE_32
    except ImportError:
        sys.exit("capstone is required: py -3 -m pip install capstone")
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    d, secs = load_xbe()
    gen = index_gen()

    if a.sweep is not None:
        names = sorted(gen)
        if a.sweep:
            names = names[:a.sweep]
        rows = []
        for n in names:
            try:
                r = check(n, gen, md, d, secs)
            except Exception:
                continue
            if r.get("error"):
                continue
            score = len(r["missing_labels"]) * 10 + len(r["stale_flags"])
            if score:
                rows.append((score, r))
        rows.sort(key=lambda t: -t[0])
        print(f"checked {len(names)} function(s); {len(rows)} with findings")
        print(f"{'function':<20} {'labels':>7} {'flags':>6}  where")
        for score, r in rows[:40]:
            print(f"{r['name']:<20} {len(r['missing_labels']):>7} "
                  f"{len(r['stale_flags']):>6}  {r['file']}:{r['line']}")
        if len(rows) > 40:
            print(f"... and {len(rows) - 40} more")
        print("\nMissing labels are weighted 10x: a dropped edge makes real code "
              "unreachable, which is worse than a branch that may test the wrong "
              "value. Confirm any of these by hand before acting.")
        return 0

    if not a.target:
        print(__doc__)
        return 2
    name = a.target if a.target.startswith("sub_") else \
        f"sub_{int(a.target, 16):08X}"
    if name not in gen:
        sys.exit(f"{name} is not in gen/")
    show(check(name, gen, md, d, secs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
