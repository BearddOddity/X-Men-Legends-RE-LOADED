"""
stub_overridden.py - remove generated bodies that hand-written code replaces.

Why this exists
---------------
A few functions are implemented by hand in `src/recomp_manual.c` (and
`src/d3d8_shim.c`) because the lifter cannot express them correctly. Those are
non-static definitions, so the generated copy in `gen/` collides with them:

    recomp_0011.c.obj : error LNK2005: sub_0019F765 already defined
                                       in recomp_manual.c.obj

Before a regeneration the generated bodies had been replaced by one-line stubs
carrying a `moved to` marker. `manual_edits.py` records those as `replace_line`
edits, but its model is "find the stub, put the stub back" - and a freshly
generated tree has no stub to find, so they silently never re-apply and the
link fails.

Method
------
The authority is the hand-written source itself, not a stored list: every
non-static `void sub_XXXXXXXX(void)` in the override files is a function whose
generated body must go. That way adding a new override needs no bookkeeping -
re-run this and it is handled.

Each generated definition is replaced by a one-line stub carrying the marker
`manual_edits.py` already understands, so the function still exists for the
dispatch table and the next `extract` records it correctly.

Usage (from src/game/):
    py -3 tools_data/stub_overridden.py           # report
    py -3 tools_data/stub_overridden.py --apply
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
GEN = os.path.join(GAME_DIR, "src", "recomp", "gen")
OVERRIDE_SOURCES = [
    os.path.join(GAME_DIR, "src", "recomp_manual.c"),
    os.path.join(GAME_DIR, "src", "d3d8_shim.c"),
]

# Non-static only: a `static void sub_X` is file-local and cannot collide.
DEF_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*$")
GEN_DEF_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*$")


def overridden():
    """Names hand-implemented in the override sources, with their file."""
    found = {}
    for path in OVERRIDE_SOURCES:
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8", errors="ignore"):
            m = DEF_RE.match(line)
            if m:
                found[m.group(1)] = os.path.basename(path)
    return found


def main(argv):
    apply = "--apply" in argv
    names = overridden()
    if not names:
        print("no non-static overrides found")
        return 0
    print(f"hand-written overrides: {len(names)}")
    for n, src in sorted(names.items()):
        print(f"  {n}  <- {src}")
    print()

    stubbed = 0
    for fname in sorted(f for f in os.listdir(GEN)
                        if f.startswith("recomp_") and f.endswith(".c")):
        path = os.path.join(GEN, fname)
        lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
        out, i, changed = [], 0, False
        while i < len(lines):
            m = GEN_DEF_RE.match(lines[i])
            if not m or m.group(1) not in names:
                out.append(lines[i])
                i += 1
                continue
            name = m.group(1)
            # Swallow the definition through its closing brace at column 0.
            j = i + 1
            depth = 0
            started = False
            while j < len(lines):
                depth += lines[j].count("{") - lines[j].count("}")
                if lines[j].startswith("{"):
                    started = True
                if started and depth == 0:
                    break
                j += 1
            # A COMMENT, not a stub definition. Emitting
            # `void name(void) { }` still defines the symbol and duplicates the
            # hand-written one - the link error this exists to fix. The
            # definition has to disappear entirely; callers and the dispatch
            # table resolve to the override in recomp_manual.c / d3d8_shim.c,
            # which recomp_funcs.h already declares.
            out.append(f"/* {name}: moved to src/{names[name]} - generated "
                       f"body removed so the hand-written definition links */")
            print(f"  {fname}: stubbed {name} "
                  f"({j - i + 1} lines removed)")
            stubbed += 1
            changed = True
            i = j + 1
        if apply and changed:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("\n".join(out) + "\n")

    if not stubbed:
        print("nothing to stub - no generated copies of the overrides")
    elif apply:
        print(f"\nstubbed {stubbed} generated definition(s)")
    else:
        print(f"\n{stubbed} would be stubbed; re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
