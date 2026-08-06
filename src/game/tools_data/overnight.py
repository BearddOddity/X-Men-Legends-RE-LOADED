#!/usr/bin/env python3
"""overnight.py - work the candidate list unattended and leave a report.

Why this exists
---------------
Most of the wall-clock cost on this port is build-and-measure, roughly minutes
per experiment, and almost none of it needs a human in the loop. A night is
about eighty experiments. Spending that time asleep instead of watching a
progress bar is the single cheapest speedup available.

What it does
------------
Greedy accumulation, not bisection. Bisection answers "which one broke it?";
this answers "how much of this list can I keep?". Start from a measured
baseline, try each candidate on top of everything kept so far, keep it if the
gate says it did not make things worse, drop it if it did, move on. The kept
set only grows, so the tree ends the night at the best configuration found.

Bisection is still the right tool once something is known to be broken - this
one is for grinding a long candidate list with nobody watching.

Stages
------
--then chains a second plan after the first, under the same lock and the same
snapshot. The point is a shakedown: run a handful of high-confidence
candidates first, and if the harness turns out to be broken, that costs twenty
minutes instead of the whole night. Stage two inherits stage one's kept set and
skips anything already decided, so nothing is measured twice.

Unattended safety
-----------------
The whole design assumes nobody is awake to notice a problem:

- Waits for any running build to finish rather than refusing and wasting the
  night, then holds the build lock for the duration.
- Hard wall-clock deadline, shared across stages. Checked before starting each
  experiment, never mid-build, so it always stops cleanly between steps.
- The tree is snapshotted up front and restored on every exit path, including
  Ctrl-C, a crash, and the deadline.
- The night ends by re-applying the best-known set and rebuilding, so the
  morning tree is the best result, not whatever the last experiment left.
- A build failure or an unreadable run log drops that candidate and continues.
  Consecutive failures abort - that means the tree is broken, and eighty more
  failures would prove nothing.
- If the shakedown stage ends in an error, the chain stops rather than taking
  a broken harness into the long plan.
- Never prompts. Nothing here can block on input.

Usage (from src/game/):
    py -3 tools_data/overnight.py --plan confirmed --then staleflags-memory --hours 8
    py -3 tools_data/overnight.py --plan staleflags-all --hours 10
    py -3 tools_data/overnight.py --plan confirmed --dry-run

Read overnight_report.md in the morning.
"""
import argparse
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import signals                                              # noqa: E402
import bisect_core as bc                                    # noqa: E402
from recomp_lock import build_lock, wait_until_quiet        # noqa: E402

REPORT = os.path.join(GAME, "overnight_report.md")
MAX_CONSECUTIVE_FAILURES = 3

# The memory manager, where the allocation failure lives.
MM_LO, MM_HI = 0x001F0000, 0x00210000

# Verified against the original XBE bytes, not inferred from the lift.
CONFIRMED = ["loc_001F0AA9", "loc_001FE7DB", "loc_002041DC"]

PLANS = ("confirmed", "staleflags-memory", "staleflags-all")


def plan_candidates(name, every):
    """Which sites this plan grinds, in the order it tries them."""
    if name == "confirmed":
        return [c for c in CONFIRMED if c in every]
    if name == "staleflags-all":
        # Confirmed first, so an early death still yields the best-evidenced result.
        return ([c for c in CONFIRMED if c in every]
                + [c for c in every if c not in CONFIRMED])
    if name == "staleflags-memory":
        keep = []
        for c in every:
            try:
                a = int(c.split("_")[1], 16)
            except (IndexError, ValueError):
                continue
            if MM_LO <= a < MM_HI:
                keep.append(c)
        return ([c for c in CONFIRMED if c in keep]
                + [c for c in keep if c not in CONFIRMED])
    raise SystemExit(f"unknown plan {name}")


def write_report(night):
    """Rewritten after every experiment, so a 4am reboot still leaves a report."""
    out = [
        "# Overnight run",
        "",
        f"- started: {time.strftime('%Y-%m-%d %H:%M', time.localtime(night['start']))}",
        f"- elapsed: {int((time.time() - night['start']) / 60)} min",
        f"- gate: `{signals.header()}` (higher/lower/higher is better)",
        f"- baseline: `{signals.fmt(night.get('baseline'))}`",
        f"- current: `{signals.fmt(night.get('best'))}`",
        "",
    ]
    if night.get("delta"):
        out += ["## Net movement", ""]
        for k, (b, n) in night["delta"].items():
            word = "no change" if b == n else ("better" if
                   ((signals.GATED[k] == "up") == (n > b)) else "WORSE")
            out.append(f"- `{k}`: {b} -> {n} — {word}")
        out.append("")

    for st in night["stages"]:
        out += [f"## Stage: `{st['plan']}`", "",
                f"{st['n']} of {st['total']} candidates tried", ""]
        out += ["### Kept", ""]
        out += [f"- `{c}` — {signals.fmt(s)}" for c, s in st["kept"]] or ["- none"]
        out += ["", "### Dropped", ""]
        out += [f"- `{c}` — {why}" for c, why in st["dropped"]] or ["- none"]
        if st.get("error"):
            out += ["", f"**ended early:** {st['error']}"]
        out.append("")

    if night.get("error"):
        out += ["## Ended early", "", "```", night["error"], "```", ""]
    out += ["---", "",
            "Tree was left at the kept set above and rebuilt. "
            "Per-experiment detail is in `bisect_journal.jsonl`."]
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


def run_stage(h, plan, cands, tok, deadline, keep, night, runs=1):
    """Greedily accumulate over `cands`. Returns the kept set, grown."""
    st = {"plan": plan, "n": 0, "total": len(cands), "kept": [], "dropped": []}
    night["stages"].append(st)
    fails = 0

    for c in cands:
        if time.time() > deadline:
            st["error"] = "wall-clock budget reached"
            print("\n  budget reached - stopping between experiments")
            break
        if fails >= MAX_CONSECUTIVE_FAILURES:
            st["error"] = (f"{fails} builds failed in a row - the tree is "
                           f"broken, stopping rather than grinding")
            print("\n  " + st["error"])
            break

        st["n"] += 1
        left = int((deadline - time.time()) / 60)
        print(f"\n[{plan} {st['n']}/{len(cands)}] +{c}   ({left} min left)")

        h.restore(tok)
        if not h.apply(keep + [c]):
            st["dropped"].append((c, "build failed"))
            bc.journal("overnight", keep + [c], None, "build-failed")
            fails += 1
            write_report(night)
            continue

        sig = h.measure(runs)
        if sig is None:
            st["dropped"].append((c, "no run log"))
            bc.journal("overnight", keep + [c], None, "no-log")
            fails += 1
            write_report(night)
            continue

        fails = 0
        bad = signals.worse_than(sig, night["best"])
        if bad:
            st["dropped"].append((c, "; ".join(bad)))
            bc.journal("overnight", keep + [c], sig, "worse")
            print(f"    {signals.fmt(sig)}  DROP: {'; '.join(bad)}")
        else:
            keep.append(c)
            st["kept"].append((c, sig))
            night["best"] = sig
            bc.journal("overnight", keep, sig, "kept")
            print(f"    {signals.fmt(sig)}  KEEP")
        write_report(night)

    return keep


def main(argv=None):
    ap = argparse.ArgumentParser(prog="overnight", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", default="confirmed", choices=PLANS)
    ap.add_argument("--then", dest="then", choices=PLANS,
                    help="second stage, run only if the first ends cleanly")
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--runs", type=int, default=1,
                    help="runs per experiment; worst case wins. 1 is right "
                         "while the boot measures deterministically")
    ap.add_argument("--wait-hours", type=float, default=2.0,
                    help="how long to wait for a running build before giving up")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    h = bc.StaleFlagHarness()
    every = h.items()
    stages = [a.plan] + ([a.then] if a.then else [])
    print(f"gate      : {signals.header()}")
    print(f"budget    : {a.hours}h")
    for s in stages:
        cs = plan_candidates(s, every)
        print(f"stage     : {s} ({len(cs)} candidates)")
        for c in cs[:12]:
            print(f"    {c}")
        if len(cs) > 12:
            print(f"    ... and {len(cs) - 12} more")
    if a.dry_run:
        print("\ndry run - nothing built")
        return 0

    print("\nwaiting for the tree to go quiet...")
    if not wait_until_quiet(timeout=int(a.wait_hours * 3600)):
        sys.exit("something is still building after the wait - not starting")

    # Whatever we waited on may have regenerated gen/, so the candidate list
    # from before the wait can be stale. Re-derive it against the tree we
    # actually got.
    every = h.items()
    print(f"re-scanned after the wait: {len(every)} candidate site(s)")

    deadline = time.time() + a.hours * 3600
    night = {"start": time.time(), "stages": [], "baseline": None, "best": None}

    with build_lock("overnight"):
        tok = h.snapshot()
        print(f"snapshot  : {tok}")
        keep = []
        try:
            print("\nbaseline...")
            if not h.apply([]):
                sys.exit("baseline build failed - fix that before running a night")
            base = h.measure(a.runs)
            if base is None:
                sys.exit("baseline produced no run log")
            if base.get("hung"):
                print("  WARNING: baseline hung; counters are a lower bound. "
                      "Continuing, but treat the deltas as soft.")
            night["baseline"] = night["best"] = base
            print(f"  {signals.fmt(base)}")
            bc.journal("overnight", [], base, "baseline")
            write_report(night)

            decided = set()
            for stage in stages:
                cands = [c for c in plan_candidates(stage, every) if c not in decided]
                if not cands:
                    continue
                keep = run_stage(h, stage, cands, tok, deadline, keep, night, a.runs)
                decided |= set(cands)
                last = night["stages"][-1]
                if last.get("error") and "budget" not in last["error"]:
                    night["error"] = (f"stage {stage} ended in an error - not "
                                      f"starting the next stage")
                    print("\n" + night["error"])
                    break
                if time.time() > deadline:
                    break

            print(f"\nrebuilding at the kept set ({len(keep)})...")
            h.restore(tok)
            if h.apply(keep):
                final = h.measure(a.runs)
                if final:
                    night["best"] = final
                print(f"  final {signals.fmt(night['best'])}")
            else:
                night["error"] = ("final rebuild of the kept set FAILED - tree "
                                  "restored to the pristine snapshot instead")
                h.restore(tok)
                h.apply([])
                print("  " + night["error"])

            night["delta"] = {k: (night["baseline"].get(k, 0), night["best"].get(k, 0))
                              for k in signals.GATED}
        except BaseException as exc:
            night["error"] = "".join(
                traceback.format_exception_only(type(exc), exc)).strip()
            h.restore(tok)
            h.apply([])
            write_report(night)
            print("\ntree restored after an interrupted night")
            raise
        finally:
            write_report(night)

    print(f"\nreport: {os.path.basename(REPORT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
