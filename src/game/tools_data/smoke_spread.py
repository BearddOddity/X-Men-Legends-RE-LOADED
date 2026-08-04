#!/usr/bin/env python3
"""Run the game N times and report the spread of the tracked signals.

Rule #7 says two runs per number. This answers the prior question: how noisy
is the number in the first place? If kernel_calls varies run to run, then a
single-run delta is not evidence and comparing two builds by one run each is
measuring noise.

Usage:  py -3 tools_data/smoke_spread.py [N]        (default 5)
"""
import collections
import os
import re
import subprocess
import sys

GAME_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STDERR = os.path.join(GAME_DIR, "stderr.txt")
RUN = os.path.join(GAME_DIR, "run.bat")

# Same patterns progress.py uses, so numbers are comparable to progress.json.
SIGNALS = {
    "kernel_calls": r"\[KERNEL\] #",
    "failed_icalls": r"Failed to resolve VA",
    "heap_allocs": r"\[HEAP\] #",
    "safe_stub": r"\[SAFE_STUB\]",
}


def one_run():
    # cmd /c to invoke the .bat; no shell=True, so the path (which has
    # spaces) is passed as one argv entry and nothing is re-parsed.
    subprocess.run(["cmd", "/c", RUN], cwd=GAME_DIR, capture_output=True)
    try:
        text = open(STDERR, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    row = {k: len(re.findall(p, text)) for k, p in SIGNALS.items()}
    m = re.search(r"crashed in (\S+)", text) or re.search(r"\[CRASH\][^\n]*", text)
    row["crash"] = m.group(0)[:60] if m else "(none)"
    return row


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    rows = []
    for i in range(n):
        r = one_run()
        if r is None:
            print(f"run {i+1}: no stderr.txt")
            continue
        rows.append(r)
        print(f"run {i+1}: " + "  ".join(f"{k}={r[k]}" for k in SIGNALS)
              + f"  crash={r['crash']}")

    if not rows:
        return 1

    print("\n--- spread over %d runs ---" % len(rows))
    stable = True
    for k in SIGNALS:
        vals = [r[k] for r in rows]
        lo, hi = min(vals), max(vals)
        flag = "" if lo == hi else "   <-- VARIES"
        if lo != hi:
            stable = False
        print(f"  {k:<14} min={lo:<7} max={hi:<7} {flag}")

    crashes = collections.Counter(r["crash"] for r in rows)
    print("  crash sites:")
    for site, cnt in crashes.most_common():
        print(f"    {cnt}/{len(rows)}  {site}")
    if len(crashes) > 1:
        stable = False

    print("\nVERDICT: " + ("deterministic - single-run deltas are meaningful"
                          if stable else
                          "NON-DETERMINISTIC - a single run is not evidence; "
                          "compare distributions, not points"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
