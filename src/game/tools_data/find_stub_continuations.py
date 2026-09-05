"""find_stub_continuations.py - unresolved stubs that are reached by a TAIL JUMP.

The lifter emits `void sub_X(void) { g_esp += 4; }` for every address it failed
to identify as a function. That stub simulates a bare `ret`. When the address is
really the target of a `jmp` - a continuation fragment, the "found it" arm of a
search loop, a shared epilogue - the stub returns where the original would have
carried on, so the whole tail of the CALLER is skipped and whatever the caller
had pushed is never popped.

That is not a hypothetical. sub_0020B8C6 is `mov %edx,%ebx; jmp 0x20b88d`;
the stub returned instead, sub_0020B850's tail never ran, and esi escaped
holding a loop count of 4, which was passed on as a pointer (ledger #219).

This ranks the stubs worth checking. A stub reached by `sub_X(); return;` in a
generated body is in tail-jump position, so if the original at that address is a
jump rather than a return, the caller is being truncated. Confirm each against
the original before touching it:

    python3 oracle.py --disas 0x<caller>:<n>

and transcribe what is actually there. Do not guess - a wrong transcription is
worse than a stub, because it looks correct.
"""
import glob
import io
import os
import re
import sys

GEN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "src", "recomp", "gen")
STUBS = os.path.join(GEN, "recomp_stubs_unresolved.c")
TAILCALL = re.compile(r"(sub_[0-9A-F]{8})\(\);\s*return;")
FUNC = re.compile(r"^void (sub_[0-9A-F]{8})\(void\)$")


def main():
    if not os.path.exists(STUBS):
        sys.exit("no recomp_stubs_unresolved.c - nothing to scan")
    stub_names = set(re.findall(r"^void (sub_[0-9A-F]{8})\(void\) \{ g_esp",
                                io.open(STUBS, encoding="utf-8",
                                        errors="replace").read(), re.M))
    print("unresolved stubs: %d" % len(stub_names), file=sys.stderr)

    hits = {}
    for path in sorted(glob.glob(os.path.join(GEN, "*.c"))):
        if path.endswith("recomp_stubs_unresolved.c"):
            continue
        cur = None
        for n, line in enumerate(io.open(path, encoding="utf-8",
                                         errors="replace"), 1):
            m = FUNC.match(line.rstrip("\n"))
            if m:
                cur = m.group(1)
                continue
            for callee in TAILCALL.findall(line):
                if callee in stub_names:
                    hits.setdefault(callee, []).append(
                        (os.path.basename(path), n, cur))

    print("stubs reached by a tail jump: %d\n" % len(hits), file=sys.stderr)
    for callee in sorted(hits, key=lambda c: (-len(hits[c]), c)):
        sites = hits[callee]
        print("%s  (%d caller site%s)" % (callee, len(sites),
                                          "" if len(sites) == 1 else "s"))
        for f, n, owner in sites:
            print("    %s:%d  in %s" % (f, n, owner))
    return 0


if __name__ == "__main__":
    sys.exit(main())
