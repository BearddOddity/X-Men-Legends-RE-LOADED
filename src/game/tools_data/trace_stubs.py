#!/usr/bin/env python3
"""Make the unresolved stubs announce themselves, so you learn which ones run.

recomp_stubs_unresolved.c holds ~3,200 empty bodies for mid-function addresses
the disassembler never classified as functions. An empty body returns without
executing the rest of the real function, so prologue pushes are never popped
and callee-saved registers are never restored - that is the stack-drift and
esi-corruption class.

Guessing which of 3,200 runs is hopeless. This rewrites each body to report
itself, turning the question into a short list.

    py -3 tools_data/trace_stubs.py          # dry run
    py -3 tools_data/trace_stubs.py --apply  # instrument
    py -3 tools_data/trace_stubs.py --off    # restore empty bodies

Idempotent both ways. Pairs with recomp_stub_hit() in src/recomp_manual.c,
which histograms the hits and dumps them via atexit.
"""
import argparse
import os
import re
import sys

GAME_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUBS = os.path.join(GAME_DIR, "src", "recomp", "gen",
                     "recomp_stubs_unresolved.c")

# The generator emits the stub body as `g_esp += 4;` (simulating the `ret` it
# assumes is there). It did NOT always do that, and these patterns were written
# against the older `{ /* ... */ }` form; they then silently matched nothing and
# --apply reported "already in the requested state", which reads as success.
# `g_esp += 4;` is therefore optional here, and re-emitted on both paths.
#
# void sub_00202D87(void) { g_esp += 4; /* 0x00202D87: not detected */ }
EMPTY = re.compile(
    r"^void (sub_([0-9A-Fa-f]+))\(void\) \{ (?:g_esp \+= 4; )?/\* (0x[0-9A-Fa-f]+): not detected \*/ \}$")
# void sub_00202D87(void) { recomp_stub_hit(0x00202D87); g_esp += 4; /* not detected */ }
ARMED = re.compile(
    r"^void (sub_([0-9A-Fa-f]+))\(void\) \{ recomp_stub_hit\((0x[0-9A-Fa-f]+)\); (?:g_esp \+= 4; )?/\* not detected \*/ \}$")


def main() -> int:
    ap = argparse.ArgumentParser(prog="trace_stubs")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="instrument the stubs")
    g.add_argument("--off", action="store_true", help="restore empty bodies")
    args = ap.parse_args()

    if not os.path.exists(STUBS):
        sys.exit(f"not found: {STUBS}")

    lines = open(STUBS, encoding="utf-8").read().splitlines(keepends=True)
    out, armed, disarmed = [], 0, 0

    for line in lines:
        s = line.rstrip("\n")
        if args.off:
            m = ARMED.match(s)
            if m:
                out.append(f"void {m.group(1)}(void) {{ g_esp += 4; "
                           f"/* {m.group(3)}: not detected */ }}\n")
                disarmed += 1
                continue
        else:
            m = EMPTY.match(s)
            if m:
                # g_esp += 4 must survive arming: it is the stub's simulated
                # `ret`, not decoration. An earlier version of this tool dropped
                # it, silently changing behaviour in every one of 2,122 stubs.
                out.append(f"void {m.group(1)}(void) {{ "
                           f"recomp_stub_hit({m.group(3)}); g_esp += 4; "
                           f"/* not detected */ }}\n")
                armed += 1
                continue
        out.append(line)

    # The instrumented bodies call into recomp_manual.c; make sure it's declared.
    text = "".join(out)
    decl = "void recomp_stub_hit(uint32_t va);\n"
    if armed and decl not in text:
        text = text.replace('#include "recomp_funcs.h"\n',
                            '#include "recomp_funcs.h"\n\n' + decl, 1)
    elif disarmed:
        text = text.replace("\n" + decl, "", 1)

    n = armed or disarmed
    if not n:
        print("nothing to change (already in the requested state)")
        return 0
    verb = "disarm" if args.off else "arm"
    if not (args.apply or args.off):
        print(f"dry run: would {verb} {n} stub(s) in {STUBS}")
        print("re-run with --apply (or --off)")
        return 0

    open(STUBS, "w", encoding="utf-8", newline="").write(text)
    print(f"{verb}ed {n} stub(s) in {os.path.basename(STUBS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
