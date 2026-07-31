"""
find_icall_esp_saves.py - find indirect calls whose _icall_esp save point sits
above the function's callee-saved register pushes.

The bug
-------
RECOMP_ICALL_SAFE restores g_esp to _icall_esp when the target cannot be
resolved, so that a failed stdcall does not leak its arguments. That is only
correct if _icall_esp was captured immediately before the *argument* pushes.

The lifter sometimes captures it before pushes that are callee-saved register
saves, not arguments:

    { uint32_t _icall_esp = g_esp;     <- too early
    PUSH32(esp, ebx);                  <- saves, popped in the epilogue
    PUSH32(esp, ebp);
    PUSH32(esp, esi);
    PUSH32(esp, edi);
    PUSH32(esp, edx);                  <- the actual argument
    PUSH32(esp, 0); RECOMP_ICALL_SAFE(MEM32(eax + 0x58), _icall_esp); }

On failure this unwinds the register saves too, leaving esp N*4 bytes high for
the rest of the function. Every later esp-relative argument read then lands N
slots off. In sub_002235D0 that handed an "igNamedObject" name string to a slot
expecting a constructor; see DEBUGGING_NOTES.md.

Detection
---------
A leading run of PUSH32 of callee-saved registers (ebx/ebp/esi/edi) inside an
icall block, where the enclosing function's epilogue contains a matching POP32
of the same register. The epilogue pop is what distinguishes a save from an
argument that happens to live in ebx.

This reports a shape, not a proven bug - review each site before editing. Bulk
sweeps have caused regressions in this tree before (see DEBUGGING_NOTES.md).

Usage (from src/game/):
    py -3 tools_data/find_icall_esp_saves.py           # summary
    py -3 tools_data/find_icall_esp_saves.py --list    # every site
"""
import glob
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(os.path.dirname(HERE), "src", "recomp", "gen")

CALLEE_SAVED = ("ebx", "ebp", "esi", "edi")

FUNC = re.compile(r"^void (sub_[0-9A-F]+)\(void\)$")
SAVE = re.compile(r"uint32_t _icall_esp = g_esp;")
PUSH = re.compile(r"^\s*PUSH32\(esp, (\w+)\);\s*$")
POP = re.compile(r"^\s*POP32\(esp, (\w+)\);")
CALL = re.compile(r"RECOMP_ICALL_SAFE")


def function_spans(lines):
    """Yield (name, start, end) for each generated function in a file."""
    starts = [(i, m.group(1)) for i, l in enumerate(lines) if (m := FUNC.match(l))]
    for n, (i, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        yield name, i, end


def scan():
    hits, total = [], 0
    for path in sorted(glob.glob(os.path.join(GEN, "recomp_*.c"))):
        lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
        for name, start, end in function_spans(lines):
            body = lines[start:end]
            # Registers this function restores in its epilogue - i.e. the ones
            # it treats as saves rather than arguments.
            popped = {m.group(1) for l in body if (m := POP.match(l))}
            if not popped:
                continue
            for off, line in enumerate(body):
                if not SAVE.search(line):
                    continue
                total += 1
                # Walk the pushes between the save point and the call.
                saves = []
                for j in range(off + 1, min(off + 16, len(body))):
                    if CALL.search(body[j]):
                        break
                    m = PUSH.match(body[j])
                    if not m:
                        continue
                    reg = m.group(1)
                    if reg in CALLEE_SAVED and reg in popped:
                        saves.append(reg)
                    else:
                        break  # first non-save push ends the leading run
                if saves:
                    hits.append({
                        "file": os.path.basename(path),
                        "line": start + off + 1,
                        "func": name,
                        "saves": saves,
                    })
    return total, hits


def live_callers():
    """Functions that actually suffered a failed indirect call last run.

    The esp skew only happens when RECOMP_ICALL_SAFE takes its failure path, so
    a site whose call resolves is harmless no matter where its save point sits.
    Intersecting with the run log turns 2900 candidate shapes into the handful
    that can affect the current boot.
    """
    sys.path.insert(0, HERE)
    import triage_crash as tc
    log = os.path.join(os.path.dirname(HERE), "stderr.txt")
    if not os.path.exists(log):
        return None
    crash = tc.parse_crash(log)
    if not crash:
        return None
    syms = tc.load_map()
    names = set()
    for c in crash["icalls"]:
        for rva in c["stack"]:
            s = tc.symbolise(syms, rva)
            if s and s["name"].startswith("sub_"):
                names.add(s["name"])
    return names


def main(argv):
    total, hits = scan()
    print(f"icall save points scanned            : {total}")
    print(f"sites capturing across register saves: {len(hits)}")

    if "--live" in argv:
        names = live_callers()
        if names is None:
            print("\n--live needs a stderr.txt with ICALL failures; run the game first.")
            return 1
        hits = [h for h in hits if h["func"] in names]
        print(f"...of which appear in the last run's ICALL failure backtraces:"
              f" {len(hits)}")
        print("\nOnly these can skew esp on the current boot path - a site whose")
        print("indirect call resolves never takes the failure path at all.")
    if not hits:
        return 0
    print()
    print("by number of saved registers unwound on failure:")
    for n, c in sorted(Counter(len(h["saves"]) for h in hits).items()):
        print(f"  {n} register(s) ({n * 4:2d} bytes of esp skew)  {c} sites")
    print()
    print("by file:")
    for f, n in Counter(h["file"] for h in hits).most_common(10):
        print(f"  {f:<24} {n}")
    print()
    show = hits if "--list" in argv else hits[:15]
    print("(all sites)" if "--list" in argv else "(first 15)")
    for h in show:
        print(f"  {h['file']}:{h['line']}  {h['func']}  "
              f"saves={','.join(h['saves'])}")
    if "--list" not in argv and len(hits) > 15:
        print(f"  ... {len(hits) - 15} more (--list)")
    print()
    print("Review each before editing - this reports a shape, not a proven bug.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
