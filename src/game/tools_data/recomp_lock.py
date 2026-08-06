#!/usr/bin/env python3
"""recomp_lock.py - stop two tools from building or running the port at once.

Why this exists
---------------
The build writes gen/*.c, seed_list.json and stderr.txt, and the run overwrites
stderr.txt again. Two tools doing that concurrently do not fail loudly - they
interleave, and one of them reads the other's stderr.txt and reports a
confident, wrong measurement. That is the worst possible failure mode here,
because every downstream decision is made from those numbers.

Anything that builds or runs takes this lock first.

Two layers, because the first one cannot see the past
-----------------------------------------------------
1. A PID lockfile. Held for the duration, released on every exit path
   including exceptions and Ctrl-C. A lockfile whose PID is dead is stale and
   gets reclaimed, so a hard kill does not wedge the tree forever.

2. An activity check. Tools written before this module - notably the seed
   bisect - hold no lock at all, so layer 1 cannot see them. But anything
   mid-flight is touching stderr.txt or seed_list.json every few seconds, and
   a recent mtime is hard evidence that something is running. It is a
   heuristic, so it warns and can be overridden; the lockfile cannot.

Usage:
    from recomp_lock import build_lock
    with build_lock("bisect_core"):
        ...build and run...

    py -3 tools_data/recomp_lock.py --status     # who holds it, is anything busy
"""
import argparse
import contextlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
LOCK = os.path.join(GAME, ".recomp-build.lock")

# Files a build or run touches continuously while it is working.
WITNESS = ("stderr.txt", "seed_list.json")
BUSY_WINDOW = 300          # seconds; a build step alone can take minutes


def _alive(pid):
    """True if that PID is still running. Windows has no signal 0."""
    if sys.platform == "win32":
        import subprocess
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def holder():
    """The live lock record, or None. Reclaims a stale one as a side effect."""
    try:
        with open(LOCK, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    if not _alive(rec.get("pid", -1)):
        with contextlib.suppress(OSError):
            os.remove(LOCK)
        return None
    return rec


def busy_hint():
    """Newest witness-file age, or None. Catches lock-unaware tools."""
    ages = [time.time() - os.path.getmtime(os.path.join(GAME, w))
            for w in WITNESS if os.path.exists(os.path.join(GAME, w))]
    if not ages:
        return None
    youngest = min(ages)
    return youngest if youngest < BUSY_WINDOW else None


def wait_until_quiet(timeout=7200, poll=60, log=print):
    """Block until nothing is building, or timeout. Returns True if quiet.

    An unattended run started at bedtime should queue behind whatever is
    already going rather than refusing and wasting the night. Polling is fine
    here - the thing being waited on takes minutes per step.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if holder() is None and busy_hint() is None:
            return True
        held = holder()
        who = f"{held['tool']} (pid {held['pid']})" if held else "an unlocked tool"
        log(f"  waiting for {who}; {int(deadline - time.time())}s left")
        time.sleep(poll)
    return holder() is None and busy_hint() is None


@contextlib.contextmanager
def build_lock(tool, force=False):
    """Hold the build lock for the duration, or raise SystemExit."""
    held = holder()
    if held:
        raise SystemExit(
            f"build lock held by {held['tool']} (pid {held['pid']}, "
            f"{int(time.time() - held['started'])}s ago). Wait, or kill it.")

    age = busy_hint()
    if age is not None and not force:
        raise SystemExit(
            f"something is already building or running - {min(WITNESS, key=lambda w: os.path.getmtime(os.path.join(GAME, w)))} "
            f"changed {int(age)}s ago, but nothing holds the lock (a tool that "
            f"predates it). Wait for it, or pass force=True if you are sure.")

    with open(LOCK, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "tool": tool, "started": time.time()}, fh)
    try:
        yield
    finally:
        # Only drop our own lock - never someone else's if ours was reclaimed.
        rec = None
        with contextlib.suppress(OSError, ValueError):
            with open(LOCK, encoding="utf-8") as fh:
                rec = json.load(fh)
        if rec and rec.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                os.remove(LOCK)


def main():
    ap = argparse.ArgumentParser(prog="recomp_lock", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.parse_args()

    held = holder()
    print(f"lock     : {held['tool']} pid {held['pid']} "
          f"({int(time.time() - held['started'])}s)" if held else "lock     : free")
    age = busy_hint()
    print(f"activity : something touched the tree {int(age)}s ago - treat as busy"
          if age is not None else "activity : quiet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
