"""
find_missing_functions.py - find real functions the disassembler never found.

The gap
-------
Generation is essentially complete: 26,504 of the 26,505 functions in
tools/disasm/output/functions.json have generated C (the one exception is the
XBE entry point, which main.c handles). So when an indirect call fails to
resolve to a *real* .text address, the failure is in **discovery**, not
translation.

Function discovery works largely from direct call targets. A function whose
address is only ever taken as data - a C++ static-initializer table, a vtable,
a jump table, a callback registered at startup - is never the target of a
`call rel32`, so nothing finds it. Those are exactly the addresses turning up
in the ICALL failure log, and several disassemble as static-initializer thunks:

    0x0034AA86: push 0x34AA40 ; call 0x19F0FF ; mov [0x5D9C1C], eax ; ret

Because RECOMP_ICALL_SAFE returns eax = 0 when it cannot resolve a target,
every such call silently yields NULL - which is where the NULL global objects
behind most of this project's crashes come from.

Method
------
Scan the data sections for 4-byte values that point into .text, then keep only
those that are plausibly *function entries*:

  - not already a known function start
  - not inside any known function's [start, end) - that would make it a jump
    table entry or a mid-function label, not a new function
  - the bytes there decode as a sensible instruction, and the run reaching the
    next `ret` contains no invalid opcodes

Reported in descending confidence. This finds candidates; it does not modify
anything.

Usage (from src/game/):
    py -3 tools_data/find_missing_functions.py
    py -3 tools_data/find_missing_functions.py --json out.json
    py -3 tools_data/find_missing_functions.py --observed   # only the VAs that
                                                            # actually failed in
                                                            # stderr.txt
"""
import argparse
import bisect
import json
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(GAME_DIR))
XBE = os.path.join(GAME_DIR, "game", "default.xbe")
FUNCS_JSON = os.path.join(REPO, "tools", "disasm", "output", "functions.json")

sys.path.insert(0, HERE)
from whatis import load_sections, find_section  # noqa: E402

DATA_SECTIONS = (".rdata", ".data", ".XPP", ".XGRPH")


# ── known-function index ────────────────────────────────────

def load_known():
    fns = json.load(open(FUNCS_JSON, encoding="utf-8"))
    starts, ranges = set(), []
    for f in fns:
        a = int(f["start"], 16)
        starts.add(a)
        ranges.append((a, int(f["end"], 16)))
    ranges.sort()
    return starts, ranges


def inside_known(ranges, keys, va):
    """True if va falls strictly inside some known function's body."""
    i = bisect.bisect_right(keys, va) - 1
    if i < 0:
        return False
    lo, hi = ranges[i]
    return lo < va < hi


# ── code plausibility ───────────────────────────────────────

def decoder():
    try:
        import capstone
    except ImportError:
        return None
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    return md


def looks_like_code(md, data, off, limit=64):
    """Decode forward; a real function reaches a ret/jmp without hitting junk.

    Returns (ok, first_instruction_text, n_instructions).
    """
    if md is None:
        return True, "(capstone unavailable)", 0
    first, n = None, 0
    for ins in md.disasm(data[off:off + 256], 0):
        if first is None:
            first = f"{ins.mnemonic} {ins.op_str}".strip()
        n += 1
        if ins.mnemonic in ("ret", "jmp") and n > 0:
            return True, first, n
        if ins.mnemonic in ("int3",) and n <= 1:
            return False, first, n
        if n >= limit:
            return True, first, n
    return (n > 0), first or "(undecodable)", n


# ── observed failures ───────────────────────────────────────

def observed_failures():
    log = os.path.join(GAME_DIR, "stderr.txt")
    if not os.path.exists(log):
        return set()
    text = open(log, encoding="utf-8", errors="ignore").read()
    return {int(x, 16)
            for x in re.findall(r"Failed to resolve VA 0x([0-9A-Fa-f]+)", text)}


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", help="write candidates to this file")
    ap.add_argument("--observed", action="store_true",
                    help="restrict to VAs that actually failed in stderr.txt")
    args = ap.parse_args(argv)

    data, secs = load_sections()
    starts, ranges = load_known()
    keys = [r[0] for r in ranges]
    md = decoder()

    text = next(s for s in secs if s["name"] == ".text")
    tlo, thi = text["va"], text["va"] + text["vsize"]

    # Every 4-byte value in a data section that points into .text.
    pointed = {}
    for s in secs:
        if s["name"] not in DATA_SECTIONS:
            continue
        raw, n = s["raw"], min(s["rawsize"], s["vsize"])
        for off in range(0, n - 3, 4):
            va = struct.unpack_from("<I", data, raw + off)[0]
            if tlo <= va < thi:
                pointed.setdefault(va, []).append(s["va"] + off)

    want = observed_failures() if args.observed else None

    cands = []
    for va, refs in sorted(pointed.items()):
        if va in starts:
            continue
        if inside_known(ranges, keys, va):
            continue
        if want is not None and va not in want:
            continue
        off = text["raw"] + (va - tlo)
        ok, first, n = looks_like_code(md, data, off)
        if not ok:
            continue
        cands.append({"va": va, "refs": refs[:4], "nrefs": len(refs),
                      "first": first, "insns": n})

    print(f"data-section pointers into .text : {len(pointed)}")
    print(f"already known functions          : "
          f"{sum(1 for v in pointed if v in starts)}")
    print(f"new function candidates          : {len(cands)}")
    if want is not None:
        print(f"(restricted to {len(want)} VAs observed failing in stderr.txt)")
    print()
    for c in cands[:60]:
        refs = ", ".join(f"0x{r:08X}" for r in c["refs"])
        print(f"  0x{c['va']:08X}  {c['insns']:3d} insns  "
              f"first: {c['first']:<32} referenced from {refs}"
              + (f" (+{c['nrefs'] - len(c['refs'])} more)"
                 if c["nrefs"] > len(c["refs"]) else ""))
    if len(cands) > 60:
        print(f"  ... {len(cands) - 60} more")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([{"start": f"0x{c['va']:08X}", "nrefs": c["nrefs"]}
                       for c in cands], f, indent=2)
        print(f"\nwrote {len(cands)} candidates -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
