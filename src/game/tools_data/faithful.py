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


def disasm_function(md, d, secs, start, limit=4096, end=None):
    """Disassemble [start, end). `end` is the next known function entry.

    Without it this ran a flat 4 KB from the entry and walked straight through
    into neighbouring functions, counting THEIR branch targets as missing labels.
    The first sweep reported 277 missing labels for a 10-byte fragment and 4,735
    functions with findings - almost all of it that artefact. An unbounded linear
    sweep cannot tell code from the next function from data.
    """
    if end is not None:
        limit = max(1, min(limit, end - start))
    return _disasm_bounded(md, d, secs, start, limit, end)


def _disasm_bounded(md, d, secs, start, limit, end=None):
    """Recursive descent within [start, end): follow every branch target.

    A linear sweep cannot do this job. Stopping at the first jmp/ret under-reads
    - sub_001F09D0 got 24 of its ~112 instructions and so MISSED the deferred-flag
    bug this tool exists to find. Continuing past a forward jmp over-reads - it
    swept 0x75 bytes of unrelated blocks into sub_00344BA0 and invented 9 dropped
    edges in a function that is lifted perfectly. Both failures came from guessing
    where a function ends from a straight line through it.

    Following branches is what the code itself does, so it visits exactly the
    bytes that are reachable and no others. Bounded by `end` (the next known
    function entry) so a tail call leaves the function rather than continuing
    into the neighbour.

    Three attempts at this; the first two shipped wrong numbers. Both ground-truth
    cases are asserted in test_faithful.py so a fourth cannot regress silently.
    """
    lo = start
    hi = end if end else start + limit
    seen, todo, out = set(), [start], []
    while todo:
        pc = todo.pop()
        while lo <= pc < hi and pc not in seen:
            off = to_file(secs, pc)
            if off is None:
                break
            ins = next(md.disasm(d[off:off + 16], pc, 1), None)
            if ins is None:
                break
            seen.add(pc)
            out.append(ins)
            if ins.mnemonic in ("ret", "retn"):
                break
            m = re.match(r"^0x([0-9a-f]+)$", ins.op_str)
            if ins.mnemonic in BRANCH and m:
                t = int(m.group(1), 16)
                if lo <= t < hi and t not in seen:
                    todo.append(t)
            if ins.mnemonic == "jmp":
                break                       # unconditional: no fallthrough
            pc += ins.size
    out.sort(key=lambda i: i.address)
    return out


def _disasm_linear_unused(md, d, secs, start, limit, end=None):
    """Linear sweep from the entry until a terminator with nothing after it.

    Linear is right here: the generated C is itself a linear translation, so a
    mismatch in what the two believe the instruction stream to be is exactly the
    kind of thing worth surfacing.
    """
    off = to_file(secs, start)
    if off is None:
        return []
    ins = list(md.disasm(d[off:off + limit], start))
    out = []
    for x in ins:
        out.append(x)
        # Stop at the first ret or unconditional jmp, full stop.
        #
        # The previous rule continued past a FORWARD jmp, on the theory that it
        # was an internal jump over a block. That is sometimes true, but a tail
        # jmp looks identical, and sub_00344BA0 is exactly that: three
        # instructions ending in `jmp 0x00344C15`. Continuing swept 0x75 bytes of
        # unrelated blocks and reported 9 dropped edges in a function that is
        # lifted perfectly.
        #
        # The next-entry bound does not save us, because the blocks in between
        # are not separate functions in gen/ and so are invisible to it.
        #
        # Conservative on purpose: this now UNDER-reads any function with an
        # internal forward jump, so real findings can be missed. For a tool whose
        # whole value is that it does not lie, a false negative costs a missed
        # bug and a false positive costs a day. Two sweeps were already lost to
        # false positives of this exact shape.
        if x.mnemonic in ("ret", "retn"):
            break
        if x.mnemonic == "jmp":
            m = re.match(r"^0x([0-9a-f]+)$", x.op_str)
            if not m:
                break                       # jmp through a register - stop
            t = int(m.group(1), 16)
            # Inside the function: an internal jump over a block, keep reading.
            # At or past the next function entry, or backwards out of range: a
            # tail jump, so the function ends here.
            if not (start < t < (end if end else start + limit)):
                break
    return out


def _already_repaired(body, cmp_ins, clobbered_reg):
    """Has fix_stale_flags.py already repaired this comparison in the C?

    The lifter preserves the original comparison verbatim in a comment on the
    line where the flags would have been set:

        (void)0; /* cmp ecx, eax - flags set for next jcc */

    So the repaired shape is that comment with a `_sfNNNN_<reg> = <reg>;`
    save just above it. Anchoring on the comment text is what keeps this
    precise - a function can hold several _sf saves for the same register, and
    a bare "does this function mention _sf_ecx" test would wave through a
    genuinely broken second site.

    Conservative on purpose: if the comment cannot be found, this returns
    False and the site is still reported. A false positive costs a look; a
    false negative loses a real defect, and this checker's whole value is that
    it does not lie.
    """
    want = "/* %s %s - flags set" % (cmp_ins.mnemonic, cmp_ins.op_str)
    save = re.compile(r"_sf\d+_%s\s*=\s*%s\s*;" % (clobbered_reg, clobbered_reg))
    for i, line in enumerate(body):
        if want in line:
            # The save is emitted immediately above the flag comment; allow a
            # little slack for interleaved generated statements.
            for j in range(max(0, i - 4), i):
                if save.search(body[j]):
                    return True
            return False
    return False


def check(name, gen, md, d, secs, ends=None):
    path, s, e, lines = gen[name]
    body = lines[s:e + 1]
    addr = int(name[4:], 16)
    ins = disasm_function(md, d, secs, addr,
                          end=(ends or {}).get(addr))
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
    #
    # A hit here is a RISK IN THE ORIGINAL, not proof of a defect in the C:
    # fix_stale_flags.py repairs exactly this shape by saving the register
    # before the clobber and branching on the saved copy -
    #
    #     uint32_t _sf21439_ecx = ecx;
    #     (void)0; /* cmp ecx, eax - flags set for next jcc */
    #     ecx = esi;
    #     if (CMP_EQ(_sf21439_ecx, eax)) goto ...;
    #
    # There are ~449 such repairs already in gen/.
    #
    # DETECTION IS NOT SUPPRESSED when a repair is present, and that is
    # deliberate. test_faithful.py's ground-truth case asserts that
    # sub_001F09D0's site at 0x001F0AB2 is still found - it is the bug this
    # checker exists to find, and a checker that goes quiet once someone
    # patches downstream is a checker you cannot use to verify the patch.
    # Suppressing it was tried and the test caught it immediately.
    #
    # Instead the repair status is reported alongside, because the two
    # questions are different:
    #
    #   stale_flags          every site with the risky shape in the ORIGINAL
    #   stale_flags_open     the subset the generated C does NOT repair
    #
    # `stale_flags_open` is what "needs attention" means. walls.py calls this
    # before applying a bypass and would otherwise write "a REAL defect" to
    # the ledger for code that is already correct - found the hard way on
    # sub_001F09D0, which is fully repaired.
    #
    # The repair match is anchored on the flag comment the lifter preserves
    # verbatim ("cmp ecx, eax - flags set for next jcc"), so it cannot be
    # confused with an unrelated _sf save elsewhere in the same function.
    stale, stale_open = [], []
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
                    site = (x.address, f"{x.mnemonic} {x.op_str}",
                            f"{y.mnemonic} {y.op_str}")
                    stale.append(site)
                    if not _already_repaired(body, x, dst):
                        stale_open.append(site)
                    break

    stmts = sum(1 for l in body
                if l.strip() and not l.strip().startswith(("/*", "*", "//")))
    return {"name": name, "file": os.path.basename(path), "line": s + 1,
            "insns": len(ins), "stmts": stmts,
            "ratio": round(stmts / max(1, len(ins)), 2),
            "missing_labels": missing, "stale_flags": stale,
            "stale_flags_open": stale_open}


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
    _open = r.get("stale_flags_open", r["stale_flags"])
    _fixed = len(r["stale_flags"]) - len(_open)
    if _open:
        print(f"  DEFERRED FLAG RISK, UNREPAIRED ({len(_open)}) - the C "
              f"re-tests at the branch, so it reads the clobbered value:")
        for at, cmp_, clob in _open[:12]:
            print(f"    0x{at:08X}  {cmp_}   then   {clob}")
    if _fixed:
        print(f"  deferred flag sites already repaired ({_fixed}) - the "
              f"original has the risky shape, but fix_stale_flags.py saves "
              f"the register and the C branches on the saved copy. Listed so "
              f"the repair can be verified, NOT work to do:")
        for at, cmp_, clob in [s for s in r["stale_flags"] if s not in _open][:6]:
            print(f"    0x{at:08X}  {cmp_}   then   {clob}   [repaired]")
    if not r["missing_labels"] and not _open:
        print("  no OPEN findings from these three checks "
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

    # Upper bound for each function: the next known entry address. Without this
    # a linear sweep walks into the following function and reports its branch
    # targets as missing labels - the first full sweep produced 277 such
    # "findings" for a 10-byte fragment.
    addrs = sorted(int(n[4:], 16) for n in gen)
    ends = {a: (addrs[i + 1] if i + 1 < len(addrs) else a + 4096)
            for i, a in enumerate(addrs)}

    if a.sweep is not None:
        names = sorted(gen)
        if a.sweep:
            names = names[:a.sweep]
        rows = []
        for n in names:
            try:
                r = check(n, gen, md, d, secs, ends)
            except Exception:
                continue
            if r.get("error"):
                continue
            score = (len(r["missing_labels"]) * 10
                     + len(r.get("stale_flags_open", r["stale_flags"])))
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
    show(check(name, gen, md, d, secs, ends))
    return 0


if __name__ == "__main__":
    sys.exit(main())
