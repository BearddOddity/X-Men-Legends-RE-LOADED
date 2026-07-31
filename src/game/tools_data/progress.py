"""
progress.py - record and show how far the boot gets, run over run.

Why
---
"How are we doing?" was being answered from memory, and memory is exactly what
runs out. The kernel-call count also is not self-explanatory: it went
53 -> 48 on the single most valuable change of the project, because that change
altered which path the program takes (see Rule #1). A number without its second
signals and a note is misleading later, when nobody remembers the context.

So each entry records the count, the supporting signals, where it crashed, and
one line on what changed - tied to a commit.

Usage (from src/game/):
    py -3 tools_data/progress.py                       # show the history
    py -3 tools_data/progress.py record -m "what changed"
    py -3 tools_data/progress.py record -m "..." --note "count dipped because X"

`record` reads stderr.txt from the last run, so run the game first. It refuses
to record while probes are still in the tree, since those numbers are not
comparable.
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
LOG = os.path.join(GAME_DIR, "stderr.txt")
DB = os.path.join(HERE, "progress.json")
GEN_GLOB = os.path.join(GAME_DIR, "src", "recomp", "gen", "recomp_*.c")


def probes_present():
    for p in glob.glob(GEN_GLOB):
        with open(p, encoding="utf-8", errors="ignore") as f:
            if "/* PROBE */" in f.read():
                return os.path.basename(p)
    return None


def measure():
    """Pull the metrics that matter out of the last run's log."""
    if not os.path.exists(LOG):
        raise SystemExit(f"{LOG} not found - run the game first")
    text = open(LOG, encoding="utf-8", errors="ignore").read()

    sys.path.insert(0, HERE)
    import triage_crash as tc
    crash = tc.parse_crash(LOG)

    where = None
    if crash:
        if crash.get("foreign_module"):
            where = os.path.basename(crash["foreign_module"])
        elif crash.get("rva") is not None:
            sym = tc.symbolise(tc.load_map(), crash["rva"])
            if sym:
                where = f"{sym['name']}+0x{sym['offset']:X}"

    return {
        "kernel_calls": len(re.findall(r"\[KERNEL\] #", text)),
        "failed_icalls": len(re.findall(r"Failed to resolve VA", text)),
        "heap_allocs": len(re.findall(r"\[HEAP\] #", text)),
        "crash_in": where,
        "fault_va": (f"0x{crash['fault_va']:08X}"
                     if crash and crash.get("fault_va") is not None else None),
    }


def git(*args):
    try:
        return subprocess.run(["git"] + list(args), cwd=GAME_DIR,
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""


def load():
    if not os.path.exists(DB):
        return []
    return json.load(open(DB, encoding="utf-8"))


def show(rows):
    if not rows:
        print("no entries yet - run: progress.py record -m \"...\"")
        return
    print(f"{'date':<11} {'kern':>5} {'delta':>6} {'icall':>6}  "
          f"{'commit':<8} what")
    print("-" * 96)
    prev = None
    for r in rows:
        k = r["kernel_calls"]
        delta = "" if prev is None else f"{k - prev:+d}"
        # Early entries predate the failed-indirect-call metric; show "-"
        # rather than inventing a zero.
        ic = "-" if r.get("failed_icalls") is None else str(r["failed_icalls"])
        print(f"{r['date']:<11} {k:>5} {delta:>6} {ic:>6}  "
              f"{r.get('commit', ''):<8} {r['message']}")
        if r.get("note"):
            print(f"{'':<32}  ^ {r['note']}")
        if r.get("crash_in"):
            print(f"{'':<32}    crashed in {r['crash_in']}"
                  + (f" at {r['fault_va']}" if r.get("fault_va") else ""))
        prev = k
    best = max(r["kernel_calls"] for r in rows)
    last = rows[-1]
    print("-" * 96)
    print(f"best: {best} kernel calls   now: {last['kernel_calls']}   "
          f"failed indirect calls: {last['failed_icalls']}")
    if last["kernel_calls"] < best:
        print("NOTE: current run is below the best ever. If that was a "
              "deliberate trade, the note above should say why (Rule #1).")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    rec = sub.add_parser("record", help="append the last run to the history")
    rec.add_argument("-m", "--message", required=True,
                     help="one line on what changed")
    rec.add_argument("--note", help="context, e.g. why a dip was accepted")
    rec.add_argument("--force", action="store_true",
                     help="record even with probes in the tree")
    args = ap.parse_args(argv)

    rows = load()
    if args.cmd != "record":
        show(rows)
        return 0

    dirty = probes_present()
    if dirty and not args.force:
        raise SystemExit(
            f"probes still present ({dirty}) - those numbers are not "
            "comparable. Run strip_probes.py --apply, rebuild, re-run, then "
            "record (or pass --force).")

    row = measure()
    row.update({
        "date": datetime.date.today().isoformat(),
        "message": args.message,
        "note": args.note,
        "commit": git("rev-parse", "--short", "HEAD"),
    })
    rows.append(row)
    with open(DB, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    print(f"recorded: {row['kernel_calls']} kernel calls, "
          f"{row['failed_icalls']} failed indirect calls\n")
    show(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
