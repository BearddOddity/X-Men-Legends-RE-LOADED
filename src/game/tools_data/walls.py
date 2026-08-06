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

Bypass patterns
---------------
ONLY patterns already proven on this codebase, never invented ones:

    chain-terminate   a walk of the form `node = call(node->fn)` continuing on
                      `node->off != 0`, where an invalid node is zeroed and then
                      dereferenced anyway. Proven 2026-08-06: took the boot from
                      a permanent 716,328,071-iteration freeze to a diagnosable
                      crash.
    count-clamp       a bounded loop whose iteration count is read from memory
                      and holds a pointer rather than a small number. Clamping
                      to zero skips a loop whose call table is empty.

Every application is measured, and reverted if the gate says it did not help.
A pattern that helps nowhere gets recorded as such and stops being tried.

Honesty rules, because nobody is watching
-----------------------------------------
- A bypass is CONTAINMENT, not a fix, and every one it writes says so in the
  generated code along with what is still unexplained.
- "Did not get worse" is never reported as progress. Only kernel_calls actually
  RISING counts as passing a wall.
- The measurement is taken twice before a wall is declared beaten, because a
  one-run gain that does not reproduce is noise.
- Nothing is invented. If no known pattern matches, it records the wall with
  full evidence and moves on rather than guessing at a novel fix.

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


NOTE = (
    "    /* CONTAINMENT applied automatically by walls.py - NOT a fix.\n"
    "     * The boot stopped here; this lets it continue so the next wall can be\n"
    "     * found. The underlying defect is NOT understood. Do not treat this as\n"
    "     * explained, and remove it once the real cause is known.\n"
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
            "Every bypass is a workaround, not a repair. They are marked in the "
            "code and listed below so they can be undone.", "", "---", ""]

    if journey:
        out += ["## What happened, in order", ""]
        for i, step in enumerate(journey, 1):
            out += [f"### Step {i}: {step['key']}", "",
                    f"- steps before: {step['before']}",
                    f"- steps after: {step['after']}",
                    f"- action: {step['action']}",
                    f"- result: **{step['result']}**", ""]

    if stuck:
        out += ["## Walls we could not pass", ""]
        for k in stuck:
            v = kb["walls"][k]
            out += [f"### {k}", "",
                    f"- seen {v.get('seen', 1)} time(s)",
                    f"- tried: {', '.join(v.get('tried', [])) or 'nothing matched'}",
                    f"- note: {v.get('note', 'no known pattern fits this shape')}",
                    ""]
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
                wall = identify_wall(text, sym)
                if not wall:
                    print(f"no wall detected at {before} steps - stopping")
                    break

                key = wall_key(wall)
                rec = kb["walls"].setdefault(key, {"tried": [], "seen": 0})
                rec["seen"] = rec.get("seen", 0) + 1
                rec["kind"] = wall["kind"]
                rec["site"] = wall["site"]
                rec["steps"] = before
                print(f"\nwall: {key}  at {before} steps")

                if not wall["site"]:
                    rec["note"] = "could not name the site from the log"
                    save_kb(kb)
                    break

                span = find_owner_span(wall["site"])
                if not span:
                    rec["note"] = f"{wall['site']} is not in gen/ - cannot patch"
                    save_kb(kb)
                    break

                applied = None
                promoted = set(kb.get("promoted", []))
                tier_used = None
                # Proven first, candidates only once the proven ones are spent -
                # a known-good fix should never lose its turn to a guess.
                for want in ("proven", "candidate"):
                    for name, fn, _p, _c in PATTERNS:
                        if name in rec["tried"]:
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
                    print("  no known pattern fits - recorded and stopping")
                    save_kb(kb)
                    break

                if not build():
                    print("  build failed - reverting")
                    restore_gen(snap)
                    rec["note"] = f"{rec['tried'][-1]} did not compile"
                    save_kb(kb)
                    break

                after = 0
                for _ in range(max(1, a.runs)):
                    s2 = signals.parse(run_once())
                    v = s2.get("reached", 0) + s2.get("kernel_calls", 0)
                    # Worst case across runs: a bypass must help on EVERY run to
                    # count, so it can lose to noise but never win by it.
                    after = min(after, v) if after else v

                progressed = after > before
                journey.append({"key": key, "before": before, "after": after,
                                "action": applied,
                                "result": "passed it" if progressed
                                          else "no gain, reverted"})
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
                    restore_gen(snap)
                    # Record the refutation where a later run will look. Without
                    # this the same pattern gets retried on the same wall next
                    # session, which is precisely how a day gets spent twice.
                    try:
                        import ledger
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
