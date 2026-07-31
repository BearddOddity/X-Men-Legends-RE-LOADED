"""
audit_kernel_ordinals.py - cross-check the kernel bridge's two ordinal tables.

src/kernel/kernel_bridge.c maintains the same ordinal in two places:

  * a HANDLER table   - `case 47: return bridge_HalReadSMCTrayState;`
  * an ARG-BYTES table - `case 47: return 24;  /* HalReadWritePCISpace(6) */`

Nothing enforces that they agree, and a disagreement is silent but severe: the
bridge pops the arg-table's byte count off the simulated stack regardless of
what the handler (or the caller) actually expects. A mismatch therefore shifts
esp on every call to that ordinal.

That was a real bug: ordinal 47's handler was the 2-argument
bridge_HalReadSMCTrayState while the arg table claimed 24 bytes (6 args), so
each call silently raised esp by 16 bytes. Downstream, the caller's next `push`
landed inside its own locals and clobbered a struct pointer it was passing.

This script flags two things:

  1. MISMATCH  - the arg table's comment names a different function than the
                 handler table maps for that ordinal. Strong bug signal.
  2. MISSING   - an ordinal has a handler but no arg-bytes entry (or vice
                 versa), so cleanup falls back to a default.

Name comparison is deliberately loose (case-insensitive, ignores the
`bridge_` prefix) because the two tables are written by hand and drift in
style, not just in content.

Usage (from src/game/):
    py -3 tools_data/audit_kernel_ordinals.py
Exit code is 1 if any MISMATCH is found, so it can gate a build.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
BRIDGE = os.path.join(REPO, "src", "kernel", "kernel_bridge.c")

# `case  47: return bridge_HalReadSMCTrayState;`
HANDLER_RE = re.compile(r"^\s*case\s+(\d+):\s*return\s+(bridge_\w+)\s*;", re.MULTILINE)
# `case  47: return 24;  /* HalReadWritePCISpace(6) */`
ARGS_RE = re.compile(
    r"^\s*case\s+(\d+):\s*return\s+(\d+)\s*;\s*(?:/\*\s*(\w+)\s*\((\d+|void)\))?",
    re.MULTILINE,
)


def norm(name):
    return re.sub(r"^bridge_", "", name or "").lower()


def main():
    if not os.path.exists(BRIDGE):
        print(f"kernel_bridge.c not found at {BRIDGE}")
        return 2
    src = open(BRIDGE, encoding="utf-8", errors="ignore").read()

    handlers = {int(o): fn for o, fn in HANDLER_RE.findall(src)}
    args = {}
    for o, nbytes, name, argc in ARGS_RE.findall(src):
        args[int(o)] = (int(nbytes), name or "", argc or "")

    print(f"handler entries : {len(handlers)}")
    print(f"arg-byte entries: {len(args)}\n")

    mismatches = []
    for ordinal, fn in sorted(handlers.items()):
        if ordinal not in args:
            continue
        nbytes, name, argc = args[ordinal]
        if not name:
            continue
        if norm(name) != norm(fn):
            mismatches.append((ordinal, fn, name, nbytes, argc))

    if mismatches:
        print("MISMATCH - handler and arg table name different functions.")
        print("The arg table drives stack cleanup, so each call shifts esp:\n")
        for ordinal, fn, name, nbytes, argc in mismatches:
            print(f"  ordinal {ordinal}:")
            print(f"    handler  : {fn}")
            print(f"    arg table: {nbytes} bytes  /* {name}({argc}) */")
        print()
    else:
        print("No name mismatches between the two tables.\n")

    missing_args = sorted(set(handlers) - set(args))
    if missing_args:
        print("MISSING arg-bytes entry (cleanup falls back to a default):")
        for ordinal in missing_args:
            print(f"  ordinal {ordinal}: handler {handlers[ordinal]}, no arg entry")
        print()

    # Sanity check: a declared argument count should match the byte count.
    inconsistent = [
        (o, nbytes, name, argc)
        for o, (nbytes, name, argc) in sorted(args.items())
        if argc and argc != "void" and int(argc) * 4 != nbytes
    ]
    if inconsistent:
        print("INCONSISTENT - byte count disagrees with the comment's arg count:")
        for o, nbytes, name, argc in inconsistent:
            print(f"  ordinal {o}: {nbytes} bytes but /* {name}({argc}) */ implies {int(argc)*4}")
        print()

    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
