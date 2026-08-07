#!/usr/bin/env python3
"""walls.py - find the wall the boot stops at, get past it, find the next one.

Why this exists
---------------
investigate.py answers where/when/what/how for ONE bug that a human already
pointed at. This finds the bugs by itself, and keeps going: measure where the
boot stops, identify the construct blocking it, try a bypass that has already
been proven on this codebase, measure again, and if the boot advanced, look for
the next wall. Unattended, all night if asked.

The point is accumulated knowledge, not one answer. Every wall found is written
to walls.json with its signature, what was tried and whether it worked - so the
next run starts from what the last one learned instead of rediscovering it. A
wall already recorded as bypassed is applied immediately; one recorded as
unbeatable is skipped rather than re-attempted for hours.

What counts as a wall
---------------------
Three shapes, all mechanically detectable from a run log:

    spin    one indirect-call target dominates the dispatch count. The address
            it is called from is the site.
    hang    the watchdog fired with no dominant target - a quiet loop making no
            calls. The captured RIP is the site.
    crash   a faulting access. The innermost game frame is the site.

The signature is (kind, site function, target) rather than a raw address, so it
survives rebuilds - addresses shift every build and a knowledge base keyed on
them would be worthless by morning.

Bypass patterns: two tiers, separated by evidence
-------------------------------------------------
    proven      a measurement on THIS codebase showed the boot advance with it.
                Tried first, always - a known-good fix never loses its turn to
                a guess.
    candidate   an invented guess. Applied ONLY inside the measured experiment
                loop, and reverted unless the numbers move.

Inventing candidates is the point, not a compromise: the run forms a hypothesis,
measures it twice, and promotes or refutes it. A candidate that earns a gain
becomes proven, recorded with its numbers. One that fails is written to the
ledger as refuted and never retried on that wall.

What must never happen is a candidate applied AS THOUGH proven. count-clamp is
the cautionary case: clamp a loop count that holds a pointer, since every call in
the loop fails anyway. Airtight-sounding, and it cost 1452 kernel calls. It is
kept in the file with its refutation attached so nobody re-derives it.

Every pattern carries the wall KINDS it is proven or offered for. A hang remedy
on a spin is not a long shot, it is a different bug's fix.

This is a PC PORT, not an emulator (project rule #11)
-----------------------------------------------------
Every bypass this tool writes is SCAFFOLDING, and the distinction matters because
the two kinds of finding have opposite value:

    A bypass (clamp a count, skip a loop, terminate a walk) is an emulator's
    move - paper over behaviour the hardware would have made work. It buys a
    measurement and nothing else. It is debt from the moment it lands, it is
    marked as such in the generated code, and it must be removed. Shipping one
    means shipping a game that limps.

    A missing initialisation, an unlifted function, a dropped branch edge or a
    miscompiled flag test is a PORT defect. Fixing it makes the native binary
    genuinely correct - the CRT __active_heap being unset is the example: four
    readers, no writer, one line to supply the value the console's own startup
    would have supplied.

So the sweeps matter more than the patterns. A pattern gets the boot moving so
the next real defect becomes visible; it is never the answer. If a wall can only
ever be passed by a bypass, that wall is not solved, and walls.json says
"bypassed" rather than "fixed" for exactly that reason.

No NV2A behaviour, no pushbuffer interpretation, no cycle accuracy, and nothing
interpreted or JIT-ed. If a fix starts to look like emulating the console, it is
the wrong fix.

Honesty rules, because nobody is watching
-----------------------------------------
- A bypass is CONTAINMENT, not a fix, and every one it writes says so in the
  generated code along with what is still unexplained.
- "Did not get worse" is never progress. Only `reached` or kernel_calls actually
  RISING counts as passing a wall - and `reached` is the sensitive one, since
  kernel_calls saturates wherever the boot stops.
- Measured twice, worst case kept, so a bypass can lose to noise but never win
  by it.
- Every wall visible in a run is worked, not just the first. Exhausting one costs
  a wall, not the run.
- A wall with nothing left to try is marked exhausted with its evidence, and the
  run moves on rather than ending.

Usage (from src/game/):
    py -3 tools_data/walls.py --hours 4
    py -3 tools_data/walls.py --hours 4 --dry-run
    py -3 tools_data/walls.py --list          # what we know so far

Read walls_report.md and walls.json afterwards.
"""
import argparse
import bisect
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
GEN = os.path.join(GAME, "src", "recomp", "gen")
sys.path.insert(0, HERE)

import signals                                              # noqa: E402
import faithful                                              # noqa: E402
import ledger                                                # noqa: E402
from recomp_lock import build_lock, wait_until_quiet        # noqa: E402

KB = os.path.join(GAME, "walls.json")
REPORT = os.path.join(GAME, "walls_report.md")
STDERR = os.path.join(GAME, "stderr.txt")


# ------------------------------------------------------------------ plumbing
def sh(cmd, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=GAME, capture_output=True, text=True, env=e)


def build():
    r = sh(["cmd", "/c", os.path.join(GAME, "build_compile.bat")])
    return r.returncode == 0


def run_once(env=None):
    sh(["cmd", "/c", os.path.join(GAME, "run.bat")], env=env)
    try:
        return open(STDERR, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


class Symbols:
    """Loaded fresh after every build. RVAs are meaningless across builds."""

    def __init__(self):
        maps = glob.glob(os.path.join(GAME, "build", "*.map"))
        self.ok, self.syms, self.base = bool(maps), [], 0
        if not self.ok:
            return
        rx = re.compile(r"^\s+\d{4}:[0-9A-Fa-f]{8}\s+(\S+)\s+([0-9A-Fa-f]{16})\s")
        for line in open(maps[0], encoding="utf-8", errors="ignore"):
            if not self.base:
                m = re.search(r"Preferred load address is ([0-9A-Fa-f]+)", line)
                if m:
                    self.base = int(m.group(1), 16)
                    continue
            g = rx.match(line)
            if g:
                self.syms.append((int(g.group(2), 16), g.group(1)))
        self.syms.sort()
        self.addrs = [s[0] for s in self.syms]

    def fn(self, rva):
        """Function NAME only - offsets shift between builds, names do not."""
        if not self.ok:
            return None
        i = bisect.bisect_right(self.addrs, self.base + rva) - 1
        return self.syms[i][1] if i >= 0 else None


LABEL = re.compile(r"^(loc_[0-9A-Fa-f]{8}):")

OURS = ("dump_native_stack", "veh_handler", "recomp_where", "print_rip",
        "recomp_icall_", "watch_", "watchdog_", "kernel_thunk", "bridge_")


def game_fn(sym, rvas):
    for r in rvas:
        if r >= 0x8000000:
            continue
        n = sym.fn(r)
        if n and not n.startswith(OURS) and n.startswith("sub_"):
            return n
    return None


def rvas_after(text, pattern):
    m = re.search(pattern + r"(?:.*\n){1,3}?"
                  r"((?:\s+\[\s*\d+\]\s+RVA 0x[0-9A-Fa-f]+\n)+)", text)
    return [int(x, 16) for x in re.findall(r"RVA 0x([0-9A-Fa-f]+)", m.group(1))] \
        if m else []


# ------------------------------------------------------------------ detection
def identify_walls(text, sym):
    """EVERY wall visible in one run, most blocking first.

    A run log usually shows more than one: a dominant spin AND the crash that
    follows it. Returning only the first meant the tool stalled completely the
    moment it ran out of patterns for the top wall, with a fully-diagnosed crash
    sitting untouched in the same log. Now it works down the list, so exhausting
    one wall costs a wall, not the run.
    """
    out = []
    for w in (_wall_spin(text, sym), _wall_hang(text, sym),
              _wall_crash(text, sym)):
        if w:
            out.append(w)
    return out


def _wall_spin(text, sym):
    sig = signals.parse(text)
    probes = re.findall(r"Spin-loop probe: failure #(\d+), VA 0x([0-9A-Fa-f]+)", text)
    if not probes or int(probes[-1][0]) < 100000:
        return None
    n, target = probes[-1]
    return {"kind": "spin",
            "site": game_fn(sym, rvas_after(text, r"Spin-loop probe: failure #" + n)),
            "target": int(target, 16), "count": int(n), "signals": sig}


def _wall_hang(text, sym):
    sig = signals.parse(text)
    if not sig.get("hung"):
        return None
    return {"kind": "hang", "site": game_fn(sym, rvas_after(text, r"hung thread")),
            "target": None, "signals": sig}


def _wall_crash(text, sym):
    sig = signals.parse(text)
    if "Raw stack scan" not in text:
        return None
    m = re.search(r"(?:read|write) at Xbox VA 0x([0-9A-Fa-f]+)", text)
    return {"kind": "crash",
            "site": game_fn(sym, rvas_after(text, r"Call stack, innermost first")),
            "target": int(m.group(1), 16) if m else None, "signals": sig}


def identify_wall(text, sym):
    """Classify how the run ended. Returns None if it ended cleanly."""
    sig = signals.parse(text)

    probes = re.findall(r"Spin-loop probe: failure #(\d+), VA 0x([0-9A-Fa-f]+)", text)
    if probes and int(probes[-1][0]) >= 100000:
        n, target = probes[-1]
        site = game_fn(sym, rvas_after(text, r"Spin-loop probe: failure #" + n))
        return {"kind": "spin", "site": site, "target": int(target, 16),
                "count": int(n), "signals": sig}

    if sig.get("hung"):
        site = game_fn(sym, rvas_after(text, r"hung thread"))
        return {"kind": "hang", "site": site, "target": None, "signals": sig}

    if "Raw stack scan" in text:
        site = game_fn(sym, rvas_after(text, r"Call stack, innermost first"))
        m = re.search(r"read|write at Xbox VA 0x([0-9A-Fa-f]+)", text)
        return {"kind": "crash", "site": site,
                "target": int(m.group(1), 16) if m else None, "signals": sig}
    return None


def wall_key(w):
    """Stable across rebuilds: names and kinds, never offsets."""
    return f"{w['kind']}@{w['site'] or '?'}" + (
        f"->0x{w['target']:08X}" if w.get("target") is not None else "")


# ------------------------------------------------------------------ patterns
def find_owner_span(name):
    """(file, start, end) for a generated function, or None."""
    for path in sorted(glob.glob(os.path.join(GEN, "recomp_*.c"))):
        lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
        for i, line in enumerate(lines):
            if line.startswith(f"void {name}("):
                for j in range(i + 1, len(lines)):
                    if lines[j].startswith("void sub_"):
                        return path, i, j - 1
                return path, i, len(lines) - 1
    return None


def ledger_refuted_for(key):
    """Pattern names the ledger already records as REFUTED on THIS wall.

    walls.json's per-wall `tried` list only covers what this knowledge base
    has seen, and it does not survive the knowledge base being reset or a
    second tool working the same wall. The ledger is the cross-run,
    cross-session record - and consulting it catches a real waste: on
    2026-08-06 iteration-cap was applied to spin@sub_001F7930 and refuted
    FOUR separate times (ledger #9, #10, #20, #21). Each attempt cost a build
    and two runs, and every one of them re-derived a result already written
    down. Rule #15 exists for exactly this.

    Matches the claim wording walls.py itself writes when it refutes:
    "<pattern> gets the boot past <wall key>".

    MUST NOT be called while holding ledger.locked(). The lock is a plain
    lockfile and is NOT reentrant, so a nested acquire would block against
    itself until the 30s stale-reclaim fired.
    """
    try:
        with ledger.locked(timeout=10):
            db = ledger.load()
    except Exception as exc:
        print(f"  (could not read the ledger, not skipping anything: {exc})")
        return set()
    SEP = " gets the boot past "
    out = set()
    for e in db.get("entries", []):
        if e.get("verdict") != "refuted":
            continue
        claim = e.get("claim", "")
        if SEP in claim and claim.split(SEP, 1)[1].strip() == key:
            out.add(claim.split(SEP, 1)[0].strip())
    return out


def still_exhausted(rec, key):
    """True if this wall genuinely has nothing left to try.

    `exhausted` is recorded against the pattern library AS IT WAS at the time,
    so a wall exhausted before a new pattern existed would be skipped forever.
    That is not hypothetical: both live walls were marked exhausted before
    heap-range-guard was mined out of manual_edits.json, so the next run would
    have walked straight past them to the static sweeps and finished in
    minutes - an unattended night spent doing almost nothing.

    So re-open a wall whenever the library has grown to include a pattern it
    has neither already tried nor had refuted against it in the ledger.
    """
    if not rec.get("exhausted"):
        return False
    had = set(rec.get("exhausted_with") or [])
    tried = set(rec.get("tried") or [])
    fresh = {n for n, _f, _p, _c in PATTERNS} - had - tried
    if fresh:
        fresh -= ledger_refuted_for(key)
    if not fresh:
        return True
    print(f"  re-opening {key}: {', '.join(sorted(fresh))} did not exist "
          f"when it was marked exhausted")
    rec["exhausted"] = False
    return False


def deepdive_wall(rec, wall):
    """Attach deepdive's summary of the wall's own function to the record.

    An unattended run leaves a report nobody watched being produced, so the
    morning question is always "what IS this function?" - which previously
    meant running six lookups by hand against a tree that may since have been
    restored. Capturing it at the moment the wall is worked puts the answer in
    walls.json and the report.

    Cached per wall; deepdive is read-only and takes NO build lock, so calling
    it from inside walls.py's own lock is safe. faithful is skipped here
    because faithful_check_once() has already run it and cached the richer
    result - running it twice would just pay the disassembly cost again.
    """
    if "deepdive" in rec:
        return rec["deepdive"]
    name = wall.get("site")
    if not name:
        rec["deepdive"] = None
        return None
    try:
        import deepdive
        d = deepdive.gather(name, int(name[4:], 16), do_faithful=False)
    except Exception as exc:
        print(f"  (deepdive on {name} failed: {exc})")
        rec["deepdive"] = None
        return None
    # Keep only what a morning reader needs; the full dump belongs to the
    # interactive tool, and walls.json should not become a second copy of it.
    rec["deepdive"] = {
        "file": d.get("file"), "line": d.get("line"),
        "lines_of_c": d.get("lines_of_c"),
        "ledger_refuted": [e["id"] for e in (d.get("ledger") or [])
                           if e.get("verdict") == "refuted"],
        "ledger_confirmed": [e["id"] for e in (d.get("ledger") or [])
                             if e.get("verdict") == "confirmed"],
        "fields": d.get("fields"),
        "globals": d.get("globals"),
        "indirect_calls": d.get("indirect_calls"),
        "loops": len(d.get("loops") or []),
        "manual_edits": len(d.get("manual_edits") or []),
        "callers": [c[0] for c in (d.get("callers") or [])][:8],
    }
    return rec["deepdive"]


_FAITHFUL_ENV = None      # lazy: (md, xbe_bytes, secs) - built once, reused


def faithful_check_once(rec, wall):
    """Run faithful.py on a wall's OWN function, once per wall, before any
    bypass is tried on it.

    faithful.py is "the one check on this project that has never given a
    wrong answer" (its own docstring) - it caught the deferred-flag miscompile
    behind a 716M-iteration freeze. Before this, it only ran as a last-resort
    static sweep once every wall was exhausted, so a wall whose real cause was
    a dropped branch edge or a stale-flag bug got a bypass applied to it
    first, with the actual defect found only later, if at all - or missed,
    since the sweep only runs when the boot is fully stuck, not per-wall.

    Cached in `rec["faithful"]` - re-running the full disasm/index setup on
    every pattern-application iteration for the same wall would be wasted
    work against an 1M+-line gen/ tree. `_FAITHFUL_ENV` caches the XBE bytes
    and capstone instance for the run (they never change); `gen`/`ends` are
    rebuilt on first use only, not memoised past that, since gen/ changes as
    patterns are applied to OTHER functions and a stale map could resolve a
    later wall's address to a name that has since moved.
    """
    global _FAITHFUL_ENV
    if "faithful" in rec:
        return rec["faithful"]
    name = wall["site"]
    try:
        if _FAITHFUL_ENV is None:
            from capstone import Cs, CS_ARCH_X86, CS_MODE_32
            md = Cs(CS_ARCH_X86, CS_MODE_32)
            d, secs = faithful.load_xbe()
            _FAITHFUL_ENV = (md, d, secs)
        md, d, secs = _FAITHFUL_ENV
        gen = faithful.index_gen()
        if name not in gen:
            rec["faithful"] = None
            return None
        addrs = sorted(int(n[4:], 16) for n in gen)
        ends = {a: (addrs[i + 1] if i + 1 < len(addrs) else a + 4096)
               for i, a in enumerate(addrs)}
        r = faithful.check(name, gen, md, d, secs, ends)
    except Exception as exc:
        print(f"  (faithful.py check on {name} failed: {exc})")
        rec["faithful"] = None
        return None
    rec["faithful"] = r
    _open = r.get("stale_flags_open", r.get("stale_flags", []))
    # OPEN only. A site fix_stale_flags.py has already repaired is not a
    # defect, and writing one to the ledger as "a REAL defect" would be a
    # confident false alarm - sub_001F09D0 is exactly that case.
    if r.get("missing_labels") or _open:
        print(f"  FAITHFUL.PY finding at {name}: "
              f"{len(r.get('missing_labels', []))} missing label(s), "
              f"{len(_open)} UNREPAIRED stale-flag site(s) - "
              f"a REAL defect, a bypass here treats the symptom, not "
              f"the cause (project rule #11)")
        try:
            with ledger.locked():
                db = ledger.load()
                ledger.seed_from_today(db)
                ledger.add(
                    db,
                    f"{name} is faithfully lifted from the original x86",
                    "refuted",
                    f"faithful.py found {len(r.get('missing_labels', []))} "
                    f"missing label(s) and {len(_open)} "
                    f"unrepaired stale-flag site(s) at {r.get('file')}:{r.get('line')}. "
                    f"Found before any bypass was applied to this wall.",
                    [name, "faithful", "lifter-defect"])
                ledger.save(db)
        except Exception as exc:
            print(f"  (could not write the ledger: {exc})")
    return r


NOTE = (
    "    /* SCAFFOLDING applied automatically by walls.py - NOT a fix, and NOT\n"
    "     * shippable.\n"
    "     *\n"
    "     * This is a PC port, not an emulator (project rule #11). Clamping a\n"
    "     * value or skipping a loop is an emulator's move: it papers over\n"
    "     * behaviour the console would have made work. It buys ONE measurement -\n"
    "     * it lets the boot continue so the next real defect becomes visible.\n"
    "     *\n"
    "     * The underlying defect is NOT understood. A correct port fixes the\n"
    "     * cause - a missing initialisation, an unlifted function, a dropped\n"
    "     * branch edge - so the native binary is genuinely right. Delete this\n"
    "     * once that is done.\n"
    "     *\n"
    "     * Pattern: {pat}\n"
    "     */\n")


def pattern_count_clamp(span, wall):
    """A bounded loop whose count came from memory and holds a pointer.

    Proven at sub_001F7930: count field held 0x00F81180, so the loop ran
    16,257,408 times calling an empty table. Matches `if (CMP_LE(<reg> & <reg>,
    0)) goto <exit>` guarding a backward branch - the lifter's shape for
    `test r,r / jle`.
    """
    path, s, e = span
    lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
    rx = re.compile(r"^(\s*)if \(CMP_LE\((e[a-z][a-z]) & \2, 0\)\) goto (loc_\w+);")
    for i in range(s, e + 1):
        m = rx.match(lines[i])
        if not m:
            continue
        indent, reg = m.group(1), m.group(2)
        if f"{reg} = 0;" in lines[i - 1] or "0x10000u" in lines[i - 1]:
            continue                              # already clamped, or count is 0
        lines.insert(i, NOTE.format(pat="count-clamp") +
                     f"{indent}if ((uint32_t){reg} >= 0x10000u) {{ {reg} = 0; }}")
        open(path, "w", encoding="utf-8").write("\n".join(lines))
        return f"clamped {reg} at {os.path.basename(path)}:{i + 1}"
    return None


def pattern_chain_terminate(span, wall):
    """`node = call(...)` then deref node with a zeroing guard in between.

    Proven at sub_0020E520: the guard set esi = 0 on an invalid pointer and then
    read MEM32(esi + off) anyway, picking up whatever sat at that low address and
    looping forever.
    """
    path, s, e = span
    lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
    rx = re.compile(r"^(\s*)if \(!\((e[a-z][a-z]) >= 0x[0-9A-Fa-f]+u"
                    r".*\)\) \{ \2 = 0; \}$")
    for i in range(s, min(e, len(lines) - 2)):
        m = rx.match(lines[i])
        if not m:
            continue
        nxt = lines[i + 1]
        d = re.match(r"^\s*(e[a-z][a-z]) = MEM32\(" + m.group(2) + r" \+ 0x[0-9A-Fa-f]+\);$", nxt)
        if not d:
            continue
        indent, reg, dst = m.group(1), m.group(2), d.group(1)
        lines[i] = (NOTE.format(pat="chain-terminate")
                    + f"{indent}if (!({reg} >= 0x00880000u && {reg} < 0x04000000u))"
                      f" {{ {reg} = 0; {dst} = 0; }}")
        lines[i + 1] = f"{indent}else{nxt}"
        open(path, "w", encoding="utf-8").write("\n".join(lines))
        return f"terminated chain walk at {os.path.basename(path)}:{i + 1}"
    return None


def pattern_iteration_cap(span, wall):
    """Bound a loop that has a back edge and no step counter.

    Proven at sub_001186A0, and it broke a wall nothing else could see. That is
    `while (eax != 0x3FFFFFFF)` descending a tree by child index, with every
    individual index perfectly in range - so an index bound could not detect it -
    but a child link pointing back at an ancestor descends forever. No kernel
    calls, no indirect dispatches, nothing for any probe to catch. Only the
    watchdog's RIP capture found it. Capping the steps broke the hang and the
    boot moved on.

    Applies to HANG walls specifically: a quiet loop making no calls. It is the
    wrong tool for a spin, where the loop is calling something and the calls are
    what fail.

    The transformation only ever REMOVES iterations, and it exits through the
    loop's own fallthrough path - the same exit the loop takes when its condition
    goes false - so it cannot invent a control-flow path the original lacked.
    The condition is evaluated first so any side effect in it still happens.

    0x40000 is the bound the proven fix used: a pool of at most that many nodes
    cannot legitimately need more steps than it has nodes.
    """
    path, s, e = span
    lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
    labels = {}
    for i in range(s, min(e + 1, len(lines))):
        m = LABEL.match(lines[i])
        if m:
            labels[m.group(1)] = i

    rx = re.compile(r"^(\s*)if \((.+)\) goto (loc_[0-9A-Fa-f]{8});(.*)$")
    for i in range(s, min(e + 1, len(lines))):
        m = rx.match(lines[i])
        if not m:
            continue
        indent, cond, tgt, tail = m.groups()
        ti = labels.get(tgt)
        if ti is None or ti >= i:
            continue                       # forward branch: not a loop back edge
        if "_wcap" in lines[i] or "_wcap" in lines[max(0, i - 1)]:
            continue                       # already capped
        # counter local, declared at the top of the function body
        brace = None
        for j in range(s, min(s + 6, len(lines))):
            if lines[j].strip() == "{":
                brace = j
                break
        if brace is None:
            continue
        lines[i] = (NOTE.format(pat="iteration-cap")
                    + f"{indent}if (({cond}) && ++_wcap <= 0x40000u)"
                      f" goto {tgt};{tail}")
        lines.insert(brace + 1, "    uint32_t _wcap = 0; "
                                "/* walls.py iteration cap */")
        open(path, "w", encoding="utf-8").write("\n".join(lines))
        return f"capped loop back-edge to {tgt} at {os.path.basename(path)}:{i + 1}"
    return None


def pattern_heap_range_guard(span, wall):
    """Zero a pointer loaded from memory before it is used as a deref base.

    Mined from manual_edits.json: 22 of the 139 hand-proven guards on this
    port are a variant of this exact shape - a register loaded from a `MEM32`
    read, then used unguarded as `MEM32(<reg> + off)` a line or two later. In
    every hand-fixed instance the loaded value was sometimes garbage (an
    uninitialised list head, a freed object's stale slot) and dereferencing it
    walked off into unmapped memory. The 22 real fixes disagree on what to do
    once caught - `goto` a specific label, zero a different register, early
    `return` - because the correct recovery is call-site-specific. This
    generalises only the part that is NOT call-site-specific: the value is
    known-bad outside `[0x00880000, 0x04000000)` (the same bound already
    proven in `pattern_chain_terminate`), so zeroing it in place is always a
    legal, conservative move if a caller downstream already treats 0 as
    invalid - which is true throughout this codebase (see chain-terminate's
    own docstring). If that assumption is wrong for a given site, the
    build/measure step below is what catches it, not this docstring.

    Candidate, not proven: the SHAPE is proven 22 times, but this specific
    generalised transform - guard-by-zeroing at every unguarded occurrence -
    has not itself been measured. That is what walls.py's invent/measure loop
    is for.

    Project rule #10 (CLAUDE.md) is a direct warning about this same bound:
    "the heap lower bound 0x00880000 is wrong AND load-bearing - 'fixing' it
    cost 61->59." That was about correcting the constant to something more
    principled; this pattern does not touch the constant, only reuses it
    exactly as already proven. But it means this bound is empirically
    load-bearing for reasons not fully understood, not a clean architectural
    fact - a reason for caution, not for touching it.

    Also rule #11: this is a bypass (SCAFFOLDING per NOTE above), not a port
    fix. If it earns a gain, the real defect - why an invalid pointer reached
    this deref at all - is still open and belongs in the ledger, not closed.
    """
    path, s, e = span
    lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
    load_rx = re.compile(r"^(\s*)(e[a-z][a-z]) = MEM32\([^;]+\);$")
    deref_rx = re.compile(r"MEM32\((e[a-z][a-z]) \+ ")
    guard_rx = re.compile(r">=\s*0x00880000u|!= 0\)|== 0\)")
    for i in range(s, min(e + 1, len(lines) - 1)):
        m = load_rx.match(lines[i])
        if not m:
            continue
        indent, reg = m.groups()
        window = lines[i + 1:min(i + 4, e + 1)]
        target = None
        for off, wl in enumerate(window):
            d = deref_rx.search(wl)
            if d and d.group(1) == reg:
                target = i + 1 + off
                break
            if reg in wl and guard_rx.search(wl):
                target = None
                break                       # already guarded some other way
        if target is None:
            continue
        guard = (f"{indent}if (!({reg} >= 0x00880000u && {reg} < 0x04000000u)) "
                 f"{reg} = 0;")
        lines.insert(target, NOTE.format(pat="heap-range-guard") + guard)
        open(path, "w", encoding="utf-8").write("\n".join(lines))
        return f"guarded {reg} before deref at {os.path.basename(path)}:{target + 1}"
    return None


# count-clamp is DISPROVEN and deliberately not in this list.
#
# It looked airtight: the loop's count field held a heap address, every call in
# the loop targeted 0 and failed, so the loop achieved nothing and skipping it
# should have cost nothing. Measured against an unchanged seed list it took the
# boot from 1452 kernel calls to 56.
#
# "Every call fails" is not "the loop has no effect" - it still advances an
# index, and the failing calls go through the safe stub, which touches esp and
# eax. Something downstream depends on that.
#
# The function is kept so the reasoning and its refutation stay together, and so
# nobody re-derives it from scratch and re-adds it. A pattern goes in this list
# only after a measurement shows kernel_calls RISING, never because the argument
# for it sounds good.
# Two tiers, and the difference is evidence, not confidence.
#
#   proven     a measurement on THIS codebase showed the boot advance with it.
#   candidate  an invented guess. Plausible, unverified, and applied ONLY inside
#              the measured experiment loop, where it is reverted unless the
#              numbers move.
#
# Inventing candidates is the point: the run generates a hypothesis, measures it
# twice, and promotes or refutes it. What must never happen is a candidate being
# applied as though it were proven - that is exactly how count-clamp cost 1452
# kernel calls while sounding airtight.
#
# A candidate that earns a gain is promoted: recorded in walls.json and written
# to the ledger as confirmed WITH the numbers. A candidate that fails is written
# to the ledger as refuted and never retried on that wall.
#
# (name, transform, proven_for, candidate_for)
PATTERNS = [
    ("chain-terminate", pattern_chain_terminate, ("spin", "hang"), ("crash",)),
    # iteration-cap is proven on a quiet hang. Extending it to a spin is a
    # genuine guess: a spin's loop IS calling something, so capping the back edge
    # may skip work the boot needs - which is precisely what happened when the
    # same reasoning was applied by hand as count-clamp. Worth ONE measured
    # attempt rather than an assumption in either direction.
    ("iteration-cap", pattern_iteration_cap, ("hang",), ("spin",)),
    # Shape mined from 22 hand-proven guards in manual_edits.json (see the
    # pattern's own docstring); the generalised transform itself is unproven,
    # so it starts life as a candidate on every wall kind an unguarded stale
    # pointer plausibly explains.
    ("heap-range-guard", pattern_heap_range_guard, (), ("spin", "hang", "crash")),
]


def tiers_for(name, kind, promoted):
    """proven / candidate / skip for this pattern against this wall kind."""
    for n, _fn, proven, cand in PATTERNS:
        if n != name:
            continue
        if kind in proven or f"{n}@{kind}" in promoted:
            return "proven"
        if kind in cand:
            return "candidate"
    return "skip"


# ------------------------------------------------------------------ errors
def harvest_errors(text, sym):
    """Every error signal in a run, ranked, with the guest function to blame.

    A run log is ~15,000 lines and most of it repeats. Re-deriving the ranked
    worklist by hand costs the first half hour of every session, so this does
    it: group by cause, count, name the guest function, and sort by how often
    each fires. What comes out is a list of things to go and look at, in order.

    Nothing here is diagnosed - it is collected. The counts say what is loudest,
    not what is most important, and those are different things.
    """
    import whereis
    funcs, _ = whereis.index()
    spans = {}
    for base, s, e, name, addr in funcs:
        spans.setdefault(base, []).append((s, e, name))

    def owner(fileline):
        base, _, ln = fileline.rpartition(":")
        base = os.path.basename(base)
        try:
            ln = int(ln)
        except ValueError:
            return None
        for s, e, name in spans.get(base, []):
            if s <= ln <= e:
                return name
        return None

    out = {}

    # Rejected indirect calls: target, count, and the generated call site.
    rej = []
    for m in re.finditer(r"^\s+0x([0-9A-Fa-f]{8})\s+x(\d+)\s+from (\S+)",
                         text, re.M):
        rej.append({"target": int(m.group(1), 16), "count": int(m.group(2)),
                    "site": owner(m.group(3)) or m.group(3)})
    rej.sort(key=lambda r: -r["count"])
    out["rejected_calls"] = rej[:25]

    # Unresolvable indirect calls, grouped by target.
    fails = {}
    for m in re.finditer(r"Failed to resolve VA 0x([0-9A-Fa-f]+)", text):
        t = int(m.group(1), 16)
        fails[t] = fails.get(t, 0) + 1
    out["unresolved_calls"] = sorted(
        ({"target": t, "count": c} for t, c in fails.items()),
        key=lambda r: -r["count"])[:25]

    # Empty stub bodies that actually got called - each is a real missing
    # function on the live path, which is stronger evidence than a vtable slot.
    stubs = {}
    for m in re.finditer(r"\[STUB\][^\n]*?(sub_[0-9A-Fa-f]{8})", text):
        stubs[m.group(1)] = stubs.get(m.group(1), 0) + 1
    out["stub_hits"] = sorted(({"fn": k, "count": v} for k, v in stubs.items()),
                              key=lambda r: -r["count"])[:25]

    # Reads of guest low memory. The layout keeps it zero on purpose so null
    # dereferences read null; anything reading there is dereferencing null.
    out["low_memory_reads"] = len(re.findall(r"MEM32\(0x[0-9A-F]{1,3}\)", text))

    # Compiler warnings, which flag lifter problems rather than runtime ones.
    warn = {}
    for log in ("build_full.log", "rebuild_check.log"):
        p = os.path.join(GAME, log)
        if not os.path.exists(p):
            continue
        for m in re.finditer(r"warning (C\d+)",
                             open(p, encoding="utf-8", errors="ignore").read()):
            warn[m.group(1)] = warn.get(m.group(1), 0) + 1
    out["build_warnings"] = warn
    return out


def report_errors(err):
    out = ["## Errors worth looking at", "",
           "Collected, not diagnosed. Counts say what is loudest, which is not "
           "the same as what matters most.", ""]
    if err["rejected_calls"]:
        out += ["### Calls to addresses that are not code", "",
                "| times | target | called from |", "|---|---|---|"]
        out += [f"| {r['count']:,} | `0x{r['target']:08X}` | `{r['site']}` |"
                for r in err["rejected_calls"]]
        out.append("")
    if err["unresolved_calls"]:
        out += ["### Calls to functions we never generated", "",
                "| times | target |", "|---|---|"]
        out += [f"| {r['count']:,} | `0x{r['target']:08X}` |"
                for r in err["unresolved_calls"]]
        out.append("")
    if err["stub_hits"]:
        out += ["### Empty stubs that were actually called", "",
                "These are missing functions ON the live path - better evidence "
                "than a vtable slot, because the game reached them.", "",
                "| times | function |", "|---|---|"]
        out += [f"| {r['count']:,} | `{r['fn']}` |" for r in err["stub_hits"]]
        out.append("")
    if err["low_memory_reads"]:
        out += [f"### Null dereferences", "",
                f"{err['low_memory_reads']} read(s) of guest low memory. That "
                "area is kept zero deliberately, so each one is a null pointer "
                "being followed.", ""]
    if err["build_warnings"]:
        out += ["### Build warnings", ""]
        out += [f"- `{k}` x{v}" for k, v in sorted(err["build_warnings"].items(),
                                                   key=lambda kv: -kv[1])]
        out.append("")
    return out


# ------------------------------------------------------------------ static work
def static_sweeps(deadline):
    """Read-only sweeps that do not need the boot to get anywhere.

    The measure-change-measure loop is useless behind a wall: every reading is
    identical, so there is nothing to steer by, and an unattended run that only
    knows how to do that just stops. Both real defects found on 2026-08-06 came
    from reading code rather than running it - the WIN32 build had no
    CreateWindow at all, and the CRT __active_heap had four readers and no
    writer - and neither needed the wall passed.

    So when the walls are exhausted, keep working statically instead of ending
    the run. Every sweep here is read-only and cannot disturb the tree.
    """
    out = []
    tools = [
        ("globals read but never written", ["unwritten.py", "--min-readers", "3"],
         "Each is a missing initialisation or set by something outside the XBE. "
         "This is how the CRT heap-mode bug was found."),
        ("generated code vs the original bytes", ["faithful.py", "--sweep", "0"],
         "Dropped branch edges make real code unreachable; deferred-flag sites "
         "may test the wrong value."),
        ("functions vtables call that were never lifted", ["recon.py"],
         "The binary proves each exists AND proves something calls it."),
    ]
    for title, argv, why in tools:
        if time.time() > deadline:
            out.append((title, "skipped - out of time", why))
            continue
        print(f"  sweeping: {title}")
        r = sh([sys.executable or "py", os.path.join("tools_data", argv[0])] + argv[1:])
        head = [l for l in (r.stdout or "").splitlines() if l.strip()][:14]
        out.append((title, "\n".join(head) or "(no output)", why))
    return out


# ------------------------------------------------------------------ knowledge
def load_kb():
    try:
        return json.load(open(KB, encoding="utf-8"))
    except (OSError, ValueError):
        return {"walls": {}}


def save_kb(kb):
    with open(KB, "w", encoding="utf-8") as fh:
        json.dump(kb, fh, indent=1)
        fh.write("\n")


def _deepdive_lines(rec):
    """Report lines for a wall's cached deepdive summary, or nothing.

    The point is that the morning reader should not have to re-run six
    lookups against a tree that has since been restored to its snapshot.
    """
    d = rec.get("deepdive")
    if not d:
        return []
    out = ["- **what this function is** (deepdive, captured while the wall "
           "was worked):"]
    if d.get("file"):
        out.append("    - lifted C: `%s:%s` (%s lines)"
                   % (d["file"], d["line"], d["lines_of_c"]))
    if d.get("ledger_refuted") or d.get("ledger_confirmed"):
        out.append("    - ledger: %d refuted, %d confirmed (ids %s / %s)"
                   % (len(d["ledger_refuted"]), len(d["ledger_confirmed"]),
                      d["ledger_refuted"] or "-", d["ledger_confirmed"] or "-"))
    for base, offs in sorted((d.get("fields") or {}).items()):
        pretty = ", ".join("%s%s" % (o, k) for o, k in offs[:12])
        out.append("    - `%s` touches %s" % (base, pretty))
    if d.get("globals"):
        out.append("    - globals: %s"
                   % ", ".join("%s(%s)" % (g, k) for g, k in d["globals"][:8]))
    if d.get("indirect_calls"):
        out.append("    - %d indirect call site(s): %s"
                   % (len(d["indirect_calls"]),
                      "; ".join(e[1][:48] for e in d["indirect_calls"][:4])))
    if d.get("loops"):
        out.append("    - %d backward branch(es) - a spin lives in one of these"
                   % d["loops"])
    if d.get("manual_edits"):
        out.append("    - %d hand-proven guard(s) already applied here"
                   % d["manual_edits"])
    if d.get("callers"):
        out.append("    - callers: %s (vtable calls are invisible to this)"
                   % ", ".join(d["callers"]))
    return out


def _faithful_lines(rec):
    """Report lines for a wall's cached faithful.py result, or nothing."""
    r = rec.get("faithful")
    _open = r.get("stale_flags_open", r.get("stale_flags", [])) if r else []
    if not r or not (r.get("missing_labels") or _open):
        return []
    out = ["- **faithful.py**: "]
    parts = []
    if r.get("missing_labels"):
        parts.append(f"{len(r['missing_labels'])} missing label(s) "
                     f"(dropped branch edge - real, not a guess)")
    if _open:
        parts.append(f"{len(_open)} unrepaired stale-flag site(s)")
    out[-1] += "; ".join(parts) + f" at {r.get('file')}:{r.get('line')}"
    return out


def write_report(kb, journey, start):
    beaten = [k for k, v in kb["walls"].items() if v.get("bypassed")]
    stuck = [k for k, v in kb["walls"].items() if not v.get("bypassed")]
    out = ["# Walls", "",
           f"Ran {int((time.time() - start) / 60)} minutes.", "",
           "## In short", ""]
    if journey:
        first, last = journey[0], journey[-1]
        moved = last["after"] - first["before"]
        out.append(f"**The game went from {first['before']} steps to "
                   f"{last['after']} steps.**" if moved > 0 else
                   f"**No progress.** Still {first['before']} steps.")
    elif stuck:
        # Found a wall but had no pattern for it. That is a real, useful result -
        # the tool declined to guess - and must not read as "nothing happened".
        out.append(f"**Found a wall and stopped.** No known fix fits it, so "
                   f"nothing was changed and nothing was broken.")
        out.append("")
        out.append("This is the tool working correctly. It only applies fixes "
                   "already proven on this game, and refuses to invent one.")
    else:
        out.append("**The game ran without hitting a wall this tool can see.**")
    out += [f"- {len(beaten)} wall(s) got past.",
            f"- {len(stuck)} wall(s) we could not pass yet.", "",
            "Every bypass is SCAFFOLDING, not a repair - this is a PC port, "
            "not an emulator. A clamp or a skip papers over behaviour the "
            "console made work; it buys one measurement and must be deleted. "
            "The static findings below are the ones worth acting on: a missing "
            "initialisation or an unlifted function is a real port defect, and "
            "fixing it makes the native build genuinely correct.",
            "", "---", ""]

    if journey:
        out += ["## What happened, in order", ""]
        for i, step in enumerate(journey, 1):
            out += [f"### Step {i}: {step['key']}", "",
                    f"- steps before: {step['before']}",
                    f"- steps after: {step['after']}",
                    f"- action: {step['action']}",
                    f"- result: **{step['result']}**"]
            if step.get("faithful_defect"):
                out.append(
                    "- **faithful.py found a real defect at this wall's own "
                    "function** - the bypass above got past the SYMPTOM; the "
                    "actual cause is still open. See the ledger entry tagged "
                    "`faithful` for this function.")
            out.append("")

    if stuck:
        out += ["## Walls we could not pass", ""]
        for k in stuck:
            v = kb["walls"][k]
            out += [f"### {k}", "",
                    f"- seen {v.get('seen', 1)} time(s)",
                    f"- tried: {', '.join(v.get('tried', [])) or 'nothing matched'}",
                    f"- note: {v.get('note', 'no known pattern fits this shape')}"]
            out += _faithful_lines(v)
            out += _deepdive_lines(v)
            out.append("")
    # Static findings and the error worklist. report_errors() existed but was
    # never called - an earlier wiring attempt did not match and failed quietly,
    # so the harvested worklist was computed every run and then thrown away.
    if kb.get("sweeps"):
        out += ["## Static findings (no wall needed)", "",
                "The boot is stuck, so measure-and-compare cannot learn anything "
                "new: every reading is identical. These read the code instead of "
                "running it, which is how both real defects so far were found - "
                "the missing host window and the uninitialised CRT heap mode.", ""]
        for title, body, why in kb["sweeps"]:
            out += [f"### {title}", "", why, "", "```", body, "```", ""]
    if kb.get("errors"):
        out += report_errors(kb["errors"])

    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


# ------------------------------------------------------------------ driver
def snapshot_gen():
    tmp = os.path.join(GAME, ".walls-snapshot")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for p in glob.glob(os.path.join(GEN, "recomp_*.c")):
        shutil.copy2(p, tmp)
    return tmp


def restore_gen(tmp):
    for n in os.listdir(tmp):
        shutil.copy2(os.path.join(tmp, n), os.path.join(GEN, n))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="walls", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--runs", type=int, default=2,
                    help="confirming runs before declaring a wall beaten")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--errors-only", action="store_true",
                    help="one run, harvest the error worklist, no patching")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    kb = load_kb()
    if a.list:
        if not kb["walls"]:
            print("nothing recorded yet")
        for k, v in kb["walls"].items():
            state = "PASSED" if v.get("bypassed") else "stuck"
            print(f"  [{state}] {k}")
            print(f"      tried: {', '.join(v.get('tried', [])) or '-'}")
            if v.get("note"):
                print(f"      note : {v['note']}")
        return 0

    if a.errors_only:
        if not build():
            sys.exit("build failed")
        kb["errors"] = harvest_errors(run_once(), Symbols())
        save_kb(kb)
        write_report(kb, [], time.time())
        e = kb["errors"]
        print(f"rejected calls   : {len(e['rejected_calls'])} distinct")
        print(f"unresolved calls : {len(e['unresolved_calls'])} distinct")
        print(f"stubs hit        : {len(e['stub_hits'])}")
        print(f"null derefs      : {e['low_memory_reads']}")
        print("report: " + os.path.basename(REPORT))
        return 0

    if a.dry_run:
        print("dry run: would build, run, identify the wall, try known "
              "patterns, and repeat while the boot advances")
        print(f"known walls: {len(kb['walls'])}")
        return 0

    print("waiting for the tree to go quiet...")
    if not wait_until_quiet(timeout=7200):
        sys.exit("something else is building")

    deadline = time.time() + a.hours * 3600
    start = time.time()
    journey = []

    with build_lock("walls"):
        snap = snapshot_gen()
        print(f"snapshot: {snap}")
        try:
            while time.time() < deadline:
                if not build():
                    print("build failed - reverting last change and stopping")
                    restore_gen(snap)
                    build()
                    break
                sym = Symbols()
                text = run_once()
                sig = signals.parse(text)
                # `reached` (distinct dispatch targets) is the progress measure,
                # not kernel_calls. kernel_calls saturates where the boot stops
                # and read exactly 1452 through every experiment of 2026-08-06,
                # so a wall-walker gated on it could never tell a bypass that
                # helped a little from one that did nothing at all - which is
                # the entire job. Sum both so a gain in either counts.
                before = sig.get("reached", 0) + sig.get("kernel_calls", 0)
                before_r = sig.get("reached", 0)
                if not journey and "errors" not in kb:
                    kb["errors"] = harvest_errors(text, sym)
                    save_kb(kb)
                walls_seen = identify_walls(text, sym)
                if not walls_seen:
                    print(f"no wall detected at {before} steps - stopping")
                    break

                # Take the first wall that still has something untried. Without
                # this the run stalls on the top wall while a fully-diagnosed
                # crash sits unexamined in the same log.
                wall = None
                for w in walls_seen:
                    k = wall_key(w)
                    r = kb["walls"].get(k, {})
                    if still_exhausted(r, k):
                        continue
                    wall = w
                    break
                if wall is None:
                    print(f"all {len(walls_seen)} visible wall(s) exhausted "
                          f"at {before} steps")
                    for w in walls_seen:
                        print(f"    {wall_key(w)}")
                    # Do NOT end the run here. Static sweeps need no runtime
                    # progress, and they found the only two real defects so far.
                    print("")
                    print("switching to static sweeps (read-only)...")
                    kb["sweeps"] = static_sweeps(deadline)
                    save_kb(kb)
                    write_report(kb, journey, start)
                    break
                if len(walls_seen) > 1:
                    print(f"  ({len(walls_seen)} wall(s) visible this run)")

                key = wall_key(wall)
                rec = kb["walls"].setdefault(key, {"tried": [], "seen": 0})
                rec["seen"] = rec.get("seen", 0) + 1
                rec["kind"] = wall["kind"]
                rec["site"] = wall["site"]
                rec["steps"] = before
                print(f"\nwall: {key}  at {before} steps")

                if not wall["site"]:
                    rec["note"] = "could not name the site from the log"
                    rec["exhausted"] = True
                    rec["exhausted_with"] = sorted(
                        p[0] for p in PATTERNS)
                    save_kb(kb)
                    continue

                span = find_owner_span(wall["site"])
                if not span:
                    rec["note"] = f"{wall['site']} is not in gen/ - cannot patch"
                    rec["exhausted"] = True
                    rec["exhausted_with"] = sorted(
                        p[0] for p in PATTERNS)
                    save_kb(kb)
                    continue

                faithful_check_once(rec, wall)
                deepdive_wall(rec, wall)
                save_kb(kb)

                applied = None
                promoted = set(kb.get("promoted", []))
                tier_used = None
                # The ledger outranks this run's own `tried` list: it survives
                # a knowledge-base reset and records what OTHER sessions
                # proved. Without it iteration-cap was re-applied to
                # spin@sub_001F7930 four times across runs, each costing a
                # build and two runs to re-derive a written-down result.
                refuted = ledger_refuted_for(key)
                # Proven first, candidates only once the proven ones are spent -
                # a known-good fix should never lose its turn to a guess.
                for want in ("proven", "candidate"):
                    for name, fn, _p, _c in PATTERNS:
                        if name in rec["tried"]:
                            continue
                        if name in refuted:
                            if want == "proven":
                                print(f"  skipping {name} - the ledger already "
                                      f"records it REFUTED on this wall")
                            continue
                        tier = tiers_for(name, wall["kind"], promoted)
                        if tier != want:
                            if tier == "skip" and want == "proven":
                                print(f"  skipping {name} - not proven or "
                                      f"offered for a {wall['kind']}")
                            continue
                        applied = fn(span, wall)
                        if applied:
                            rec["tried"].append(name)
                            tier_used = tier
                            print(f"  applying {name} [{tier}]: {applied}")
                            break
                    if applied:
                        break
                if not applied:
                    rec["note"] = ("no known pattern matches this shape; "
                                   "needs a human to look")
                    rec["exhausted"] = True
                    rec["exhausted_with"] = sorted(
                        p[0] for p in PATTERNS)
                    print("  no pattern left for this wall - "
                          "recorded, moving to the next")
                    save_kb(kb)
                    continue

                if not build():
                    print("  build failed - reverting")
                    restore_gen(snap)
                    rec["note"] = f"{rec['tried'][-1]} did not compile"
                    if len(rec["tried"]) >= len(PATTERNS):
                        rec["exhausted"] = True
                        rec["exhausted_with"] = sorted(
                            p[0] for p in PATTERNS)
                    save_kb(kb)
                    continue

                after = 0
                for _ in range(max(1, a.runs)):
                    s2 = signals.parse(run_once())
                    v = s2.get("reached", 0) + s2.get("kernel_calls", 0)
                    # Worst case across runs: a bypass must help on EVERY run to
                    # count, so it can lose to noise but never win by it.
                    after = min(after, v) if after else v

                progressed = after > before
                fr = rec.get("faithful") or {}
                journey.append({"key": key, "before": before, "after": after,
                                "action": applied,
                                "result": "passed it" if progressed
                                          else "no gain, reverted",
                                "faithful_defect": bool(
                                    fr.get("missing_labels") or fr.get("stale_flags"))})
                if progressed:
                    rec["bypassed"] = True
                    rec["note"] = f"{applied} took {before} -> {after} steps"
                    print(f"  PASSED: {before} -> {after} steps")
                    snap = snapshot_gen()      # new known-good baseline

                    # A candidate that earned a gain is now proven for this wall
                    # kind. Promotion is recorded so later runs reach for it
                    # first instead of re-deriving that it works, and the ledger
                    # gets the NUMBERS, which is the only thing that makes it
                    # proven rather than merely believed.
                    if tier_used == "candidate":
                        key_pat = f"{rec['tried'][-1]}@{wall['kind']}"
                        kb.setdefault("promoted", [])
                        if key_pat not in kb["promoted"]:
                            kb["promoted"].append(key_pat)
                        print(f"  PROMOTED {key_pat} from candidate to proven")
                        try:
                            import ledger
                            with ledger.locked():
                                db = ledger.load()
                                ledger.seed_from_today(db)
                                ledger.add(
                                    db,
                                    f"{rec['tried'][-1]} gets the boot past a "
                                    f"{wall['kind']} wall",
                                    "confirmed",
                                    f"applied at {wall['site']}: {applied}. "
                                    f"worst-of-{max(1, a.runs)} runs moved "
                                    f"{before} -> {after}.",
                                    [rec["tried"][-1], wall["kind"], "promoted"])
                                ledger.save(db)
                        except Exception as exc:
                            print(f"  (could not write the ledger: {exc})")
                else:
                    print(f"  no gain ({before} -> {after}) - reverting")
                    rec["note"] = f"{rec['tried'][-1]} gave no gain"
                    if len(rec["tried"]) >= len(PATTERNS):
                        rec["exhausted"] = True
                        rec["exhausted_with"] = sorted(
                            p[0] for p in PATTERNS)
                    restore_gen(snap)
                    # Record the refutation where a later run will look. Without
                    # this the same pattern gets retried on the same wall next
                    # session, which is precisely how a day gets spent twice.
                    try:
                        import ledger
                        with ledger.locked():
                            db = ledger.load()
                            ledger.seed_from_today(db)
                            ledger.add(
                                db,
                                f"{rec['tried'][-1]} gets the boot past {key}",
                                "refuted",
                                f"applied at {wall['site']}: {applied}. "
                                f"reached/kernel_calls did not rise "
                                f"({before} -> {after}), reverted.",
                                [rec["tried"][-1], wall["kind"], wall["site"] or "?"])
                            ledger.save(db)
                    except Exception as exc:
                        print(f"  (could not write the ledger: {exc})")
                save_kb(kb)
                write_report(kb, journey, start)
        except BaseException:
            restore_gen(snap)
            build()
            save_kb(kb)
            write_report(kb, journey, start)
            raise
        finally:
            save_kb(kb)
            write_report(kb, journey, start)
            # The snapshot is only a rollback buffer; leaving 30 stale copies of
            # gen/ behind invites someone restoring from a run days old.
            shutil.rmtree(snap, ignore_errors=True)

    print(f"\nreport: {os.path.basename(REPORT)}   knowledge: {os.path.basename(KB)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
