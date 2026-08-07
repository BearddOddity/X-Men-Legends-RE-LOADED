#!/usr/bin/env python3
"""deepdive.py - everything already known about one function, in one place.

Why this exists
---------------
Deep-diving sub_001F7930 on 2026-08-07 took six separate lookups that all had
to be correlated by hand: the lifted C, faithful.py's verdict, the ledger's
prior claims, the hand-written guards in manual_edits.json, walls.json's
record of it as a wall, and its callers. Every one of those is a different
tool with a different output shape.

Worse, the ledger step is the one most likely to be skipped and the most
expensive to skip. That same session started to re-derive a "free-list links
are overwriting the registry" finding that ledger #16 already had, better and
CONFIRMED. Anything that makes checking the record cheaper than not checking
it pays for itself immediately (project rule #15).

So this answers one question - "what do we already know about this function?"
- and answers it before any new work starts.

What it reports
---------------
    ledger      every entry naming the function, REFUTED ones first and
                loudest. The section that stops repeated work.
    faithful    faithful.py's three checks, run in-process.
    wall        its walls.json record, if it is a known wall.
    guards      hand-proven manual edits already applied to it.
    layout      offsets it touches per base register, with READ vs WRITTEN
                distinguished. A read-only offset is a field it trusts
                someone else to have set - which is precisely the shape of
                the registry+0x2C bug: `ebx 0x2Cr`, read, never written.
    globals     absolute guest addresses touched, for whatis.py.
    icalls      indirect call targets - the callees no name search can find.
    loops       backward branches, where a spin or hang must live.
    probes      probes currently in the body, which make any measurement
                taken right now incomparable with the baseline.
    write-ups   mentions in progress.json and DEBUGGING_NOTES.md, which carry
                the reasoning the ledger's one-line claims compress away.

What it does NOT do, deliberately
---------------------------------
It does not touch Ghidra, and that is a conflict decision, not an omission.

Ghidra holds an exclusive project lock (see "X-men Legends Recomp.lock" beside
the .gpr). A script driving Ghidra headless would either fail against the
running instance or, worse, fight it for the project. The ReVa MCP server is
already the coordinated way in, and it is already available to whoever is
asking. So this prints the exact ReVa call to make and stops there.

Nor does it reimplement anything. faithful.py and ledger.py are imported and
called; manual_edits.json and walls.json are read as data. One source of truth
per fact (rule #4, and the reason the MCP server wraps rather than duplicates).

Conflict safety
---------------
- READ-ONLY. Nothing here writes gen/, seed_list.json or stderr.txt, so it
  takes no build lock - matching the existing convention that only writers
  lock (triage/log/function/search do not either). It is therefore safe to run
  while walls.py or overnight.py is mid-flight, which is exactly when you want
  to ask what is known.
- The ledger IS read under ledger.locked(), briefly. ledger.load() swallows a
  parse error and returns an empty database, so an unlocked read racing a
  writer would silently report "nothing on record" - the single most dangerous
  wrong answer this tool could give. A lock timeout is reported as a timeout,
  never as silence.
- capstone is only imported if faithful.py is actually run (--no-faithful
  skips it), so this still works on an interpreter without it.

Usage (from src/game/):
    py -3 tools_data/deepdive.py sub_001F7930
    py -3 tools_data/deepdive.py 0x001F7930
    py -3 tools_data/deepdive.py sub_001F7930 --no-faithful   # skip capstone
    py -3 tools_data/deepdive.py sub_001F7930 --json
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
GEN = os.path.join(GAME, "src", "recomp", "gen")
sys.path.insert(0, HERE)

import ledger                                                # noqa: E402


def normalise(target):
    """Accept sub_XXXXXXXX, 0xADDR or a bare hex address."""
    t = target.strip()
    if t.lower().startswith("sub_"):
        return t, int(t[4:], 16)
    addr = int(t, 16)
    return "sub_%08X" % addr, addr


def find_in_gen(name):
    """(file, line, source) for a generated function, or None.

    Same span rule as walls.find_owner_span: a function runs until the next
    `void sub_`, which is what the generator emits.
    """
    head = "void %s(void)" % name
    for path in sorted(glob.glob(os.path.join(GEN, "recomp_*.c"))):
        lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
        for i, line in enumerate(lines):
            if line.startswith(head):
                for j in range(i + 1, len(lines)):
                    if lines[j].startswith("void sub_"):
                        return os.path.basename(path), i + 1, lines[i:j]
                return os.path.basename(path), i + 1, lines[i:]
    return None


def callers_of(name):
    """Functions whose body mentions `name`, excluding its own definition.

    Deliberately a text scan rather than a call-graph: the lifter emits direct
    calls, tail calls and address-of references in different shapes, and a
    mention is the honest superset. Indirect calls through a vtable will NOT
    appear - that is a real limit and it is printed, because the wall being
    chased on this project is reached through exactly such a call.
    """
    out = []
    for path in sorted(glob.glob(os.path.join(GEN, "recomp_*.c"))):
        cur = None
        for i, line in enumerate(
                open(path, encoding="utf-8", errors="ignore"), 1):
            if line.startswith("void sub_"):
                m = re.match(r"void (sub_[0-9A-Fa-f]{8})", line)
                cur = m.group(1) if m else None
            elif name in line and cur and cur != name:
                out.append((cur, os.path.basename(path), i, line.strip()))
    return out


def ledger_hits(name, addr):
    """Ledger entries naming this function or its address.

    Uses ledger.similar()'s identifier matching rather than its fuzzy
    word-overlap score. That distinction matters: the fuzzy path scored a real
    matching claim at 0.27 against a 0.34 threshold and reported nothing.
    """
    try:
        with ledger.locked(timeout=10):
            db = ledger.load()
    except TimeoutError:
        return None                       # caller prints this as a timeout
    ids = {name, "0x%08X" % addr}
    return [e for _, e in ledger.similar(db, "", identifiers_=ids)]


def manual_edits_for(name):
    path = os.path.join(HERE, "manual_edits.json")
    try:
        edits = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in edits if e.get("function") == name]


def wall_record(name):
    path = os.path.join(GAME, "walls.json")
    try:
        kb = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for key, v in (kb.get("walls") or {}).items():
        if v.get("site") == name:
            return key, v
    return None


def field_access(body):
    """base register -> {offset: "r" / "w" / "rw"} for every MEM access.

    This is the single most useful thing to know about a function on this
    project and it was being reconstructed by eye every time. Reading
    sub_001F7930 by hand, the fact that mattered was that it touches
    `this+0x28`, `+0x38`, `+0x48`, `+0x58` and `+0x20/+0x24` - i.e. it treats
    ecx as an object with a known layout. Seeing the offsets as a table makes
    the shape obvious in a way that scrolling 188 lines of lifted C does not.

    Reads and writes are distinguished because they answer different
    questions: a written offset is a field this function OWNS, a read-only one
    is a field it merely consumes and therefore trusts someone else to have
    set. The registry bug is exactly a trusted-but-wrong read.
    """
    acc = {}
    w_rx = re.compile(r"MEM(?:8|16|32)\((e[a-z][a-z]) \+ (0x[0-9A-Fa-f]+|-?\d+)\)\s*=")
    r_rx = re.compile(r"MEM(?:8|16|32)\((e[a-z][a-z]) \+ (0x[0-9A-Fa-f]+|-?\d+)\)")
    for line in body:
        s = line.strip()
        if s.startswith(("/*", "*", "//")):
            continue
        written = {(m.group(1), m.group(2)) for m in w_rx.finditer(s)}
        for m in r_rx.finditer(s):
            base, off = m.group(1), m.group(2)
            if base == "esp":
                continue              # stack slots are frame noise, not layout
            kind = "w" if (base, off) in written else "r"
            cur = acc.setdefault(base, {}).get(off, "")
            if kind not in cur:
                acc[base][off] = (cur + kind) or kind
    # Sort offsets numerically so the layout reads like a struct.
    out = {}
    for base, offs in acc.items():
        out[base] = sorted(offs.items(), key=lambda kv: int(kv[0], 0))
    return out


def globals_referenced(body):
    """Absolute guest addresses this function touches, with access kind.

    A hardcoded MEM32(0x5BC508) is an engine global, and on this project those
    are the seams where state goes wrong - unwritten.py exists precisely
    because a global with readers and no writer is a missing initialisation.
    Surfacing them here means the question "what global state does this depend
    on?" is answered without a grep.
    """
    out = {}
    w_rx = re.compile(r"MEM(?:8|16|32)\((0x[0-9A-Fa-f]{5,8})\)\s*=")
    r_rx = re.compile(r"MEM(?:8|16|32)\((0x[0-9A-Fa-f]{5,8})\)")
    for line in body:
        s = line.strip()
        if s.startswith(("/*", "*", "//")):
            continue
        written = {m.group(1) for m in w_rx.finditer(s)}
        for m in r_rx.finditer(s):
            g = m.group(1).upper().replace("0X", "0x")
            kind = "w" if m.group(1) in written else "r"
            cur = out.get(g, "")
            if kind not in cur:
                out[g] = (cur + kind) or kind
    return sorted(out.items())


def indirect_calls(body):
    """Indirect call sites and the expression that supplies the target.

    The complement to callers_of(): that scan finds who calls THIS function
    by name, and is blind to vtable dispatch. This finds who THIS function
    calls indirectly, which on this codebase is where the interesting failures
    live - a virtual call through vtable+0x64, or a table walk like
    `MEM32(eax + esi * 4)`. A target expression involving a register scaled by
    an index is a table dispatch and the count feeding it is worth checking.
    """
    out = []
    rx = re.compile(r"_icall_target = ([^;]+);")
    for i, line in enumerate(body):
        m = rx.search(line)
        if m:
            out.append((i, m.group(1).strip()))
    return out


def back_edges(body):
    """`goto` targets that jump BACKWARDS - i.e. loops.

    walls.py's iteration-cap pattern hunts exactly this shape, and knowing
    where the loops are is the first question when a function spins. A loop
    whose bound comes from memory is the specific hazard this project keeps
    hitting, so the bound expression is worth eyeballing next to it.
    """
    label_at = {}
    for i, line in enumerate(body):
        m = re.match(r"^(loc_[0-9A-Fa-f]{8}):", line.strip())
        if m:
            label_at[m.group(1)] = i
    out = []
    for i, line in enumerate(body):
        for m in re.finditer(r"goto (loc_[0-9A-Fa-f]{8});", line):
            tgt = m.group(1)
            j = label_at.get(tgt)
            if j is not None and j < i:
                out.append((tgt, j, i, line.strip()[:90]))
    return out


def probes_present(body):
    """Probes currently sitting in this function.

    progress.py refuses to record a measurement while probes are in the tree,
    so knowing they are here explains an otherwise baffling refusal - and
    warns that any number taken from this function right now is not
    comparable with the baseline.
    """
    return [(i, l.strip()[:100]) for i, l in enumerate(body)
            if "/* PROBE" in l or "recomp_where(" in l]


def note_mentions(name):
    """Where this function is discussed in the project's own write-ups.

    progress.json entries and DEBUGGING_NOTES.md carry the narrative that the
    ledger's one-line claims compress away - why something was tried, what it
    felt like, what was ruled out in passing. Pointing at them costs nothing
    and the alternative is grepping two files by hand every time.
    """
    hits = []
    p = os.path.join(HERE, "progress.json")
    try:
        for row in json.load(open(p, encoding="utf-8")):
            blob = " ".join(str(row.get(k, "")) for k in ("message", "note"))
            if name in blob:
                hits.append(("progress.json", row.get("date", "?"),
                             (row.get("message") or "")[:110]))
    except (OSError, ValueError):
        pass
    d = os.path.join(GAME, "DEBUGGING_NOTES.md")
    try:
        for i, line in enumerate(open(d, encoding="utf-8", errors="ignore"), 1):
            if name in line:
                hits.append(("DEBUGGING_NOTES.md", "line %d" % i, line.strip()[:110]))
    except OSError:
        pass
    return hits


def faithful_verdict(name):
    """faithful.py's three checks, or a reason it could not run."""
    try:
        from capstone import Cs, CS_ARCH_X86, CS_MODE_32
    except ImportError:
        return {"skipped": "capstone not installed for this interpreter"}
    try:
        import faithful as F
        md = Cs(CS_ARCH_X86, CS_MODE_32)
        d, secs = F.load_xbe()
        gen = F.index_gen()
        if name not in gen:
            return {"skipped": "not present in gen/"}
        addrs = sorted(int(n[4:], 16) for n in gen)
        ends = {a: (addrs[i + 1] if i + 1 < len(addrs) else a + 4096)
                for i, a in enumerate(addrs)}
        return F.check(name, gen, md, d, secs, ends)
    except Exception as exc:
        return {"skipped": "faithful.py failed: %s" % exc}


def gather(name, addr, do_faithful=True):
    loc = find_in_gen(name)
    body = loc[2] if loc else []
    return {
        "name": name,
        "address": "0x%08X" % addr,
        "in_gen": bool(loc),
        "file": loc[0] if loc else None,
        "line": loc[1] if loc else None,
        "lines_of_c": len(body),
        "faithful": faithful_verdict(name) if do_faithful else {"skipped": "--no-faithful"},
        "ledger": ledger_hits(name, addr),
        "manual_edits": manual_edits_for(name),
        "wall": wall_record(name),
        "callers": callers_of(name),
        "fields": field_access(body),
        "globals": globals_referenced(body),
        "indirect_calls": indirect_calls(body),
        "loops": back_edges(body),
        "probes": probes_present(body),
        "notes": note_mentions(name),
    }


def report(d):
    n = d["name"]
    out = []
    out.append("=" * 68)
    out.append("%s  (%s)" % (n, d["address"]))
    out.append("=" * 68)

    if d["in_gen"]:
        out.append("lifted C : %s:%d  (%d lines)"
                   % (d["file"], d["line"], d["lines_of_c"]))
    else:
        out.append("lifted C : NOT in gen/ - an unresolved stub, or never "
                   "discovered. A failed indirect call to a clean .text "
                   "address means a function is MISSING (see CLAUDE.md).")

    # Ledger first, and loudly. This is the section that stops wasted work.
    out.append("")
    if d["ledger"] is None:
        out.append("LEDGER   : could NOT be read - the lock was held for 10s. "
                   "Treat this as UNKNOWN, not as 'nothing on record', and "
                   "retry before starting work.")
    elif not d["ledger"]:
        out.append("LEDGER   : nothing on record for this function.")
    else:
        ref = [e for e in d["ledger"] if e["verdict"] == "refuted"]
        con = [e for e in d["ledger"] if e["verdict"] == "confirmed"]
        inc = [e for e in d["ledger"] if e["verdict"] == "inconclusive"]
        out.append("LEDGER   : %d entr%s - %d refuted, %d confirmed, %d open"
                   % (len(d["ledger"]), "y" if len(d["ledger"]) == 1 else "ies",
                      len(ref), len(con), len(inc)))
        for e in ref:
            out.append("  [REFUTED]      #%d %s" % (e["id"], e["claim"]))
            out.append("                 %s" % e["evidence"][:160])
        for e in con:
            out.append("  [CONFIRMED]    #%d %s" % (e["id"], e["claim"]))
        for e in inc:
            out.append("  [INCONCLUSIVE] #%d %s" % (e["id"], e["claim"]))
        if ref:
            out.append("  ^ READ THE REFUTED ONES before spending time here.")

    f = d["faithful"]
    out.append("")
    if f.get("skipped"):
        out.append("faithful : skipped - %s" % f["skipped"])
    elif f.get("error"):
        out.append("faithful : error - %s" % f["error"])
    else:
        ml = len(f.get("missing_labels", []))
        _open = f.get("stale_flags_open", f.get("stale_flags", []))
        sf = len(_open)
        if ml or sf:
            out.append("faithful : %d missing label(s), %d UNREPAIRED stale-flag site(s) "
                       "- a REAL lifter defect, fix the cause not the symptom"
                       % (ml, sf))
            for at, mn, t in f.get("missing_labels", [])[:6]:
                out.append("             0x%08X %s -> 0x%08X (no loc_ in the C)"
                           % (at, mn, t))
            for at, cmp_, clob in _open[:6]:
                out.append("             0x%08X %s  then  %s" % (at, cmp_, clob))
        else:
            out.append("faithful : clean on all three checks (NOT a proof of "
                       "correctness - it checks dropped edges, stale flags "
                       "and density, nothing else)")
            out.append("             %d original instructions -> %d statements "
                       "(ratio %s)" % (f.get("insns", 0), f.get("stmts", 0),
                                       f.get("ratio", "?")))

    if d["wall"]:
        key, v = d["wall"]
        out.append("")
        out.append("WALL     : this function is a known wall - %s" % key)
        out.append("             kind %s, seen %s time(s), at %s steps"
                   % (v.get("kind"), v.get("seen"), v.get("steps")))
        out.append("             tried: %s"
                   % (", ".join(v.get("tried", [])) or "nothing matched"))
        out.append("             note: %s" % v.get("note", "-"))

    if d["manual_edits"]:
        out.append("")
        out.append("guards   : %d hand-proven manual edit(s) already applied here"
                   % len(d["manual_edits"]))
        for e in d["manual_edits"]:
            out.append("             %s in %s" % (e.get("kind"), e.get("file")))

    if d["probes"]:
        out.append("")
        out.append("PROBES   : %d probe(s) currently IN this function - any "
                   "number measured now is NOT comparable with the baseline, "
                   "and progress.py will refuse to record."
                   % len(d["probes"]))
        for i, txt in d["probes"][:5]:
            out.append("             +%d  %s" % (i, txt))

    if d["fields"]:
        out.append("")
        out.append("layout   : offsets this function touches, by base register")
        out.append("           (w = it writes the field and so owns it; "
                   "r = it only reads, and trusts someone else to have set it)")
        for base in sorted(d["fields"]):
            offs = d["fields"][base]
            pretty = ", ".join("%s%s" % (o, k) for o, k in offs[:14])
            more = "" if len(offs) <= 14 else " ... (+%d)" % (len(offs) - 14)
            out.append("             %-4s %s%s" % (base, pretty, more))

    if d["globals"]:
        out.append("")
        out.append("globals  : %d absolute address(es) touched"
                   % len(d["globals"]))
        for g, kind in d["globals"][:12]:
            out.append("             %s (%s)   whatis.py %s  # classify it"
                       % (g, kind, g))

    if d["indirect_calls"]:
        out.append("")
        out.append("icalls   : %d indirect call site(s) - these are the callees "
                   "a name-based search can never find"
                   % len(d["indirect_calls"]))
        for i, expr in d["indirect_calls"][:10]:
            out.append("             +%-4d target = %s" % (i, expr[:80]))

    if d["loops"]:
        out.append("")
        out.append("loops    : %d backward branch(es) - a spin or hang lives in "
                   "one of these" % len(d["loops"]))
        for tgt, j, i, txt in d["loops"][:8]:
            out.append("             +%d -> +%d (%s)  %s" % (i, j, tgt, txt))

    if d["notes"]:
        out.append("")
        out.append("write-ups: %d mention(s) in the project's own notes"
                   % len(d["notes"]))
        for src, where, txt in d["notes"][:8]:
            out.append("             %s %s: %s" % (src, where, txt))

    out.append("")
    if d["callers"]:
        out.append("callers  : %d direct mention(s) in generated code"
                   % len(d["callers"]))
        for who, f_, ln, txt in d["callers"][:12]:
            out.append("             %s at %s:%d  %s" % (who, f_, ln, txt[:70]))
        if len(d["callers"]) > 12:
            out.append("             ... and %d more" % (len(d["callers"]) - 12))
    else:
        out.append("callers  : none found by text scan.")
    out.append("           NOTE: indirect calls through a vtable are INVISIBLE "
               "to this scan. Absence here is not absence of callers - the "
               "wall on this project is reached through exactly such a call.")

    # The Ghidra half, which this tool deliberately does not do itself.
    out.append("")
    out.append("-" * 68)
    out.append("Ghidra ground truth is NOT fetched here - Ghidra holds an "
               "exclusive project lock and driving it headless would fight "
               "the running instance. Ask ReVa instead:")
    out.append("")
    out.append("  mcp__ReVa__get-decompilation")
    out.append("      programPath          /default.xbe")
    out.append("      functionNameOrAddress %s" % d["address"])
    out.append("")
    out.append("If ReVa answers \"No instruction at address\", Ghidra has not "
               "disassembled it yet - create it first:")
    out.append("")
    out.append("  mcp__ReVa__create-function")
    out.append("      programPath /default.xbe")
    out.append("      address     %s" % d["address"])
    out.append("")
    out.append("Diffing that against the lifted C above is what this project's "
               "notes call the comparison that has never given a wrong answer.")
    out.append("-" * 68)
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="deepdive", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="sub_XXXXXXXX, 0xADDR or bare hex")
    ap.add_argument("--no-faithful", action="store_true",
                    help="skip the faithful.py check (avoids needing capstone)")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    a = ap.parse_args(argv)

    try:
        name, addr = normalise(a.target)
    except ValueError:
        sys.exit("not a function name or address: %s" % a.target)

    d = gather(name, addr, do_faithful=not a.no_faithful)
    if a.json:
        print(json.dumps(d, indent=1, default=str))
    else:
        print(report(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
