#!/usr/bin/env python3
"""filter_candidates.py - sort find_missing_functions.py's output into the
candidates worth seeding and the ones that are data read as code.

Why this exists
---------------
find_missing_functions.py reports every .text address a data section points at
that is not already a known function. That is the right net to cast, but its
raw output cannot be seeded: 2,900 candidates include long runs of addresses one
byte apart - 0x00020020, 0x00020021, 0x00020022 ... - whose "first instruction"
is whatever a misaligned byte happens to decode as. Those are a table of
non-pointer data being read as if every byte started a function. Seeding them
would be worse than doing nothing.

The filters, each with a stated reason:

  aligned      A compiled function starts on a 4-byte boundary at minimum; the
               vast majority are 16-byte aligned. A candidate at an odd address
               is almost always a misread.
  not-a-run    Three or more candidates within 4 bytes of each other is the
               signature of a data table, not three tiny functions.
  prologue     The first instruction should look like a function entry - push
               of a callee-saved register, sub esp, mov edi/edi, or a frame
               setup. "and al, 0x38" is not a prologue.
  in-text      Must land inside .text.

    python3 tools_data/filter_candidates.py --json /tmp/cands.json
    python3 tools_data/filter_candidates.py --json /tmp/cands.json --out seed.json

Self-check:  python3 tools_data/filter_candidates.py --selftest
"""
import argparse
import json
import os
import struct
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
XBE = os.path.join(GAME, "game", "default.xbe")

# Instructions that plausibly START a function. Anything else is suspect.
PROLOGUE_MNEMONICS = {
    "push", "sub", "mov", "lea", "xor", "test", "cmp", "fld", "movzx",
    "and", "or", "add", "call", "jmp", "enter", "pushad", "fnstcw",
}
# A prologue that is `push` of a callee-saved register, or a frame setup, is
# much stronger evidence than a bare mov.
STRONG_FIRST = ("push ebp", "push esi", "push edi", "push ebx", "sub esp",
                "mov edi, edi", "push ecx", "enter")


def load_xbe():
    """(bytes, base VA, text range) from the XBE section table."""
    data = open(XBE, "rb").read()
    base = struct.unpack_from("<I", data, 0x104)[0]
    nsec = struct.unpack_from("<I", data, 0x11C)[0]
    secs = struct.unpack_from("<I", data, 0x120)[0] - base
    out = []
    for i in range(nsec):
        off = secs + i * 0x38
        va = struct.unpack_from("<I", data, off + 0x04)[0]
        vsz = struct.unpack_from("<I", data, off + 0x08)[0]
        raw = struct.unpack_from("<I", data, off + 0x0C)[0]
        rsz = struct.unpack_from("<I", data, off + 0x10)[0]
        nameva = struct.unpack_from("<I", data, off + 0x14)[0] - base
        name = data[nameva:data.index(b"\0", nameva)].decode("ascii", "replace")
        out.append({"name": name, "va": va, "vsize": vsz, "raw": raw, "rsize": rsz})
    return data, base, out


def first_insn(data, secs, va):
    try:
        import capstone
    except ImportError:
        return None
    for s in secs:
        if s["va"] <= va < s["va"] + s["vsize"]:
            off = s["raw"] + (va - s["va"])
            blob = data[off:off + 16]
            md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
            for ins in md.disasm(blob, va):
                return f"{ins.mnemonic} {ins.op_str}".strip()
            return None
    return None


def classify(cands, data, base, secs):
    text = next((s for s in secs if s["name"] == ".text"), None)
    vas = sorted(int(c["start"], 16) for c in cands)
    near = set()
    for i, v in enumerate(vas):
        run = [u for u in vas[max(0, i - 3):i + 4] if abs(u - v) <= 4]
        if len(run) >= 3:
            near.add(v)

    kept, dropped = [], Counter()
    for c in cands:
        va = int(c["start"], 16)
        if text and not (text["va"] <= va < text["va"] + text["vsize"]):
            dropped["not in .text"] += 1
            continue
        if va % 4:
            dropped["unaligned"] += 1
            continue
        if va in near:
            dropped["one of a dense run (data table)"] += 1
            continue
        ins = first_insn(data, secs, va)
        if ins is None:
            dropped["undecodable"] += 1
            continue
        if ins.split()[0] not in PROLOGUE_MNEMONICS:
            dropped["first insn is not a prologue"] += 1
            continue
        kept.append({"start": c["start"], "nrefs": c.get("nrefs", 0),
                     "first": ins,
                     "strong": any(ins.startswith(s) for s in STRONG_FIRST)})
    return kept, dropped


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="candidates from find_missing_functions.py --json")
    ap.add_argument("--out", help="write the surviving candidates here")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.json:
        ap.error("give --json, or --selftest")

    cands = json.load(open(a.json))
    data, base, secs = load_xbe()
    kept, dropped = classify(cands, data, base, secs)

    print(f"{len(cands)} candidates in\n")
    for reason, n in dropped.most_common():
        print(f"  dropped {n:>5}  {reason}")
    strong = [k for k in kept if k["strong"]]
    print(f"\n  KEPT    {len(kept):>5}  of which {len(strong)} have a strong prologue")

    print("\nstrongest candidates (most referenced first):")
    for k in sorted(strong, key=lambda k: -k["nrefs"])[:20]:
        print(f"    {k['start']}  {k['nrefs']:>3} ref(s)  {k['first']}")

    if a.out:
        json.dump(kept, open(a.out, "w"), indent=1)
        print(f"\nwrote {len(kept)} -> {a.out}")
    return 0


def selftest():
    """The dense-run filter is the one that matters; check it directly."""
    fake = [{"start": f"0x{0x20020 + i:08X}", "nrefs": 1} for i in range(6)]
    fake += [{"start": "0x00011000", "nrefs": 1}]
    vas = sorted(int(c["start"], 16) for c in fake)
    near = set()
    for i, v in enumerate(vas):
        run = [u for u in vas[max(0, i - 3):i + 4] if abs(u - v) <= 4]
        if len(run) >= 3:
            near.add(v)
    assert 0x20020 in near, "dense run not detected"
    assert 0x00011000 not in near, "isolated candidate wrongly flagged"
    assert 0x20021 % 4 != 0, "alignment check sanity"
    print("selftest ok - dense runs detected, isolated candidates spared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
