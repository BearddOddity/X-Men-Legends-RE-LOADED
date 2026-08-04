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

    # How much code actually ran. The kernel-call count is a narrow proxy: it
    # moved 54 -> 58 across the fall-through-edge fix while total indirect
    # calls went 80 -> 60,164, i.e. ~750x more code executing. Tracking only
    # the proxy made a large advance look like noise.
    #
    # Two sources: the running total the safe stub prints, and the
    # "(total calls: N)" the ICALL failure log has always carried. Take the
    # max, so an older stderr.txt still yields a number.
    totals = [int(n) for n in re.findall(r"\[ICALL-TOTAL\] (\d+)", text)]
    totals += [int(n) for n in re.findall(r"total calls: (\d+)", text)]

    return {
        "kernel_calls": len(re.findall(r"\[KERNEL\] #", text)),
        "failed_icalls": len(re.findall(r"Failed to resolve VA", text)),
        "total_icalls": max(totals) if totals else None,
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
    print(f"{'date':<11} {'kern':>5} {'delta':>6} {'icall':>6} {'TOTAL':>9}  "
          f"{'commit':<8} what")
    print("-" * 106)
    prev = None
    for r in rows:
        k = r["kernel_calls"]
        delta = "" if prev is None else f"{k - prev:+d}"
        # Early entries predate these metrics; show "-" rather than
        # inventing a zero.
        ic = "-" if r.get("failed_icalls") is None else str(r["failed_icalls"])
        tot = "-" if r.get("total_icalls") is None else f"{r['total_icalls']:,}"
        print(f"{r['date']:<11} {k:>5} {delta:>6} {ic:>6} {tot:>9}  "
              f"{r.get('commit', ''):<8} {r['message']}")
        if r.get("note"):
            print(f"{'':<32}  ^ {r['note']}")
        if r.get("crash_in"):
            print(f"{'':<32}    crashed in {r['crash_in']}"
                  + (f" at {r['fault_va']}" if r.get("fault_va") else ""))
        prev = k
    best = max(r["kernel_calls"] for r in rows)
    last = rows[-1]
    tots = [r["total_icalls"] for r in rows if r.get("total_icalls")]
    best_tot = max(tots) if tots else None
    print("-" * 106)
    print(f"best: {best} kernel calls   now: {last['kernel_calls']}   "
          f"failed indirect calls: {last['failed_icalls']}")
    if best_tot:
        cur_tot = last.get("total_icalls")
        print(f"code executed (total indirect calls):  best {best_tot:,}   "
              f"now {cur_tot:,}" if cur_tot else
              f"code executed (total indirect calls):  best {best_tot:,}")
    if last["kernel_calls"] < best:
        print("NOTE: kernel calls are below the best ever - but that count is "
              "a narrow proxy.\n      Check the total above before calling it "
              "a regression (Rules #1, #8).")


def improved(cur, prev):
    """Did any tracked signal move the right way between two entries?"""
    reasons = []
    if cur["kernel_calls"] > prev["kernel_calls"]:
        reasons.append(f"kernel calls {prev['kernel_calls']} -> {cur['kernel_calls']}")
    if (cur.get("failed_icalls") is not None
            and prev.get("failed_icalls") is not None
            and cur["failed_icalls"] < prev["failed_icalls"]):
        reasons.append(f"failed icalls {prev['failed_icalls']} -> {cur['failed_icalls']}")
    # More code executing is progress even when the kernel-call proxy is flat.
    if (cur.get("total_icalls") or 0) > (prev.get("total_icalls") or 0):
        reasons.append(f"total icalls {prev.get('total_icalls')} -> "
                       f"{cur.get('total_icalls')}")
    if (cur.get("heap_allocs") or 0) > (prev.get("heap_allocs") or 0):
        reasons.append(f"heap allocs {prev.get('heap_allocs')} -> {cur.get('heap_allocs')}")
    if cur.get("crash_in") and cur.get("crash_in") != prev.get("crash_in"):
        reasons.append(f"crash moved to {cur['crash_in']}")
    return reasons


def stalled(rows):
    """Rule #14: two consecutive changes improving no tracked signal."""
    if len(rows) < 3:
        print("not enough history to judge")
        return 0
    last_two = [(rows[-2], rows[-3]), (rows[-1], rows[-2])]
    flat = []
    for cur, prev in last_two:
        why = improved(cur, prev)
        flat.append((cur, why))
        mark = "improved" if why else "NO IMPROVEMENT"
        print(f"{cur['date']}  {cur['message'][:58]:<58} {mark}")
        for w in why:
            print(f"    + {w}")
    if all(not why for _, why in flat):
        print("\nRULE #14 TRIGGERED: two consecutive changes improved no tracked")
        print("signal. Stop varying this approach. Pick one, and say which:")
        print("  - go around it   (stub or skip the subsystem and keep moving)")
        print("  - escalate       (report the wall and the options to the user)")
        print("  - switch targets (work a different blocker, come back later)")
        return 1
    print("\nnot stalled - at least one recent change moved a signal")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("stalled", help="Rule #14 check: are we going in circles?")
    rec = sub.add_parser("record", help="append the last run to the history")
    rec.add_argument("-m", "--message", required=True,
                     help="one line on what changed")
    rec.add_argument("--note", help="context, e.g. why a dip was accepted")
    rec.add_argument("--force", action="store_true",
                     help="record even with probes in the tree")
    args = ap.parse_args(argv)

    rows = load()
    if args.cmd == "stalled":
        return stalled(rows)
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
