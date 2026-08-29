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
of the same register, AND the push is that register's first in the function.

The epilogue pop on its own does not distinguish a save from an argument that
happens to live in ebx: if a function saves edi in its prologue and later passes
edi as an argument to a virtual call, the pop exists in both cases. Requiring
the first push is what separates them - the prologue save comes first, every
later push of that register is an argument.

Without that condition this tool rewrote four argument pushes as if they were
saves (sub_00209650 x2, sub_002235D0, sub_00236500). A failed ICALL then
restored esp to *after* the argument, leaking it, and the following POP32 took
the register back from the wrong slot. See docs/PAGE_ZERO_CENSUS.md.

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
            # The FIRST push of a callee-saved register is the prologue save;
            # any later push of that same register is a call argument that
            # happens to live in it. `popped` alone cannot tell them apart,
            # because the epilogue pop belongs to the prologue save either
            # way - which is how four argument pushes were once rewritten as
            # if they were saves. See docs/PAGE_ZERO_CENSUS.md.
            first_push = {}
            for j, l in enumerate(body):
                m = PUSH.match(l)
                if m and m.group(1) in CALLEE_SAVED:
                    first_push.setdefault(m.group(1), j)
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
                    if (reg in CALLEE_SAVED and reg in popped
                            and first_push.get(reg) == j):
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


def apply_fix(hits, only=None):
    """Move each save point below the callee-saved pushes it wrongly spans.

    The transformation is purely mechanical - the same edit was made by hand
    five times, and a full regeneration wipes all of them, so it belongs in the
    detector that already knows exactly which sites qualify (Rule #4/#9).

        {  uint32_t _icall_esp = g_esp;      ->   {
        PUSH32(esp, esi);   /* save */           PUSH32(esp, esi);
        PUSH32(esp, edi);   /* save */           PUSH32(esp, edi);
        PUSH32(esp, edx);   /* argument */       uint32_t _icall_esp = g_esp;
        ... RECOMP_ICALL_SAFE(...)               PUSH32(esp, edx);
                                                 ... RECOMP_ICALL_SAFE(...)
    """
    by_file = {}
    for h in hits:
        if only and h["func"] not in only:
            continue
        by_file.setdefault(h["file"], []).append(h)

    total = 0
    for fname, group in by_file.items():
        path = os.path.join(GEN, fname)
        lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
        # Descending, so earlier line numbers stay valid as we edit.
        for h in sorted(group, key=lambda x: -x["line"]):
            i = h["line"] - 1
            if "_icall_esp = g_esp" not in lines[i]:
                print(f"  SKIP {fname}:{h['line']} - not a save-point line")
                continue
            n = len(h["saves"])
            indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
            note = (f"{indent}/* Manual fix (not in original x86): _icall_esp "
                    f"captured below the\n"
                    f"{indent} * callee-saved push"
                    f"{'es' if n > 1 else ''} of "
                    f"{', '.join(h['saves'])}, which are register saves (the\n"
                    f"{indent} * epilogue pops them), not call arguments. With the "
                    f"save point above\n"
                    f"{indent} * them, a failed ICALL rolled esp back past the "
                    f"saves and left it\n"
                    f"{indent} * {n * 4} bytes high for the rest of the function. "
                    f"Applied by\n"
                    f"{indent} * tools_data/find_icall_esp_saves.py --fix. */")
            # Find the Nth actual PUSH32 after the save point rather than
            # assuming it is N lines down. A previously applied manual comment
            # sits between them at some sites, and counting blindly inserted
            # this note *inside* that comment, producing nested `/*` and a file
            # that would not compile.
            seen, k = 0, i + 1
            while k < len(lines) and seen < n:
                if PUSH.match(lines[k]):
                    seen += 1
                k += 1
            if seen < n:
                print(f"  SKIP {fname}:{h['line']} - could not locate {n} save "
                      f"push(es) below the save point")
                continue
            lines[i] = indent + "{"
            lines[k:k] = note.split("\n") + [
                indent + "uint32_t _icall_esp = g_esp;"]
            total += 1
            print(f"  fixed {fname}:{h['line']}  {h['func']}  "
                  f"saves={','.join(h['saves'])}")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines))
    print(f"\napplied {total} fix(es); rebuild and verify twice (Rules #1, #7)")
    return 0


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

    if "--fix" in argv:
        only = None
        for i, a in enumerate(argv):
            if a == "--only" and i + 1 < len(argv):
                only = set(argv[i + 1].split(","))
        if not only:
            raise SystemExit("--fix requires --only FUNC[,FUNC...]; Rule #6 - "
                             "never sweep this pattern tree-wide, most "
                             "single-register hits are genuine arguments")
        return apply_fix(hits, only)

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
