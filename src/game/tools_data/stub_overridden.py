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
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
GEN = os.path.join(GAME_DIR, "src", "recomp", "gen")
OVERRIDE_SOURCES = [
    os.path.join(GAME_DIR, "src", "recomp_manual.c"),
    os.path.join(GAME_DIR, "src", "d3d8_shim.c"),
    # Seeded functions count too. seed_missing_functions.py recompiles
    # addresses the sweep never found, and those same addresses already have
    # an empty "not detected" body in recomp_stubs_unresolved.c - so seeding
    # one without removing its stub is an immediate LNK2005.
    #
    # Worth being clear about why the empty stub is not harmless: in this
    # calling convention the callee pops the fake return address, so a stub
    # that does nothing pops nothing and leaks simulated stack on every call.
    # That is how _initterm's cursor was destroyed - sub_001A0189 was a stub,
    # its 16-byte purge never happened, and __SEH_epilog then restored
    # ebx/esi/edi from 16 bytes off.
    os.path.join(GAME_DIR, "src", "recomp", "gen", "recomp_seed.c"),
]

# Non-static only: a `static void sub_X` is file-local and cannot collide.
DEF_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*$")
GEN_DEF_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*$")

# recomp_stubs_unresolved.c writes its definitions on ONE line:
#     void sub_001A0189(void) { /* 0x001A0189: not detected */ }
# which GEN_DEF_RE cannot see, because it anchors on end-of-line after the
# signature. Missing this form meant seeding a previously-unresolved address
# still collided at link time.
ONELINE_DEF_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*\{.*\}\s*$")


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

    # recomp_seed.c is an override source, not a target - it must never have
    # its own bodies removed, or seeding would undo itself.
    override_files = {os.path.basename(p) for p in OVERRIDE_SOURCES}

    stubbed = 0
    for fname in sorted(f for f in os.listdir(GEN)
                        if f.startswith("recomp_") and f.endswith(".c")
                        and f not in override_files):
        path = os.path.join(GEN, fname)
        lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
        out, i, changed = [], 0, False
        while i < len(lines):
            one = ONELINE_DEF_RE.match(lines[i])
            if one and one.group(1) in names:
                name = one.group(1)
                out.append(f"/* {name}: moved to src/{names[name]} - generated "
                           f"body removed so the hand-written definition links */")
                print(f"  {fname}: stubbed {name} (1 line removed)")
                stubbed += 1
                changed = True
                i += 1
                continue
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
            # BACK UP BEFORE DESTROYING. gen/ is gitignored, so a stubbed body
            # is gone with no undo - git cannot help and the only recovery is a
            # full regeneration or scavenging an old snapshot tarball. That is
            # not hypothetical: on 2026-08-07 an override was tried, measured a
            # 4000 -> 36 kernel-call regression, and reverting it meant
            # extracting the original 235-line body out of a two-day-old
            # snapshot by hand.
            #
            # One .prestub copy per file, refreshed each run, kept beside the
            # source and gitignored with the rest of gen/. Restoring is a copy:
            #   cp recomp_0014.c.prestub recomp_0014.c
            bak = path + ".prestub"
            try:
                shutil.copy2(path, bak)
            except OSError as exc:
                sys.exit(f"refusing to stub {fname}: could not write the "
                         f"pre-stub backup ({exc}). Fix that first - stubbing "
                         f"without one is unrecoverable.")
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("\n".join(out) + "\n")
            print(f"  {fname}: backed up to {os.path.basename(bak)} before stubbing")

    if not stubbed:
        print("nothing to stub - no generated copies of the overrides")
    elif apply:
        print(f"\nstubbed {stubbed} generated definition(s)")
    else:
        print(f"\n{stubbed} would be stubbed; re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
