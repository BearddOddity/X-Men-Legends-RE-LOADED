#!/usr/bin/env python3
"""bisect_core.py - "which one of these N changes broke it?", for any change.

Why this exists
---------------
bisect_seeds.py proved the shape works: add half, measure, keep the half that
behaves, recurse. Its own docstring notes that the bisection is generic and
only apply_set() and measure() are project-specific. That prediction came due
immediately - the stale-flag class has 447 candidate sites and needs exactly
the same treatment, and writing a second bisect that drifts from the first is
how two tools end up disagreeing about what "worse" means.

So the loop lives here once, and a Harness supplies the two project-specific
halves. Today there are two harnesses; that is why this is an abstraction
rather than a wrapper around one caller.

    seeds       add addresses to seed_list.json, re-seed, rebuild
    staleflags  rewrite deferred-flag sites in gen/*.c, rebuild

What it guarantees
------------------
- The tree is snapshotted before anything is touched and restored on EVERY
  exit path, including Ctrl-C and an unhandled exception. The original seed
  revert worked because someone had happened to keep a backup; that was luck,
  and luck is not a rollback strategy.
- It takes the build lock, so it cannot interleave with another build.
- A build failure or a missing run log counts as WORSE, never as "no change" -
  a step that cannot be measured must not be silently kept.
- A run that tripped the watchdog is a lower bound, not a measurement, and is
  refused as a baseline.
- Every measurement is appended to bisect_journal.jsonl, so a later session can
  see what was already tried instead of re-deriving it.

Usage (from src/game/):
    py -3 tools_data/bisect_core.py staleflags --dry-run
    py -3 tools_data/bisect_core.py staleflags --only loc_001F0AA9,loc_001FE7DB
    py -3 tools_data/bisect_core.py seeds --from-gaps

Note: bisect_seeds.py still has its own copy of this loop. It is mid-run as of
this writing and must not be edited underneath itself; fold it onto this module
once it has finished.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import signals                                     # noqa: E402
from recomp_lock import build_lock                 # noqa: E402

PY = sys.executable or "py"
JOURNAL = os.path.join(GAME, "bisect_journal.jsonl")


def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=GAME, capture_output=True, text=True, **kw)


def build():
    r = sh(["cmd", "/c", os.path.join(GAME, "build_compile.bat")])
    return not (r.returncode and "error" in (r.stdout + r.stderr).lower())


def run_once():
    sh(["cmd", "/c", os.path.join(GAME, "run.bat")])
    sig = signals.read()
    if sig and sig.get("stale"):
        # run.bat produced no fresh log, so these counters describe the
        # previous tree. Unmeasurable is the honest answer; the caller treats
        # that as WORSE and drops the candidate rather than trusting numbers
        # that were never actually produced by this build.
        print("    run produced no fresh log - treating as unmeasurable")
        return None
    return sig


# --------------------------------------------------------------------------
# Harnesses: the only project-specific part.
# --------------------------------------------------------------------------
class Harness:
    name = "?"

    def items(self):        raise NotImplementedError
    def label(self, item):  return str(item)
    def snapshot(self):     raise NotImplementedError
    def restore(self, tok): raise NotImplementedError
    def apply(self, subset): raise NotImplementedError

    def measure(self, runs=1):
        """Worst case across `runs`, so a candidate cannot be kept by luck.

        1 is the right default here: the boot was measured at 1452/152/11 on
        three consecutive runs on 2026-08-06, so it is deterministic even
        though it ends in a watchdog hang. Raise it if that stops being true.
        """
        acc = None
        for _ in range(max(1, runs)):
            sig = run_once()
            if sig is None:
                return None
            acc = sig if acc is None else signals.worst_of(acc, sig)
        return acc


class StaleFlagHarness(Harness):
    """Deferred-flag repairs in gen/*.c.

    Cheaper per step than seeds: the fixes are edits to already-generated code,
    so a step is restore-edit-build-run with no re-seeding pass at all.
    """
    name = "staleflags"

    def __init__(self):
        import fix_stale_flags as fx
        self.fx = fx

    def items(self):
        by_file, cache = self.fx.plan()
        out = []
        for path, edits in by_file.items():
            for e in edits:
                out.append(e["label"])
        return sorted(set(out))

    def snapshot(self):
        """Copy only the files the full plan would touch - not all of gen/."""
        by_file, _ = self.fx.plan()
        tmp = tempfile.mkdtemp(prefix="staleflag-")
        for path in by_file:
            shutil.copy2(path, os.path.join(tmp, os.path.basename(path)))
        return tmp

    def restore(self, tok):
        for name in os.listdir(tok):
            shutil.copy2(os.path.join(tok, name),
                         os.path.join(self.fx.GEN, name))

    def apply(self, subset):
        by_file, cache = self.fx.plan(set(subset))
        if subset and not by_file:
            return False                    # asked for sites that vanished
        self.fx.apply(by_file, cache)
        return build()


class SeedHarness(Harness):
    """Addresses added to seed_list.json, then the full seeding pipeline."""
    name = "seeds"
    STEPS = (
        ["tools_data/seed_missing_functions.py", "--from-list", "seed_list.json", "--apply"],
        ["tools_data/stub_overridden.py", "--apply"],
        ["tools_data/manual_edits.py", "apply"],
        ["tools_data/manual_edits.py", "apply"],
    )

    def __init__(self, from_gaps=False, addrs=()):
        self.seed_list = os.path.join(GAME, "seed_list.json")
        self.base = json.load(open(self.seed_list))["addresses"]
        self._items = list(addrs) or (self._from_gaps() if from_gaps else [])
        have = set(self.base)
        self._items = [a for a in self._items if a not in have]

    def _from_gaps(self):
        import re
        r = sh([PY, "tools_data/find_icall_gaps.py"])
        out, seen = [], False
        for line in r.stdout.splitlines():
            if line.startswith("=== FRAGMENTS"):
                seen = True
                continue
            if seen and line.startswith("==="):
                break
            m = re.match(r"\s+0x([0-9A-Fa-f]{8})\s", line)
            if seen and m:
                out.append(int(m.group(1), 16))
        return out

    def items(self):            return self._items
    def label(self, item):      return f"0x{item:08X}"

    def snapshot(self):
        tmp = tempfile.mkdtemp(prefix="seeds-")
        shutil.copy2(self.seed_list, os.path.join(tmp, "seed_list.json"))
        return tmp

    def restore(self, tok):
        shutil.copy2(os.path.join(tok, "seed_list.json"), self.seed_list)

    def apply(self, subset):
        d = json.load(open(self.seed_list))
        d["addresses"] = sorted(set(self.base) | set(subset))
        d["count"] = len(d["addresses"])
        with open(self.seed_list, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1)
            fh.write("\n")
        for step in self.STEPS:
            r = sh([PY] + step)
            if r.returncode and "manual_edits" not in step[0]:
                return False
        return build()


class BatchSeedHarness(SeedHarness):
    """Seeds, but a candidate is a GROUP of addresses rather than one.

    A seed step costs a full re-seed plus a build, roughly ten minutes, so
    one-at-a-time caps an overnight run at about two dozen functions. There are
    1,289 functions that vtables provably call and the recompiler never
    emitted; at one per build that is three weeks of nights.

    Batching trades resolution for reach. A batch that behaves keeps ~50
    functions in one step. A batch that misbehaves is dropped whole - so a
    single bad function loses its 49 innocent neighbours - which is the right
    trade only because the pool is huge and the alternative covers almost none
    of it. Re-run a dropped batch through `bisect_core.py seeds` afterwards to
    recover the good ones; that is what bisection is for.
    """
    name = "seedbatch"

    def __init__(self, addrs=(), batch=50):
        SeedHarness.__init__(self, addrs=addrs)
        self._batches = [tuple(self._items[i:i + batch])
                         for i in range(0, len(self._items), batch)]

    def items(self):
        return list(self._batches)

    def label(self, b):
        return f"{len(b)}fns@0x{b[0]:06X}"

    def apply(self, subset):
        flat = [a for b in subset for a in b]
        return SeedHarness.apply(self, flat)


HARNESSES = {"seeds": SeedHarness, "staleflags": StaleFlagHarness,
             "seedbatch": BatchSeedHarness}


# --------------------------------------------------------------------------
# The generic loop.
# --------------------------------------------------------------------------
def journal(harness, subset, sig, verdict):
    rec = {"ts": int(time.time()), "harness": harness, "n": len(subset),
           "subset": list(subset)[:40], "verdict": verdict,
           "signals": sig or {}}
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def evaluate(h, subset, base_sig, tag):
    print(f"  [{tag}] trying {len(subset)}")
    if not h.apply(subset):
        print("    build/apply failed - counts as WORSE")
        journal(h.name, [h.label(i) for i in subset], None, "build-failed")
        return False
    sig = h.measure()
    if sig is None:
        print("    no run log - counts as WORSE")
        journal(h.name, [h.label(i) for i in subset], None, "no-log")
        return False
    bad = signals.worse_than(sig, base_sig)
    print(f"    {signals.fmt(sig)}   " + ("WORSE: " + "; ".join(bad) if bad else "ok"))
    journal(h.name, [h.label(i) for i in subset], sig,
            "worse" if bad else "ok")
    return not bad


def bisect(h, cands, base_sig, tok, depth=0):
    if not cands:
        return []
    if evaluate(h, cands, base_sig, f"d{depth} n={len(cands)}"):
        print(f"    -> all {len(cands)} safe")
        return list(cands)
    if len(cands) == 1:
        print(f"    -> CULPRIT {h.label(cands[0])}")
        return []
    mid = len(cands) // 2
    h.restore(tok)
    left = bisect(h, cands[:mid], base_sig, tok, depth + 1)
    h.restore(tok)
    right = bisect(h, cands[mid:], base_sig, tok, depth + 1)
    return left + right


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bisect_core", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=sorted(HARNESSES))
    ap.add_argument("--only", help="comma-separated subset of candidates")
    ap.add_argument("--from-gaps", action="store_true", help="seeds: use find_icall_gaps")
    ap.add_argument("--dry-run", action="store_true", help="plan only, build nothing")
    ap.add_argument("--force", action="store_true", help="ignore the activity check")
    a = ap.parse_args(argv)

    kw = {}
    if a.kind == "seeds":
        kw["from_gaps"] = a.from_gaps
        if a.only:
            kw["addrs"] = [int(x, 16) for x in a.only.replace(" ", "").split(",")]
            a.only = None
    h = HARNESSES[a.kind](**kw)

    cands = h.items()
    if a.only:
        want = set(a.only.replace(" ", "").split(","))
        cands = [c for c in cands if h.label(c) in want]
    if not cands:
        sys.exit("no candidates")

    print(f"harness   : {h.name}")
    print(f"candidates: {len(cands)}")
    for c in cands[:20]:
        print(f"  {h.label(c)}")
    if len(cands) > 20:
        print(f"  ... and {len(cands) - 20} more")
    print(f"gate      : {signals.header()}")

    if a.dry_run:
        print("\ndry run - nothing built")
        return 0

    with build_lock(f"bisect_core:{h.name}", force=a.force):
        tok = h.snapshot()
        print(f"snapshot  : {tok}")
        try:
            print("\nbaseline (no candidates)...")
            if not h.apply([]):
                sys.exit("baseline build failed - fix that first")
            base = h.measure()
            if base is None:
                sys.exit("baseline produced no run log")
            if base.get("hung"):
                sys.exit("baseline run hung - its counters are a lower bound, "
                         "not a measurement; fix the hang before bisecting")
            print(f"  baseline {signals.fmt(base)}")
            journal(h.name, [], base, "baseline")

            print("\nbisecting...")
            h.restore(tok)
            safe = bisect(h, cands, base, tok)

            print("\n=== result ===")
            bad = [c for c in cands if c not in safe]
            print(f"safe ({len(safe)}): " + (", ".join(h.label(c) for c in safe) or "none"))
            print(f"BAD  ({len(bad)}): " + (", ".join(h.label(c) for c in bad) or "none"))

            h.restore(tok)
            print("\napplying the safe set...")
            if h.apply(safe):
                print(f"  final {signals.fmt(h.measure())}")
        except BaseException:
            h.restore(tok)
            print("\nrestored the tree after an interrupted run")
            raise
    print(f"\njournal: {os.path.basename(JOURNAL)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
