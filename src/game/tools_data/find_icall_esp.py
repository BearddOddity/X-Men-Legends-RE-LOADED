#!/usr/bin/env python3
"""find_icall_esp.py - find (and optionally fix) the ledger #145 defect.

The defect
----------
The lifter emits an indirect call as::

    { uint32_t _icall_esp = g_esp;
    PUSH32(esp, ebx);            <- callee-saved REGISTER SAVE
    PUSH32(esp, esi);
    edi = ecx;
    eax = MEM32(edi);
    uint32_t _icall_target = MEM32(eax + 0x50);
    PUSH32(esp, 0); RECOMP_ICALL_SAFE(_icall_target, _icall_esp); }

`RECOMP_ICALL_SAFE` restores ``g_esp = _icall_esp`` when the call fails. If the
capture happened BEFORE the callee-saved pushes, that rollback discards them,
and the function's epilogue then POPs the wrong stack slots and hands its caller
garbage in ebx/esi/edi. Ledger #145 diagnosed it; #130 flagged a second site as
latent, and that latent site is what caused wall 42 three weeks later.

The generator's `_fixup_icall_esp_save` cannot tell an argument push from a
register save, which is why these keep appearing.

The rule this script uses
-------------------------
A push is a REGISTER SAVE (not an argument) when both hold:

* it pushes ebx, ebp, esi or edi - the x86 callee-saved set; and
* the same register is POPped later in the same function, which is what an
  epilogue does and what an argument push never does.

Only when EVERY push between the capture and the call satisfies that is the
capture moved. Anything mixed is reported and left alone for a human, because
moving a capture past a genuine argument push would break the rollback it
exists to perform.

Usage
-----
    py -3 tools_data/find_icall_esp.py            # report only
    py -3 tools_data/find_icall_esp.py --apply    # rewrite in place

Reporting is the default: this edits generated code that carries hand-written
fixes, so look before you leap.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(os.path.dirname(HERE), "src", "recomp", "gen")

CALLEE_SAVED = ("ebx", "ebp", "esi", "edi")

FUNC_RE = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)")
OPEN_RE = re.compile(r"^\s*\{ uint32_t _icall_esp = g_esp;\s*$")
PUSH_RE = re.compile(r"^\s*PUSH32\(esp, (\w+)\);\s*$")
POP_RE = re.compile(r"^\s*POP32\(esp, (\w+)\);")
TARGET_RE = re.compile(r"^\s*uint32_t _icall_target = ")


def function_spans(lines):
    """(name, start, end) for every generated function body."""
    spans, cur, start = [], None, None
    for i, line in enumerate(lines):
        m = FUNC_RE.match(line)
        if m:
            if cur:
                spans.append((cur, start, i))
            cur, start = m.group(1), i
    if cur:
        spans.append((cur, start, len(lines)))
    return spans


def scan_function(lines, start, end):
    """Yield one dict per PROLOGUE capture inside this function.

    Only the function's own prologue is considered. Every confirmed instance of
    this defect - ledger #145's sub_001EA770, and sub_001EA600, sub_001EA6B0,
    sub_001EA8E0 and sub_002093F0 found since - has the capture in the prologue,
    where the pushes can only be the register saves the epilogue pops.

    Deeper captures inside a function body are NOT reported. There, a
    `PUSH32(esp, esi)` is far more likely to be an argument that happens to use
    a callee-saved register, and "the register is popped somewhere in this
    function" is not enough to tell the two apart. Moving a capture past a real
    argument push would break the rollback it exists to perform, so those are
    out of scope for a mechanical pass.
    """
    popped = {m.group(1) for m in
              (POP_RE.match(l) for l in lines[start:end]) if m}
    # The prologue is the first label and the lines up to the first capture.
    limit = min(start + 12, end)
    out = []
    for i in range(start, limit):
        if not OPEN_RE.match(lines[i]):
            continue
        pushes, j = [], i + 1
        while j < end and not TARGET_RE.match(lines[j]):
            m = PUSH_RE.match(lines[j])
            if m:
                pushes.append((j, m.group(1)))
            j += 1
        if j >= end or not pushes:
            continue                      # no pushes before the call: fine
        saves = [(k, r) for k, r in pushes
                 if r in CALLEE_SAVED and r in popped]
        args = [(k, r) for k, r in pushes if (k, r) not in saves]
        out.append({"open": i, "target": j, "saves": saves, "args": args})
        break                             # one prologue capture per function
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the files (default is report only)")
    ap.add_argument("--gen-dir", default=GEN)
    args = ap.parse_args(argv)

    clean = mixed = 0
    per_file = {}
    for name in sorted(os.listdir(args.gen_dir)):
        if not name.endswith(".c"):
            continue
        path = os.path.join(args.gen_dir, name)
        with open(path, encoding="utf-8", errors="ignore", newline="") as fh:
            lines = fh.read().split("\n")
        hits = []
        for fn, s, e in function_spans(lines):
            for h in scan_function(lines, s, e):
                h["fn"] = fn
                hits.append(h)
        if not hits:
            continue
        per_file[name] = (path, lines, hits)
        for h in hits:
            if not h["saves"]:
                continue          # arguments only: capture placement is correct
            if h["args"]:
                mixed += 1
                print("  MIXED  %-14s %s:%d  saves=%s args=%s" %
                      (h["fn"], name, h["open"] + 1,
                       [r for _, r in h["saves"]], [r for _, r in h["args"]]))
            else:
                clean += 1
                print("  DEFECT %-14s %s:%d  saves=%s" %
                      (h["fn"], name, h["open"] + 1,
                       [r for _, r in h["saves"]]))

    print()
    print("%d capture(s) sit before callee-saved pushes ONLY - safe to move" % clean)
    print("%d capture(s) mix register saves with argument pushes - left for a human" % mixed)

    if not args.apply:
        print("\nreport only; pass --apply to rewrite")
        return 0

    moved = 0
    for name, (path, lines, hits) in per_file.items():
        # rewrite back-to-front so earlier indices stay valid
        for h in sorted(hits, key=lambda x: -x["open"]):
            if h["args"] or not h["saves"]:
                continue      # mixed, or arguments only (already correct)
            lines[h["open"]] = "    {"
            lines.insert(h["target"],
                         "    /* _icall_esp capture moved here by "
                         "find_icall_esp.py: it sat before this call's "
                         "callee-saved pushes, so a failed ICALL rolled the "
                         "stack back past them. See ledger #145. */\n"
                         "    uint32_t _icall_esp = g_esp;")
            moved += 1
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("\n".join(lines))
    print("\nmoved %d capture(s)" % moved)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
