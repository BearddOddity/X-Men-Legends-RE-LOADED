#!/usr/bin/env python3
"""signals.py - the single definition of "did this build get further?".

Why this exists
---------------
The same four counters were being re-counted in three places: bisect_seeds.py,
smoke_test.ps1, and phase.py. They had already drifted - one of them gated on
safe_stub, which on a spinning build is time-boxed by the watchdog and varies
run to run by construction, so gating on it rejects good changes at random.

A measurement that means different things to different tools is worse than no
measurement, because every tool still reports a confident number. This module
is the one place the counters and their directions are defined; everything else
imports it.

Directions
----------
    kernel_calls  UP    fewer = the game died earlier
    heap_allocs   UP    fewer = the allocator regressed
    failed_icalls DOWN  more  = a function went missing
    safe_stub     n/a   NOT gated - watchdog time-boxing makes it noise

safe_stub is still parsed and reported, because it is useful to a human reading
a diff. It is simply never allowed to decide anything automatically.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
STDERR = os.path.join(GAME, "stderr.txt")

# name -> pattern counted in the run log
COUNTS = {
    "kernel_calls": r"\[KERNEL\] #",
    "failed_icalls": r"Failed to resolve VA",
    "heap_allocs": r"\[HEAP\] #",
    "safe_stub": r"ICALL_SAFE_STUB",
}

# Signals read as a single number rather than counted occurrences.
VALUES = {
    "reached": r"\[COVERAGE\] distinct=(\d+)",
}

# name -> "up" (higher is better) or "down" (lower is better).
# Anything absent from this dict is reported but never gates a decision.
GATED = {
    # Distinct dispatch targets entered. FIRST because it is the only signal
    # with any resolution: kernel_calls saturates where the boot stops and read
    # 1452 for every experiment across a whole day of automated work, so no tool
    # could tell a small gain from none. This one moves whenever more of the
    # engine runs, even when the boot still stops in the same place.
    "reached": "up",
    "kernel_calls": "up",
    "failed_icalls": "down",
    "heap_allocs": "up",
}

# A run that tripped the watchdog never reached its natural end, so its counters
# are a lower bound, not a measurement. Callers must not baseline on one.
HANG = r"\[WATCHDOG\] No progress"


def parse(text):
    sig = {n: len(re.findall(p, text)) for n, p in COUNTS.items()}
    for name, pat in VALUES.items():
        m = re.findall(pat, text)
        sig[name] = int(m[-1]) if m else 0
    sig["hung"] = bool(re.search(HANG, text))
    return sig


# Anything newer than the run log means the log describes an older tree.
BUILD_ARTIFACTS = ("seed_list.json", "src/recomp/gen/recomp_seed.c")


def read(path=None):
    """Parse the run log; None if it is not there (a run that never started).

    Also flags a log that predates the tree it supposedly measured. This is a
    real trap, not a hypothetical: a bisect left seed_list.json newer than
    stderr.txt, and reading the counters straight out gave a confident
    1452 -> 62 "regression" that had simply never been measured. A stale log
    is far more dangerous than a missing one, because it still returns numbers.
    """
    path = path or STDERR
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            sig = parse(fh.read())
    except OSError:
        return None

    try:
        log_age = os.path.getmtime(path)
        newer = [a for a in BUILD_ARTIFACTS
                 if os.path.exists(os.path.join(GAME, a))
                 and os.path.getmtime(os.path.join(GAME, a)) > log_age]
    except OSError:
        newer = []
    sig["stale"] = bool(newer)
    sig["stale_because"] = newer
    return sig


def worst_of(a, b):
    """Pessimistic merge of two runs of the same build.

    A build that hangs is cut off by the watchdog, so its counters are a lower
    bound that moves with timing. Comparing two single noisy runs makes the
    gate flip on noise. Taking the worse value per signal means a candidate has
    to be good on EVERY run to be kept - it can lose to noise, never win by it,
    which is the safe direction when nobody is awake to sanity-check the call.
    """
    if a is None or b is None:
        return None
    out = dict(a)
    for name, direction in GATED.items():
        x, y = a.get(name, 0), b.get(name, 0)
        out[name] = min(x, y) if direction == "up" else max(x, y)
    out["hung"] = bool(a.get("hung") or b.get("hung"))
    out["stale"] = bool(a.get("stale") or b.get("stale"))
    return out


def spread(runs):
    """Per-signal (min, max) across runs - how noisy the measurement is."""
    return {n: (min(r.get(n, 0) for r in runs), max(r.get(n, 0) for r in runs))
            for n in GATED}


def worse_than(now, base):
    """Directional comparison. Returns reasons; empty list means not worse."""
    bad = []
    for name, direction in GATED.items():
        n, b = now.get(name, 0), base.get(name, 0)
        if direction == "up" and n < b:
            bad.append(f"{name} {b} -> {n}")
        elif direction == "down" and n > b:
            bad.append(f"{name} {b} -> {n}")
    return bad


def fmt(sig):
    if sig is None:
        return "(no run)"
    s = "/".join(str(sig.get(n, "?")) for n in GATED)
    if sig.get("hung"):
        s += "  HUNG"
    if sig.get("stale"):
        s += "  STALE(" + ",".join(os.path.basename(p)
                                   for p in sig["stale_because"]) + ")"
    return s


def header():
    """Column legend for fmt(), so a log line is readable without this file."""
    return "/".join(GATED)


if __name__ == "__main__":
    sig = read()
    print(header())
    print(fmt(sig))
    if sig:
        print(f"safe_stub {sig['safe_stub']} (not gated)")
