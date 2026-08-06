#!/usr/bin/env python3
"""find_icall_gaps.py - which failed indirect calls are MISSING FUNCTIONS?

Why this exists
---------------
Seeding a genuinely-missing function has been the single most productive move
on this project. Three times in one day:

  * 9 CRT static-init thunks   -> made gen/ reproducible (the 22-call loss)
  * 2 allocator vtable methods -> 54 -> 100 kernel calls, a crash removed
  * 13 static-init guards      -> 1426 -> 1456, another crash removed

Every time, the work was the same manual pipeline: grep stderr.txt for failed
ICALL targets, diff them against seeded_functions.json, run whatis.py on the
survivors, read the disassembly, decide which look like real function entries,
hand-edit seed_list.json, re-seed. That is a lot of steps to repeat, and the
judgement call in the middle is the part worth automating badly - because
getting it WRONG is expensive.

Seeding junk actively hurts. The project log records a round of "the 195
skipped seed addresses are junk" that was wrong in one direction, and separate
occasions where adding fabricated entries made things worse. So this tool
sorts candidates into three buckets and only ever offers the top one.

What separates a real function from garbage
-------------------------------------------
An unresolved ICALL target is one of:

  1. A real function the detector never found, because its address is only
     ever taken as data - a vtable slot or a static-init table. Disassembles
     as a plausible function entry. THESE ARE WORTH SEEDING.
  2. A data word misread as a pointer. Disassembles as nonsense: `into`,
     `pop ss`, `push cs`, a run of 00 bytes decoding as
     `add byte ptr [eax], al`, or wild displacements like [ebp - 0x74b88b0a].
  3. A small integer or a page-aligned round number (0x40000, 0x140000,
     0x382000) - a field value or a size that reached a call site.

The discriminator that has actually worked is the FIRST INSTRUCTION. Real
entries open with a recognisable prologue. In particular the C++ run-once
static-initialiser guard, which is what all 13 of the last batch were:

    mov al, byte ptr [flag] / test al, al / jne skip / push desc /
    mov byte ptr [flag], 1  / call <registration runner>

Usage (from src/game/):
    py -3 tools_data/find_icall_gaps.py                 # report only
    py -3 tools_data/find_icall_gaps.py -f other.txt
    py -3 tools_data/find_icall_gaps.py --add            # append LIKELY to
                                                         # seed_list.json
    py -3 tools_data/find_icall_gaps.py --all            # show UNLIKELY too

After --add, re-seed and rebuild:
    py -3 tools_data/seed_missing_functions.py --from-list seed_list.json --apply
    py -3 tools_data/stub_overridden.py --apply
    py -3 tools_data/manual_edits.py apply

How this sits with the other tools
----------------------------------
NOT a duplicate of find_missing_functions.py. That one scans DATA sections
statically for words that point into .text - it finds functions nobody has
called yet. This one starts from the RUNTIME log and asks which calls actually
failed. Static discovery casts wider; this is evidence-driven and ordered by
what the boot is blocked on. Use both.

It writes seed_list.json, which seed_missing_functions.py both reads
(--from-list) and rewrites (--record, on by default). That used to mean an
address this tool added would be silently deleted again if the seeder skipped
it - and then re-offered on the next sweep, forever. seed_missing_functions.py
now preserves requested-but-skipped addresses in the record for exactly this
reason, so the two do not fight. If you see "keeping N skipped address(es)"
from the seeder, that is this interaction working.

Nothing here touches gen/, so it cannot conflict with manual_edits.py,
add_probe.py or strip_probes.py.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import whatis  # section table, function map and capstone setup already live here

STDERR = os.path.join(GAME_DIR, "stderr.txt")
SEED_LIST = os.path.join(GAME_DIR, "seed_list.json")
FUNC_DB = os.path.join(GAME_DIR, "seeded_functions.json")

FAIL_RE = re.compile(r"Failed to resolve VA 0x([0-9A-Fa-f]{8})")

# First-instruction mnemonics that plausibly OPEN a function. Deliberately
# generous: a false LIKELY costs one wasted seed, while a false UNLIKELY hides
# a real function, and hidden functions have cost this project days.
PROLOGUE = {
    "push", "sub", "mov", "movzx", "movsx", "lea", "xor", "and", "or",
    "cmp", "test", "call", "jmp", "fld", "fild", "fldz", "enter",
}

# Mnemonics that essentially never open a real function here. Every one of
# these was observed as a false candidate in the 2026-08-05 sweep.
JUNK_OPEN = {
    "into", "iret", "iretd", "hlt", "in", "out", "insb", "outsb", "int3",
    "int1", "int", "pop", "popf", "pushf", "sahf", "lahf", "cdq", "cwde",
    "fnstsw", "fnstcw", "loop", "loopne", "loope", "sbb", "adc", "ror",
    "rol", "rcl", "rcr", "xchg", "aaa", "aad", "aam", "aas", "daa", "das",
    "salc", "std", "cld", "sti", "cli", "leave", "ret", "retf", "bound",
    "arpl", "les", "lds", "div", "idiv", "neg", "not",
}

# `add byte ptr [eax], al` is the decoding of 00 00 - a run of zero bytes,
# i.e. padding or a null pointer table, never a function.
ZERO_RUN = "add byte ptr [eax], al"

# A displacement this large in the first instruction means we are decoding
# data, not code.
WILD_DISP = re.compile(r"0x[0-9a-f]{7,}")


def disasm_head(va, data, secs, count=4):
    """First `count` instructions at va, or None if it cannot be read."""
    sec = whatis.find_section(secs, va)
    if not sec or ".text" not in sec["name"]:
        return None
    off = whatis.file_offset(sec, va)
    if off is None:
        return None
    try:
        import capstone
    except ImportError:
        return None
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    out = []
    for ins in md.disasm(data[off:off + 48], va):
        out.append((ins.mnemonic, ins.op_str))
        if len(out) >= count:
            break
    return out or None


def instruction_boundaries(owner, data, secs):
    """Every instruction start inside a known function, or None.

    This is the strongest test in the file. A real call target must sit ON an
    instruction boundary; garbage lands mid-instruction. Decoding from the
    owner's entry point gives the authoritative boundary set, and it caught
    junk the prologue heuristic below happily accepted - 0x00040000 decodes as
    a tidy `sub al, 1` but is three bytes into an instruction of sub_0003FACE.
    """
    start, end, _name, _f = owner
    sec = whatis.find_section(secs, start)
    if not sec:
        return None
    off = whatis.file_offset(sec, start)
    if off is None:
        return None
    try:
        import capstone
    except ImportError:
        return None
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    return {ins.address for ins in md.disasm(data[off:off + (end - start)], start)}


def classify(va, head, owner=None, boundaries=None):
    """(verdict, reason) for one candidate. verdict in LIKELY/UNLIKELY/UNDECODABLE."""
    if not head:
        return "UNDECODABLE", "no decodable instructions at this VA"

    # Inside a known function: the boundary test is decisive, so use it and
    # ignore the prologue heuristics entirely.
    if owner and boundaries is not None:
        start, _end, name, _f = owner
        if va == start:
            return "UNLIKELY", f"already the entry of {name} - not missing"
        if va in boundaries:
            return "LIKELY", (f"instruction boundary +0x{va - start:X} into "
                              f"{name} - a real mid-function target (fragment)")
        return "UNLIKELY", (f"NOT on an instruction boundary of {name} "
                            f"(+0x{va - start:X}) - decoding mid-instruction")

    mnem, ops = head[0]
    first = f"{mnem} {ops}".strip()

    if first == ZERO_RUN:
        return "UNLIKELY", "opens on a run of 00 bytes (padding, not code)"
    if mnem in JUNK_OPEN:
        return "UNLIKELY", f"`{mnem}` does not open a function"
    # `push` is a normal prologue opener, but pushing a SEGMENT register never
    # is - `push cs` / `push ss` are how misaligned data decodes. Caught by the
    # self-check, which is the whole reason it exists.
    if mnem == "push" and ops.strip() in ("cs", "ds", "es", "ss", "fs", "gs"):
        return "UNLIKELY", f"`push {ops.strip()}` - segment push, decoding data"
    if WILD_DISP.search(ops):
        return "UNLIKELY", "wild displacement in the first instruction - decoding data"
    if mnem not in PROLOGUE:
        return "UNLIKELY", f"`{mnem}` is not a recognised prologue opener"

    # The run-once static-initialiser guard: this exact shape accounted for all
    # 13 real functions found in the last sweep, so name it when we see it.
    if len(head) >= 2 and mnem == "mov" and ops.startswith("al,") \
            and head[1][0] == "test":
        return "LIKELY", "run-once static-initialiser guard (mov al,[flag] / test)"

    note = ""
    if va % 0x1000 == 0:
        # Not disqualifying on its own - but page-aligned round numbers were
        # the bulk of the junk in the last sweep, so say so.
        note = "  (NOTE: page-aligned, a common junk shape - check by hand)"
    return "LIKELY", f"plausible prologue `{first}`{note}"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="find_icall_gaps",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--file", default=STDERR,
                    help="log to read (default stderr.txt)")
    ap.add_argument("--add", action="store_true",
                    help="append LIKELY candidates to seed_list.json")
    ap.add_argument("--all", action="store_true",
                    help="also list UNLIKELY / UNDECODABLE candidates")
    a = ap.parse_args(argv)

    if not os.path.exists(a.file):
        sys.exit(f"{a.file} not found - build and run first")

    log = open(a.file, encoding="utf-8", errors="replace").read()
    targets = {int(m, 16) for m in FAIL_RE.findall(log)}
    if not targets:
        print("no failed indirect calls in the log - nothing to do")
        return 0

    db = {int(f["start"], 16) for f in json.load(open(FUNC_DB))}
    data, secs = whatis.load_sections()
    funcs = whatis.load_functions()

    likely, unlikely, undec, known, notext = [], [], [], [], []
    for va in sorted(targets):
        sec = whatis.find_section(secs, va)
        if not sec or ".text" not in sec["name"]:
            notext.append(va)
            continue
        if va in db:
            known.append(va)
            continue
        head = disasm_head(va, data, secs)
        owner = whatis.find_function(funcs, va)
        bounds = instruction_boundaries(owner, data, secs) if owner else None
        verdict, reason = classify(va, head, owner, bounds)
        row = (va, reason, head[0] if head else None, owner)
        {"LIKELY": likely, "UNLIKELY": unlikely,
         "UNDECODABLE": undec}[verdict].append(row)

    print(f"failed indirect calls in log : {len(targets)}")
    print(f"  outside .text (data/small ints/thunks) : {len(notext)}")
    print(f"  already in the function database       : {len(known)}")
    print(f"  in .text and unknown                   : "
          f"{len(likely) + len(unlikely) + len(undec)}")
    print()
    print(f"=== LIKELY missing functions ({len(likely)}) ===")
    if not likely:
        print("  (none)")
    for va, reason, head, owner in likely:
        ins = f"{head[0]} {head[1]}".strip() if head else "?"
        print(f"  0x{va:08X}  {ins[:46]:<46}  {reason}")
        if owner:
            print(f"              ^ falls INSIDE {owner[2]} "
                  f"(0x{owner[0]:08X}-0x{owner[1]:08X}) - the seeder will "
                  f"emit it as a fragment to that function's end")

    if a.all:
        print()
        print(f"=== UNLIKELY ({len(unlikely)}) - not offered for seeding ===")
        for va, reason, head, _o in unlikely:
            ins = f"{head[0]} {head[1]}".strip() if head else "?"
            print(f"  0x{va:08X}  {ins[:46]:<46}  {reason}")
        print()
        print(f"=== UNDECODABLE ({len(undec)}) ===")
        for va, reason, _h, _o in undec:
            print(f"  0x{va:08X}  {reason}")

    if not a.add:
        print()
        print("report only. Re-run with --add to append the LIKELY list to "
              "seed_list.json, then re-seed:")
        print("  py -3 tools_data/seed_missing_functions.py "
              "--from-list seed_list.json --apply")
        return 0

    if not likely:
        print("\nnothing to add")
        return 0

    d = json.load(open(SEED_LIST))
    have = set(d["addresses"])
    new = sorted({va for va, _r, _h, _o in likely} - have)
    if not new:
        print("\nevery LIKELY candidate is already in seed_list.json")
        return 0
    d["addresses"] = sorted(have | set(new))
    d["count"] = len(d["addresses"])
    with open(SEED_LIST, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
        fh.write("\n")
    print(f"\nseed_list.json: {len(have)} -> {len(d['addresses'])} "
          f"(+{len(new)})")
    for va in new:
        print(f"  + 0x{va:08X}")
    print("\nnow: py -3 tools_data/seed_missing_functions.py "
          "--from-list seed_list.json --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
