#!/usr/bin/env python3
"""Restore hand-written guards that manual_edits.py could not re-place.

After a regeneration, manual_edits.py re-applies most edits by matching anchor
lines. Anchors that the new lifter output moved or reworded simply fail, and
those guards are silently lost - they were load-bearing, so the boot regresses
for reasons unrelated to whatever the regeneration was meant to fix.

This restores them from a pre-regeneration copy of gen/, but only when it can
*prove* the splice is safe:

  1. The new body must contain no `fall-through:` edge. If the lifter changed
     the function's control flow, the old body is stale and must not win.
  2. Every line of the new body must appear in the old body, in order. If the
     new body is a subsequence of the old, then the regeneration only *removed*
     lines here (the manual edits), so the old body is the new body plus those
     edits and nothing else has changed.

Anything failing either test is reported, not spliced.

    py -3 tools_data/restore_lost_guards.py --old <dir>            # dry run
    py -3 tools_data/restore_lost_guards.py --old <dir> --apply
    py -3 tools_data/restore_lost_guards.py --old <dir> --fn sub_X --apply
"""
import argparse
import os
import re
import sys

GAME_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = os.path.join(GAME_DIR, "src", "recomp", "gen")

MANUAL = re.compile(r"Manual (guard|fix|addition)")


def extract(path, fn):
    """Return (start_idx, end_idx, lines) for the function, or None."""
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines(True)
    except OSError:
        return None
    head = f"void {fn}(void)\n"
    try:
        i = lines.index(head)
    except ValueError:
        return None
    for j in range(i + 1, len(lines)):
        if lines[j].rstrip("\n") == "}":
            return i, j + 1, lines
    return None


def locate(lines, fn):
    """Return (start_idx, end_idx) for the function within an in-memory list."""
    head = f"void {fn}(void)\n"
    try:
        i = lines.index(head)
    except ValueError:
        return None
    for j in range(i + 1, len(lines)):
        if lines[j].rstrip("\n") == "}":
            return i, j + 1
    return None


def is_subsequence(small, big):
    """Every line of `small` appears in `big`, in order."""
    it = iter(big)
    return all(any(s == b for b in it) for s in small)


def main():
    ap = argparse.ArgumentParser(prog="restore_lost_guards")
    ap.add_argument("--old", required=True,
                    help="pre-regeneration gen/ directory")
    ap.add_argument("--fn", action="append",
                    help="restore only this function (repeatable)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.old):
        sys.exit(f"not a directory: {args.old}")

    # Which functions lost a guard? Any function whose OLD body carries a
    # manual-edit marker that the NEW body no longer has.
    targets = set(args.fn or [])
    if not targets:
        for name in sorted(os.listdir(args.old)):
            if not name.startswith("recomp_") or not name.endswith(".c"):
                continue
            oldp, newp = os.path.join(args.old, name), os.path.join(GEN, name)
            if not os.path.exists(newp):
                continue
            old_txt = open(oldp, encoding="utf-8", errors="replace").read()
            new_txt = open(newp, encoding="utf-8", errors="replace").read()
            for m in re.finditer(r"^void (sub_[0-9A-Fa-f]+)\(void\)$",
                                 old_txt, re.M):
                fn = m.group(1)
                o = extract(oldp, fn)
                n = extract(newp, fn)
                if not o or not n:
                    continue
                o_body = "".join(o[2][o[0]:o[1]])
                n_body = "".join(n[2][n[0]:n[1]])
                if MANUAL.search(o_body) and not MANUAL.search(n_body):
                    targets.add(fn)

    spliced, skipped = [], []
    for name in sorted(os.listdir(args.old)):
        if not name.startswith("recomp_") or not name.endswith(".c"):
            continue
        oldp, newp = os.path.join(args.old, name), os.path.join(GEN, name)
        if not os.path.exists(newp):
            continue
        # Read each file once and splice into that single live list. Using the
        # loop variable from the last iteration here was a bug: it went None
        # whenever the final target wasn't in this file, and the write blew up.
        new_lines = open(newp, encoding="utf-8", errors="replace").readlines()
        changed = False
        for fn in sorted(targets):
            o = extract(oldp, fn)
            if not o:
                continue
            n = locate(new_lines, fn)
            if not n:
                continue
            o_lines = o[2][o[0]:o[1]]
            n_lines = new_lines[n[0]:n[1]]
            o_body, n_body = "".join(o_lines), "".join(n_lines)
            if o_body == n_body:
                continue
            if "fall-through:" in n_body:
                skipped.append((fn, "new body has a fall-through edge"))
                continue
            if not is_subsequence([l for l in n_lines if l.strip()],
                                  [l for l in o_lines if l.strip()]):
                skipped.append((fn, "new body is not a subsequence of old - "
                                    "lifter output changed, needs hand review"))
                continue
            new_lines[n[0]:n[1]] = o_lines
            changed = True
            spliced.append((fn, name))
        if changed and args.apply:
            open(newp, "w", encoding="utf-8", newline="").writelines(new_lines)

    print(f"restorable: {len(spliced)}   needs hand review: {len(skipped)}")
    for fn, name in spliced:
        print(f"  + {fn:<18} ({name})")
    for fn, why in skipped:
        print(f"  ! {fn:<18} {why}")
    if not args.apply:
        print("\ndry run; re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
