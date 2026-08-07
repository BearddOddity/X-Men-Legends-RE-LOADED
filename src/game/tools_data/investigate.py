#!/usr/bin/env python3
"""investigate.py - a four-stage unattended investigation: where, when, what, how.

Why these four stages
---------------------
The user proposed this shape and it is the right one. Every bug chased on this
port has been solved by answering the same four questions in the same order,
and every wrong turn came from answering them out of order - guessing WHAT was
broken before establishing WHEN it broke.

    WHERE  the game stops, named as functions rather than addresses
    WHEN   the order things happen in, especially writes to suspect memory
    WHAT   the actual defect: bad value, wrong owner, or unfaithful code
    HOW    what a fix would have to do, and what the numbers say now

Each stage writes its own section of the report and hands its findings to the
next. Later stages only run if earlier ones found something to work on, so a
clean WHERE stage ends the run early instead of burning hours on nothing.

The build/map rule
------------------
RVAs in a log mean nothing except against the map from the SAME build. On
2026-08-06 an address resolved to sub_001F7930+0x720 in one build and
sub_001F77B0+0x510 in the next, and comparing across the two produced a
"contradiction" that cost an hour. So this tool builds ONCE at the start,
loads that map, and never rebuilds mid-run. If a stage needs new instrumented
code, it says so and stops rather than silently invalidating every address it
has already printed.

Unattended safety
-----------------
- takes the build lock, waits rather than refusing
- hard wall-clock deadline, checked between stages
- read-only apart from the initial build; nothing in gen/ is edited
- report rewritten after every stage, so an interrupted run still leaves work
- plain-English summary first; addresses live further down

Usage (from src/game/):
    py -3 tools_data/investigate.py --hours 4
    py -3 tools_data/investigate.py --hours 4 --watch 0x00F81288,0x00F812B4
    py -3 tools_data/investigate.py --stages where,when --hours 1
    py -3 tools_data/investigate.py --dry-run

Read investigation_report.md afterwards.
"""
import argparse
import bisect
import glob
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import signals                                              # noqa: E402
import deepdive                                             # noqa: E402
import ledger                                               # noqa: E402
from recomp_lock import build_lock, wait_until_quiet        # noqa: E402

REPORT = os.path.join(GAME, "investigation_report.md")
STDERR = os.path.join(GAME, "stderr.txt")
STAGES = ("where", "when", "what", "how")


# ---------------------------------------------------------------- plumbing
def sh(cmd, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=GAME, capture_output=True, text=True, env=e)


def build():
    return sh(["cmd", "/c", os.path.join(GAME, "build_compile.bat")]).returncode == 0


def run(env=None):
    sh(["cmd", "/c", os.path.join(GAME, "run.bat")], env=env)
    try:
        return open(STDERR, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


class Symbols:
    """The map from ONE build. Never mix these with a log from another."""

    def __init__(self):
        maps = glob.glob(os.path.join(GAME, "build", "*.map"))
        self.ok = bool(maps)
        self.syms, self.base = [], 0
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
        self.mtime = os.path.getmtime(maps[0])

    def resolve(self, rva):
        if not self.ok:
            return f"RVA 0x{rva:X}"
        va = self.base + rva
        i = bisect.bisect_right(self.addrs, va) - 1
        if i < 0:
            return f"RVA 0x{rva:X} (before any symbol)"
        return f"{self.syms[i][1]}+0x{va - self.syms[i][0]:X}"


def in_module_rvas(text, pattern, limit=24):
    """RVAs from a labelled block. Anything above 0x8000000 is a system DLL.

    The `{1,3}` matters: between the label and the frames there is usually a
    header line ("Native call stack (resolve against build/*.map):"), and
    allowing only one line silently matched nothing and printed an empty stack.
    """
    m = re.search(pattern + r"(?:.*\n){1,3}?"
                  r"((?:\s+\[\s*\d+\]\s+RVA 0x[0-9A-Fa-f]+\n)+)", text)
    if not m:
        return []
    out = [int(x, 16) for x in re.findall(r"RVA 0x([0-9A-Fa-f]+)", m.group(1))]
    return [r for r in out if r < 0x8000000][:limit]


# Our own diagnostic machinery always sits on top of a crash stack. Reporting
# it as part of the finding buries the game frame that actually matters.
OURS = ("dump_native_stack", "veh_handler", "recomp_where", "print_rip",
        "recomp_icall_", "watch_", "watchdog_")


def game_frames(names):
    return [n for n in names if not n.startswith(OURS)]


def deepdive_frames(where, limit=6):
    """Run deepdive on the functions the WHERE stage just named.

    WHERE answers "which functions are involved" as a bare list of names, and
    the very next question is always "what ARE they?" - six lookups each, by
    hand, against a tree an unattended run may since have rebuilt. Capturing
    it here means the morning report carries the answer instead of the
    homework.

    faithful IS run here, unlike walls.py's copy of this idea, because
    walls.py has already run faithful separately on its wall and this tool has
    not. A dropped branch edge in a frame on the failing path is the most
    actionable thing this stage can surface.

    Frames arrive as "sub_001F7930+0x1FC"; the offset is stripped because the
    function is the stable identity and the offset shifts every build.

    Read-only, takes no build lock, and reads the ledger under ledger's own
    lock - so it must not be called while holding that lock. investigate.py
    holds no ledger lock here.
    """
    seen, out = set(), []
    for n in (where.get("spin_frames") or []) + (where.get("frames") or []):
        name = n.split("+")[0]
        if not name.startswith("sub_") or name in seen:
            continue
        seen.add(name)
        try:
            d = deepdive.gather(name, int(name[4:], 16))
        except Exception as exc:
            print(f"  (deepdive on {name} failed: {exc})")
            continue
        led = d.get("ledger") or []
        f = d.get("faithful") or {}
        out.append({
            "name": name, "file": d.get("file"), "line": d.get("line"),
            "refuted": [e["id"] for e in led if e.get("verdict") == "refuted"],
            "confirmed": [e["id"] for e in led if e.get("verdict") == "confirmed"],
            "open_flags": len(f.get("stale_flags_open",
                                    f.get("stale_flags", []) or [])),
            "missing_labels": len(f.get("missing_labels") or []),
            "fields": d.get("fields"), "globals": d.get("globals"),
            "loops": len(d.get("loops") or []),
            "guards": len(d.get("manual_edits") or []),
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- stages
def stage_where(ctx):
    """How the game ends, and which functions are involved."""
    text = ctx["log"]
    sym = ctx["sym"]
    f = {"ending": "ran to completion", "frames": [], "spin": None,
         "spin_frames": [], "signals": signals.parse(text)}

    if re.search(r"\[WATCHDOG\] No progress", text):
        f["ending"] = "froze (watchdog killed it)"
    elif "Raw stack scan" in text:
        f["ending"] = "crashed"

    m = re.search(r"access\s+:\s*(\S+.*)", text)
    if m:
        f["access"] = m.group(1).strip()

    f["frames"] = game_frames([sym.resolve(r) for r in
                   in_module_rvas(text, r"Call stack, innermost first")])

    # The dominant failing indirect call, and where it is made from.
    probes = re.findall(r"Spin-loop probe: failure #(\d+), VA 0x([0-9A-Fa-f]+)", text)
    if probes:
        n, va = probes[-1]
        f["spin"] = {"count": int(n), "target": int(va, 16)}
        f["spin_frames"] = game_frames([sym.resolve(r) for r in
            in_module_rvas(text, r"Spin-loop probe: failure #" + n)])
    return f


def stage_when(ctx):
    """Chronological write history for each watched address, with values."""
    out = []
    for addr in ctx["watch"]:
        text = run({"RECOMP_WATCH": f"0x{addr:X}:4:w{addr:X}",
                    "RECOMP_WATCH_TRACE": "60"})
        writes = []
        for line in text.splitlines():
            m = re.search(r"\[WATCH:w[0-9A-Fa-f]+\] #(\d+)\s+VA 0x([0-9A-Fa-f]+)"
                          r"\s+\(\+0x([0-9A-Fa-f]+)\) = 0x([0-9A-Fa-f]+)"
                          r"\s+from RVA=0x([0-9A-Fa-f]+)", line)
            if m:
                rva = int(m.group(5), 16)
                writes.append({
                    "n": int(m.group(1)), "va": int(m.group(2), 16),
                    "value": int(m.group(4), 16), "rva": rva,
                    "who": ctx["sym"].resolve(rva) if rva < 0x8000000
                           else "(system DLL - a CRT memset/memcpy)",
                })
        out.append({"addr": addr, "writes": writes})
    return out


HEAP_LO, HEAP_HI = 0x00880000, 0x04000000


def classify(v):
    if v == 0:
        return "zero"
    if HEAP_LO <= v < HEAP_HI:
        return "heap pointer"
    if 0x00010000 <= v < 0x00400000:
        return "code/data address"
    if v < 0x10000:
        return "small number (plausible count)"
    return "large/garbage"


def _ledger_warnings(addr, owners):
    """Prior REFUTED claims touching this address or one of its writers, so a
    stale diagnosis does not get re-presented as new.

    Uses ledger.similar()'s identifier matching (added 2026-08-06 for exactly
    this case) rather than a second implementation of the same idea - the
    function name is the stable identity across investigations, the raw
    address is not, and plain word-overlap alone missed this real case at
    0.27 against the 0.34 threshold before that fix.
    """
    try:
        db = ledger.load()
    except Exception:
        return []
    names = set(owners) | {f"0x{addr:08X}"}
    hits = ledger.similar(db, "", identifiers_=names)
    return [e for _, e in hits if e.get("verdict") == "refuted"]


def stage_what(ctx, when):
    """Turn the write history into a defect statement, or say it is unclear."""
    findings = []
    for entry in when:
        addr, writes = entry["addr"], entry["writes"]
        if not writes:
            findings.append({"addr": addr, "verdict": "nothing wrote here",
                             "detail": "The address is never written during the run."})
            continue

        real = [w for w in writes if "system DLL" not in w["who"]]
        owners = {w["who"].split("+")[0] for w in real}
        kinds = [classify(w["value"]) for w in real]
        last = real[-1] if real else None

        # Distinct code/data addresses landing at offset 0 over time is the
        # signature of one address holding several different C++ objects -
        # a vtable pointer being overwritten by a later, unrelated object.
        vtables = {w["value"] for w in real
                   if classify(w["value"]) == "code/data address"}

        detail = [f"{len(writes)} write(s), {len(owners)} distinct writer(s)."]
        verdict = "unclear"
        if len(vtables) >= 2:
            verdict = "several code/data values stored here (NEEDS CHECKING)"
            detail.append(
                f"{len(vtables)} different code/data values were stored here at "
                "different times. IF this address is an object's base, that word "
                "is its vtable pointer and two values would mean the memory was "
                "reused by a different kind of object. But that only holds at "
                "offset 0 of a real object - at any other field, storing two "
                "different function or table pointers over a run is completely "
                "normal.")
            detail.append(
                "Do not act on this without checking the writers' code. On "
                "2026-08-06 this exact verdict was a false positive: the writers "
                "turned out to be a manager legitimately iterating a list and "
                "updating each element, via MEM32(esi + 8) - a field, not a "
                "base. Confirm what the storing instruction's base register "
                "actually points at before believing any reuse story.")
        elif len(owners) >= 3:
            verdict = "shared memory, many owners"
            detail.append("Several unrelated functions write here, so this is "
                          "not one object's private field.")
        elif last and classify(last["value"]) == "heap pointer":
            verdict = "holds a pointer where a value is expected"
            detail.append(f"Last write stored 0x{last['value']:08X}, a heap "
                          "address. If the reader treats this as a count or a "
                          "flag, it will behave wildly.")

        prior = _ledger_warnings(addr, owners)
        if prior:
            verdict = f"{verdict} [SEE LEDGER - {len(prior)} prior refutation(s)]"
            detail.append(
                "LEDGER: this address already has a REFUTED claim on record. "
                "Read it before trusting the verdict above - " +
                "; ".join(f"#{e['id']} \"{e['claim']}\" ({e['evidence'][:100]})"
                         for e in prior[:3]))

        findings.append({"addr": addr, "verdict": verdict,
                         "detail": " ".join(detail), "owners": sorted(owners),
                         "kinds": kinds})
    return findings


def stage_how(ctx, what):
    """What a fix has to do, plus the current measured numbers."""
    notes = []
    for f in what:
        v = f["verdict"]
        if v.startswith("several code/data values"):
            notes.append(
                f"0x{f['addr']:08X}: FIRST check whether this address is an "
                "object's base or one of its fields. Open each writer listed "
                "above and look at the store: `MEM32(reg)` at an object base is "
                "a vtable write and suggests reuse; `MEM32(reg + 0x8)` is an "
                "ordinary field and suggests nothing at all. Only if it is a "
                "base should you go looking for a release. Getting this backwards "
                "produced a confident wrong diagnosis once already.")
        elif v.startswith("holds a pointer"):
            notes.append(
                f"0x{f['addr']:08X}: the writer stores a pointer where the "
                "reader expects a number. Check the original XBE bytes at the "
                "READER before changing anything - if the lifted code matches "
                "the original, the data is wrong, not the code.")
        elif v.startswith("shared memory"):
            notes.append(
                f"0x{f['addr']:08X}: many owners, so a fixed-address watch "
                "cannot answer much more. Identify the allocation this belongs "
                "to and watch the whole block instead.")
        else:
            notes.append(f"0x{f['addr']:08X}: no clear defect. {f['detail']}")
    return {"notes": notes, "signals": signals.read()}


# ---------------------------------------------------------------- report
def write_report(ctx, res):
    where = res.get("where") or {}
    sig = where.get("signals") or {}
    out = ["# Investigation", "",
           f"Started {time.strftime('%Y-%m-%d %H:%M', time.localtime(ctx['start']))}, "
           f"ran {int((time.time() - ctx['start']) / 60)} minutes.", "",
           "## In short", ""]

    if where:
        out.append(f"- The game **{where['ending']}**.")
        out.append(f"- It got {sig.get('kernel_calls', 0)} steps in.")
        if where.get("spin"):
            sp = where["spin"]
            out.append(f"- It called a bad address {sp['count']:,} times "
                       f"before giving up.")
    for f in res.get("what") or []:
        out.append(f"- **{f['verdict']}** at 0x{f['addr']:08X}.")
    if not res.get("what"):
        out.append("- No memory was watched, so there is no verdict yet.")
    out += ["", "Nothing was changed. This run only looked.", "", "---", ""]

    if where:
        out += ["## Where it stops", "",
                f"Ending: {where['ending']}", ""]
        if where.get("access"):
            out.append(f"Bad memory access: {where['access']}")
            out.append("")
        if where.get("spin"):
            sp = where["spin"]
            out += [f"Repeatedly called `0x{sp['target']:08X}` "
                    f"{sp['count']:,} times. Called from:", ""]
            out += [f"{i}. `{n}`" for i, n in enumerate(where["spin_frames"], 1)]
            out.append("")
        if where.get("frames"):
            out += ["Crash call stack:", ""]
            out += [f"{i}. `{n}`" for i, n in enumerate(where["frames"], 1)]
            out.append("")

        if where.get("deepdive"):
            out += ["### What those functions are", "",
                    "Captured while the tree still matched the log that named "
                    "them. Anything already REFUTED is the first thing to "
                    "read - it is a road someone has already walked.", ""]
            for d in where["deepdive"]:
                out.append(f"**`{d['name']}`** - `{d['file']}:{d['line']}`")
                if d["refuted"] or d["confirmed"]:
                    out.append(f"- ledger: {len(d['refuted'])} refuted "
                               f"{d['refuted'] or ''}, {len(d['confirmed'])} "
                               f"confirmed {d['confirmed'] or ''}")
                if d["missing_labels"] or d["open_flags"]:
                    out.append(f"- **faithful.py: {d['missing_labels']} missing "
                               f"label(s), {d['open_flags']} unrepaired "
                               f"stale-flag site(s) - a REAL port defect, worth "
                               f"more than any bypass**")
                for base, offs in sorted((d.get("fields") or {}).items()):
                    pretty = ", ".join(f"{o}{k}" for o, k in offs[:12])
                    out.append(f"- `{base}` touches {pretty}")
                if d.get("globals"):
                    out.append("- globals: " + ", ".join(
                        f"{g}({k})" for g, k in d["globals"][:8]))
                if d["loops"]:
                    out.append(f"- {d['loops']} backward branch(es)")
                if d["guards"]:
                    out.append(f"- {d['guards']} hand-proven guard(s) already here")
                out.append("")

    for entry in res.get("when") or []:
        out += [f"## When: writes to 0x{entry['addr']:08X}", "",
                "| # | offset | value | kind | written by |",
                "|---|---|---|---|---|"]
        for w in entry["writes"]:
            out.append(f"| {w['n']} | +0x{w['va'] - entry['addr']:X} "
                       f"| `0x{w['value']:08X}` | {classify(w['value'])} "
                       f"| `{w['who']}` |")
        out.append("")

    for f in res.get("what") or []:
        out += [f"## What is wrong at 0x{f['addr']:08X}", "",
                f"**{f['verdict']}**", "", f["detail"], ""]
        if f.get("owners"):
            out += ["Writers:", ""] + [f"- `{o}`" for o in f["owners"]] + [""]

    how = res.get("how")
    if how:
        out += ["## How to fix", ""]
        out += [f"- {n}" for n in how["notes"]]
        out += ["", f"Current numbers: `{signals.fmt(how['signals'])}` "
                    f"(`{signals.header()}`)", ""]

    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


# ---------------------------------------------------------------- driver
def main(argv=None):
    ap = argparse.ArgumentParser(prog="investigate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--watch", default="",
                    help="comma-separated guest addresses to trace in WHEN")
    ap.add_argument("--stages", default=",".join(STAGES))
    ap.add_argument("--wait-hours", type=float, default=2.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    stages = [s.strip() for s in a.stages.split(",") if s.strip()]
    bad = [s for s in stages if s not in STAGES]
    if bad:
        sys.exit(f"unknown stage(s): {', '.join(bad)}; pick from {', '.join(STAGES)}")

    watch = [int(x, 16) for x in a.watch.replace(" ", "").split(",") if x]
    print(f"stages : {' -> '.join(stages)}")
    print(f"watch  : {', '.join(f'0x{w:X}' for w in watch) or '(none)'}")
    print(f"budget : {a.hours}h")
    if a.dry_run:
        print("\ndry run - nothing built or run")
        return 0

    print("\nwaiting for the tree to go quiet...")
    if not wait_until_quiet(timeout=int(a.wait_hours * 3600)):
        sys.exit("something is still building - not starting")

    deadline = time.time() + a.hours * 3600
    ctx = {"start": time.time(), "watch": watch}
    res = {}

    with build_lock("investigate"):
        # ONE build. Every RVA printed below is resolved against this map and
        # would be meaningless against any other.
        print("building once, so every address resolves against one map...")
        if not build():
            sys.exit("build failed - fix that before investigating")
        ctx["sym"] = Symbols()
        if not ctx["sym"].ok:
            sys.exit("no build/*.map - cannot name any address")

        if "where" in stages:
            print("\n[WHERE] running the game...")
            ctx["log"] = run()
            res["where"] = stage_where(ctx)
            print(f"  ending: {res['where']['ending']}")
            # Capture what the named functions ARE while the tree still
            # matches the log that named them.
            res["where"]["deepdive"] = deepdive_frames(res["where"])
            if res["where"]["deepdive"]:
                print(f"  deepdived {len(res['where']['deepdive'])} frame "
                      f"function(s)")
            if res["where"].get("spin"):
                print(f"  spin target 0x{res['where']['spin']['target']:08X} "
                      f"x{res['where']['spin']['count']:,}")
            write_report(ctx, res)

        if "when" in stages and watch and time.time() < deadline:
            print(f"\n[WHEN] tracing {len(watch)} address(es)...")
            res["when"] = stage_when(ctx)
            for e in res["when"]:
                print(f"  0x{e['addr']:08X}: {len(e['writes'])} write(s)")
            write_report(ctx, res)
        elif "when" in stages and not watch:
            print("\n[WHEN] skipped - no --watch addresses given")

        if "what" in stages and res.get("when") and time.time() < deadline:
            print("\n[WHAT] classifying...")
            res["what"] = stage_what(ctx, res["when"])
            for f in res["what"]:
                print(f"  0x{f['addr']:08X}: {f['verdict']}")
            write_report(ctx, res)

        if "how" in stages and res.get("what"):
            print("\n[HOW] what a fix needs to do...")
            res["how"] = stage_how(ctx, res["what"])
            write_report(ctx, res)

    print(f"\nreport: {os.path.basename(REPORT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
