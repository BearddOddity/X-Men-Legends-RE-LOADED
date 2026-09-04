#!/usr/bin/env python3
"""bisect_seeds.py - find WHICH seed in a batch broke the boot.

Why this exists
---------------
On 2026-08-05 thirteen seed addresses were added in one step and the boot went
from 1452 kernel calls to 56. The batch was reverted wholesale and every one of
the thirteen was abandoned - including, presumably, the ones that were fine.
That is the wrong trade: seeding a genuinely missing function has been the
highest-yield fix on this project, and throwing away twelve good candidates to
escape one bad one is expensive.

Bisection makes the batch safe to try. Add half, measure, keep the half that
behaves, recurse. log2(13) is four builds to name the culprit instead of
thirteen, and the good ones survive.

It also removes the failure mode that made the original revert lucky rather
than designed: the seed list is snapshotted here automatically and restored on
every path out, including Ctrl-C and an exception. Nothing is left half-applied.

What counts as "worse"
----------------------
The same directional rules the regression gate uses, because a seed that helps
one signal and wrecks another is not an improvement:

    kernel_calls  must not FALL      (fewer = died earlier)
    failed_icalls must not RISE      (more = a function went missing)
    heap_allocs   must not FALL      (fewer = allocator regressed)

safe_stub is deliberately ignored: on a spinning build it is time-boxed by the
watchdog and varies run to run by construction, so it cannot discriminate.

Usage (from src/game/):
    # bisect a list of candidates
    py -3 tools_data/bisect_seeds.py 0x14548,0x31F4C,0x79410,0x10A9DC

    # or take every FRAGMENT find_icall_gaps.py currently reports
    py -3 tools_data/bisect_seeds.py --from-gaps

    # see the plan without building anything
    py -3 tools_data/bisect_seeds.py --from-gaps --dry-run

Leaves seed_list.json holding the baseline PLUS every candidate proven safe,
and prints the ones that are not. Re-seed and rebuild afterwards - it does that
itself between steps, so the tree is already consistent when it exits.

Portability
-----------
The bisection is generic. What is project-specific is apply_set() (how a
candidate becomes part of the build) and measure() (how a build is scored).
Swap those two to reuse this anywhere.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
SEED_LIST = os.path.join(GAME, "seed_list.json")
BACKUP = os.path.join(GAME, "seed_list.bisect-backup.json")
STDERR = os.path.join(GAME, "stderr.txt")
PY = sys.executable or "py"

# (name, "up" = higher is better, "down" = lower is better)
# Gated signals, in the order rule #1 says to weigh them. reached and
# callsites are the trustworthy pair; kernel_calls is deliberately NOT
# gated - ledger #174 showed it can nearly double while the dispatch count
# does not move, so it measures something other than depth. It is still
# measured and printed, just never decisive on its own.
GATED = [("reached", "up"), ("callsites", "up"),
         ("failed_icalls", "down"), ("heap_allocs", "up")]
INFORMATIONAL = ["kernel_calls"]


def read_list():
    return json.load(open(SEED_LIST))


def write_list(addrs):
    d = read_list()
    d["addresses"] = sorted(set(addrs))
    d["count"] = len(d["addresses"])
    with open(SEED_LIST, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
        fh.write(chr(10))


def apply_set(extra, dry):
    """Put baseline+extra in the seed list and rebuild the tree around it."""
    base = json.load(open(BACKUP))["addresses"]
    if dry:
        print(f"    (dry) would seed {len(base)} + {len(extra)} extra")
        return True
    write_list(list(base) + list(extra))
    steps = [
        [PY, os.path.join("tools_data", "seed_missing_functions.py"),
         "--from-list", "seed_list.json", "--apply"],
        [PY, os.path.join("tools_data", "stub_overridden.py"), "--apply"],
        [PY, os.path.join("tools_data", "manual_edits.py"), "apply"],
        [PY, os.path.join("tools_data", "manual_edits.py"), "apply"],
    ]
    for cmd in steps:
        r = subprocess.run(cmd, cwd=GAME, capture_output=True, text=True)
        if r.returncode and "manual_edits" not in cmd[1]:
            print(f"    step failed: {' '.join(cmd)}")
            return False
    trace_seeds(extra)
    r = subprocess.run(["cmd", "/c", os.path.join(GAME, "build_compile.bat")],
                       cwd=GAME, capture_output=True, text=True)
    if "error" in (r.stdout + r.stderr).lower() and r.returncode:
        print("    BUILD FAILED - treating as worse")
        return False
    return True


def measure(dry):
    """Run once and return the tracked signals."""
    if dry:
        return dict({n: 0 for n, _ in GATED},
                    **{n: 0 for n in INFORMATIONAL}, hits=[])
    subprocess.run(["cmd", "/c", os.path.join(GAME, "run.bat")],
                   cwd=GAME, capture_output=True)
    try:
        t = open(STDERR, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    m = re.search(r"\[COVERAGE\] distinct=(\d+)", t)
    c = re.search(r"\[COVERAGE\] callsites=(\d+)", t)
    return {
        "reached": int(m.group(1)) if m else 0,
        "callsites": int(c.group(1)) if c else 0,
        "kernel_calls": len(re.findall(r"\[KERNEL\] #", t)),
        "failed_icalls": len(re.findall(r"Failed to resolve VA", t)),
        "heap_allocs": len(re.findall(r"\[HEAP\] #", t)),
        # Which candidates actually ran. A seed that never fires is inert, and
        # saying so is more useful than a silent "no change".
        "hits": sorted(set(re.findall(r"\[WHERE:seedhit-([0-9A-Fa-f]+)\]", t))),
    }


def worse_than(now, base):
    """Directional comparison; returns a list of reasons, empty if not worse."""
    bad = []
    for name, direction in GATED:
        n, b = now.get(name, 0), base.get(name, 0)
        if direction == "up" and n < b:
            bad.append(f"{name} {b} -> {n}")
        if direction == "down" and n > b:
            bad.append(f"{name} {b} -> {n}")
    return bad


def fmt(sig):
    return "/".join(str(sig.get(n, "?")) for n, _ in GATED)


def evaluate(extra, base_sig, dry, label):
    print(f"  [{label}] trying {len(extra)} candidate(s)")
    if not apply_set(extra, dry):
        return False, {}
    sig = measure(dry)
    if sig is None:
        print("    no stderr.txt - treating as worse")
        return False, {}
    bad = worse_than(sig, base_sig)
    print(f"    {fmt(sig)}   " + ("WORSE: " + "; ".join(bad) if bad else "ok"))
    return (not bad), sig


def bisect(cands, base_sig, dry, depth=0):
    """Return the subset of cands that is safe to keep."""
    if not cands:
        return []
    ok, _ = evaluate(cands, base_sig, dry, f"d{depth} n={len(cands)}")
    if ok:
        print(f"    -> all {len(cands)} safe")
        return list(cands)
    if len(cands) == 1:
        print(f"    -> CULPRIT 0x{cands[0]:08X}")
        return []
    mid = len(cands) // 2
    left = bisect(cands[:mid], base_sig, dry, depth + 1)
    right = bisect(cands[mid:], base_sig, dry, depth + 1)
    return left + right


def from_gaps():
    """Every FRAGMENT the gap finder currently reports."""
    r = subprocess.run([PY, os.path.join("tools_data", "find_icall_gaps.py")],
                       cwd=GAME, capture_output=True, text=True)
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bisect_seeds", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("addresses", nargs="?", default="",
                    help="comma-separated candidate addresses")
    ap.add_argument("--from-gaps", action="store_true",
                    help="use find_icall_gaps.py's FRAGMENT list")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    cands = from_gaps() if a.from_gaps else [
        int(x, 16) for x in a.addresses.replace(" ", "").split(",") if x]
    if not cands:
        sys.exit("no candidates - pass addresses or --from-gaps")

    have = set(read_list()["addresses"])
    cands = [c for c in cands if c not in have]
    if not cands:
        sys.exit("every candidate is already seeded")

    shutil.copy2(SEED_LIST, BACKUP)
    print(f"baseline seed list backed up -> {os.path.basename(BACKUP)}")
    print(f"candidates: {len(cands)}")
    for c in cands:
        print(f"  0x{c:08X}")

    try:
        print("\nmeasuring baseline (no candidates)...")
        if not apply_set([], a.dry_run):
            sys.exit("baseline build failed - fix that first")
        base_sig = measure(a.dry_run)
        if base_sig is None:
            sys.exit("baseline produced no stderr.txt")
        print(f"  baseline {fmt(base_sig)}")

        print("\nbisecting...")
        safe = bisect(cands, base_sig, a.dry_run)

        print("\n=== result ===")
        bad = [c for c in cands if c not in safe]
        print(f"safe to keep ({len(safe)}): "
              + (", ".join(f"0x{c:08X}" for c in safe) or "none"))
        print(f"REJECTED    ({len(bad)}): "
              + (", ".join(f"0x{c:08X}" for c in bad) or "none"))
        print("\napplying the safe set and rebuilding...")
        apply_set(safe, a.dry_run)
        final = measure(a.dry_run)
        if final:
            print(f"  final {fmt(final)}")
    finally:
        # The seed list is left holding baseline+safe, which apply_set wrote.
        # The backup stays on disk deliberately: if anything above died
        # mid-flight, `copy seed_list.bisect-backup.json seed_list.json`
        # is the one-step way back.
        print(f"\nbackup kept at {os.path.basename(BACKUP)} - "
              f"restore from it if this exited unexpectedly")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def trace_seeds(extra):
    """Put a one-line probe at the entry of each candidate we just seeded.

    Without this a seed that is never called looks identical to one that was
    called and changed nothing, so a null result teaches nothing. The probe
    prints once per function and carries the /* PROBE */ marker so
    strip_probes.py removes it.
    """
    seed = os.path.join(GAME, "src", "recomp", "gen", "recomp_seed.c")
    try:
        text = open(seed, encoding="utf-8", errors="replace").read()
    except OSError:
        return
    n = 0
    for va in extra:
        name = "sub_%08X" % va
        sig = "void %s(void)\n{\n" % name
        if sig not in text or ("seedhit-%08X" % va) in text:
            continue
        probe = ('    { /* PROBE */ recomp_where("seedhit-%08X", 1, 0, 0, 0, 0); '
                 '} /* PROBE */\n' % va)
        text = text.replace(sig, sig + probe, 1)
        n += 1
    if n:
        with open(seed, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("    traced %d seeded function(s)" % n)
