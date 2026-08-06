#!/usr/bin/env python3
"""recon.py - investigate what the port does not know yet, unattended.

Why this exists
---------------
overnight.py grinds a list of candidate fixes that someone already identified.
This answers the earlier question: what should be on that list at all?

The allocator bug found on 2026-08-06 is the template. Nobody guessed it. It
fell out of three mechanical observations that a machine can make far better
than a person reading code at midnight:

    1. sub_001FE670 populates a table, and has ZERO callers anywhere.
    2. It is nonetheless referenced twice - from .rdata, i.e. from vtables.
    3. One of those vtable slots is reached from a branch that never fires.

Every one of those is a query, not an insight. So they become probes, and the
machine runs them over the whole binary instead of the one function a human
happened to be staring at.

Concurrency
-----------
Every probe is READ-ONLY, so this deliberately does NOT take the build lock and
can run beside overnight.py all night. To stay honest about that, gen/*.c is
copied to a scratch directory once at startup and every probe reads the copy -
a build rewriting gen/ underneath a scan would otherwise produce torn reads and
confident nonsense. The XBE itself never changes and is read in place.

Probes
------
    vtables   map every .rdata function pointer to its vtable and slot offset,
              so "what is vtable+0x68?" is a lookup instead of a byte hunt
    orphans   functions with no direct caller AND no vtable slot - genuinely
              unreachable, which usually means a missing edge, not dead code
    ghosts    functions reachable ONLY through a vtable - fine in principle,
              but this is where a never-taken branch hides a whole subsystem

Usage (from src/game/):
    py -3 tools_data/recon.py                     # every probe
    py -3 tools_data/recon.py --probe vtables
    py -3 tools_data/recon.py --slot 0x68         # who sits at vtable+0x68
    py -3 tools_data/recon.py --func sub_001FE670 # everything known about one

Writes recon_report.md and recon_findings.json. The JSON is the machine-usable
form: feed interesting addresses straight into overnight.py or bisect_core.py.
"""
import argparse
import glob
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
GEN = os.path.join(GAME, "src", "recomp", "gen")
XBE = os.path.join(GAME, "game", "default.xbe")
REPORT = os.path.join(GAME, "recon_report.md")
FINDINGS = os.path.join(GAME, "recon_findings.json")

FN_DEF = re.compile(r"^void (sub_([0-9A-Fa-f]{8}))\(")
FN_CALL = re.compile(r"\b(sub_[0-9A-Fa-f]{8})\s*\(\s*\)")


# --------------------------------------------------------------------------
# XBE reading. The file is immutable, so no snapshot needed.
# --------------------------------------------------------------------------
def load_xbe():
    d = open(XBE, "rb").read()
    base = struct.unpack_from("<I", d, 0x104)[0]
    cnt = struct.unpack_from("<I", d, 0x11C)[0]
    hdr = struct.unpack_from("<I", d, 0x120)[0] - base
    secs = []
    for i in range(cnt):
        off = hdr + i * 56
        _fl, va, vs, raw, rs, na = struct.unpack_from("<IIIIII", d, off)
        name = d[na - base:na - base + 16].split(b"\x00")[0].decode("latin1", "replace")
        secs.append({"name": name, "va": va, "vs": vs, "raw": raw, "rs": rs})
    return d, secs


def section(secs, name):
    for s in secs:
        if s["name"] == name:
            return s
    return None


# --------------------------------------------------------------------------
# The gen/ snapshot, so a concurrent build cannot corrupt a scan.
# --------------------------------------------------------------------------
def snapshot_gen():
    tmp = tempfile.mkdtemp(prefix="recon-gen-")
    for p in glob.glob(os.path.join(GEN, "recomp_*.c")):
        shutil.copy2(p, tmp)
    return tmp


def index_functions(gendir):
    """address -> {name, file}, and the set of names called directly."""
    defined, called = {}, set()
    for path in sorted(glob.glob(os.path.join(gendir, "recomp_*.c"))):
        for line in open(path, encoding="utf-8", errors="ignore"):
            m = FN_DEF.match(line)
            if m:
                defined[int(m.group(2), 16)] = {"name": m.group(1),
                                                "file": os.path.basename(path)}
                continue
            for c in FN_CALL.findall(line):
                called.add(c)
    return defined, called


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------
def probe_vtables(d, secs, defined):
    """Every aligned .rdata word that points at a known function.

    Runs of consecutive pointers are treated as one vtable, which is what they
    are; the slot offset is the byte distance from the run's start. That is the
    number the generated code indexes with (MEM32(edx + 0x68)), so it is the
    number worth reporting.
    """
    rd, text = section(secs, ".rdata"), section(secs, ".text")
    if not rd or not text:
        return {"vtables": [], "by_function": {}, "holes": []}
    blob = d[rd["raw"]:rd["raw"] + rd["rs"]]
    tlo, thi = text["va"], text["va"] + text["vs"]

    # A slot is any word pointing into .text - NOT just one we managed to lift.
    # Splitting runs on unlifted pointers was a real bug: it reported
    # sub_001FE670 at slot 0x2C when the true offset is 0x68, because four
    # slots earlier in the same vtable point at functions the recompiler never
    # emitted. Getting the base wrong corrupts every offset in the table.
    ptrs = []
    for off in range(0, len(blob) - 3, 4):
        val = struct.unpack_from("<I", blob, off)[0]
        ptrs.append(val if tlo <= val < thi else None)

    vtables, by_fn, holes, i = [], {}, [], 0
    while i < len(ptrs):
        if ptrs[i] is None:
            i += 1
            continue
        j = i
        while j < len(ptrs) and ptrs[j] is not None:
            j += 1
        if j - i >= 2:                      # a lone pointer is not a vtable
            va = rd["va"] + i * 4
            slots = []
            for k in range(i, j):
                addr, slot = ptrs[k], (k - i) * 4
                info = defined.get(addr)
                slots.append({"slot": slot, "addr": addr,
                              "name": info["name"] if info else None,
                              "lifted": info is not None})
                if info:
                    by_fn.setdefault(info["name"], []).append(
                        {"vtable": va, "slot": slot})
                else:
                    holes.append({"vtable": va, "slot": slot, "addr": addr})
            vtables.append({"va": va, "count": len(slots), "slots": slots})
        i = j
    return {"vtables": vtables, "by_function": by_fn, "holes": holes}


def probe_orphans(defined, called, by_fn):
    """Defined, never called directly, and in no vtable. Should not exist."""
    out = []
    for addr, info in sorted(defined.items()):
        if info["name"] in called or info["name"] in by_fn:
            continue
        out.append({"addr": addr, "name": info["name"], "file": info["file"]})
    return out


def probe_ghosts(defined, called, by_fn):
    """Reachable only through a vtable - where a dead branch hides a subsystem.

    sub_001FE670 was exactly this: zero direct callers, two vtable slots, and
    the one branch that would have reached it never fired. Ranked by how few
    slots hold them, because a function sitting in a single slot is reached by
    exactly one code path and that path is a single point of failure.
    """
    out = []
    for addr, info in sorted(defined.items()):
        name = info["name"]
        if name in called or name not in by_fn:
            continue
        out.append({"addr": addr, "name": name, "file": info["file"],
                    "slots": by_fn[name]})
    out.sort(key=lambda g: len(g["slots"]))
    return out


# --------------------------------------------------------------------------
def write_report(res, elapsed):
    v, orph, ghosts = res["vtables"], res["orphans"], res["ghosts"]
    out = [
        "# Recon",
        "",
        f"- generated: {time.strftime('%Y-%m-%d %H:%M')}  ({elapsed:.1f}s)",
        f"- functions defined: {res['n_functions']}",
        f"- vtables found: {len(v['vtables'])}",
        f"- orphans (no caller, no vtable): {len(orph)}",
        f"- ghosts (vtable-only): {len(ghosts)}",
        "",
        "## Ghosts — reachable only through a vtable",
        "",
        "These run only if some indirect call actually fires. A ghost in a "
        "single slot has exactly one path to it; if that path is a branch that "
        "never fires, the function is dead and nothing says so. This is the "
        "shape of the 2026-08-06 allocator bug.",
        "",
        "| function | slots | file |",
        "|---|---|---|",
    ]
    for g in ghosts[:60]:
        where = ", ".join(f"0x{s['vtable']:08X}+0x{s['slot']:02X}" for s in g["slots"][:3])
        out.append(f"| `{g['name']}` | {len(g['slots'])} — {where} | {g['file']} |")
    if len(ghosts) > 60:
        out.append(f"| ... | {len(ghosts) - 60} more | |")

    out += ["", "## Orphans — no caller and no vtable slot", "",
            "Nothing can reach these. Either the lifter lost an edge, or they "
            "are reached by a computed jump the scan cannot see.", "",
            "| function | file |", "|---|---|"]
    for o in orph[:60]:
        out.append(f"| `{o['name']}` | {o['file']} |")
    if len(orph) > 60:
        out.append(f"| ... | {len(orph) - 60} more | |")

    holes = res.get("holes", [])
    out += ["", "## Vtable holes — slots pointing at functions that were never lifted", "",
            f"{len(holes)} slot(s). Calling one of these lands on the safe stub "
            "and silently returns 0. These are the highest-value seed "
            "candidates on the list: the binary proves the function exists and "
            "proves something calls it.", "",
            "| vtable+slot | target | seed candidate |", "|---|---|---|"]
    for h in holes[:80]:
        out.append(f"| `0x{h['vtable']:08X}+0x{h['slot']:02X}` "
                   f"| `0x{h['addr']:08X}` | `0x{h['addr']:08X}` |")
    if len(holes) > 80:
        out.append(f"| ... | {len(holes) - 80} more | |")

    out += ["", "---", "",
            "`recon_findings.json` holds the full machine-readable results, "
            "including the complete vtable map."]
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="recon", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", choices=["vtables", "orphans", "ghosts", "all"],
                    default="all")
    ap.add_argument("--slot", help="who sits at this vtable offset, e.g. 0x68")
    ap.add_argument("--func", help="everything known about one function")
    a = ap.parse_args(argv)

    t0 = time.time()
    d, secs = load_xbe()
    gendir = snapshot_gen()
    try:
        defined, called = index_functions(gendir)
        vt = probe_vtables(d, secs, defined)
        by_fn = vt["by_function"]

        if a.func:
            name = a.func
            addr = next((k for k, v in defined.items() if v["name"] == name), None)
            print(f"{name}: " + ("defined" if addr else "NOT DEFINED"))
            if addr:
                print(f"  file          : {defined[addr]['file']}")
            print(f"  direct callers: {'yes' if name in called else 'NONE'}")
            slots = by_fn.get(name, [])
            print(f"  vtable slots  : {len(slots)}")
            for s in slots:
                print(f"    0x{s['vtable']:08X} + 0x{s['slot']:02X}")
            if addr and name not in called and slots:
                print("  -> GHOST: only reachable through those slots")
            return 0

        if a.slot:
            want = int(a.slot, 16)
            print(f"functions at vtable+0x{want:02X}:")
            n = 0
            for t in vt["vtables"]:
                for s in t["slots"]:
                    if s["slot"] == want:
                        print(f"  0x{t['va']:08X}+0x{want:02X}  {s['name']}")
                        n += 1
            print(f"{n} slot(s)")
            return 0

        res = {
            "n_functions": len(defined),
            "vtables": vt,
            "holes": vt["holes"],
            "orphans": probe_orphans(defined, called, by_fn),
            "ghosts": probe_ghosts(defined, called, by_fn),
        }
        with open(FINDINGS, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
        write_report(res, time.time() - t0)

        print(f"functions : {res['n_functions']}")
        print(f"vtables   : {len(vt['vtables'])}")
        print(f"orphans   : {len(res['orphans'])}")
        print(f"ghosts    : {len(res['ghosts'])}")
        print(f"\nreport    : {os.path.basename(REPORT)}")
        print(f"findings  : {os.path.basename(FINDINGS)}")
    finally:
        shutil.rmtree(gendir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
