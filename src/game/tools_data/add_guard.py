"""
add_guard.py - insert the project's standard pointer-plausibility guard.

Why this exists
---------------
The commonest repair in this tree is the same shape every time: some register
holds an object pointer that is NULL or garbage, the original x86 dereferences
it unconditionally because on hardware it could not be garbage, and the fake TIB
at VA 0 turns the bad read into plausible nonsense instead of a clean fault.
The fix is always a heap-range check plus a skip.

That guard has been hand-written many times, and hand-writing it risks drifting
the wording, the range, or the marker comment - and `manual_edits.py` anchors
re-application after a regeneration on exactly those markers. This emits it
identically every time.

The range
---------
    ptr >= 0x00880000 && ptr < 0x04000000

The lower bound is technically wrong - the heap starts at 0x00F80000 - but it is
LOAD-BEARING. "Correcting" it once regressed the boot 61 -> 59 and was reverted.
Rule #10: do not tidy it. It is centralised here so it stays consistent.

Usage (from src/game/):
    # skip a whole function (cdecl, nothing pushed yet)
    py -3 tools_data/add_guard.py src/recomp/gen/recomp_0007.c \
        --after "loc_00123600: ;" --reg ecx --action return \
        --why "refcount release; releasing a reference to a non-existent object is a no-op"

    # jump past a block
    ... --action "goto loc_0013B085"

    # skip a call by faking a failed one
    ... --action eax0

    # add the range test to an existing null-only check
    py -3 tools_data/add_guard.py FILE --extend "if (CMP_EQ(ecx, ebp)) goto loc_X;" \
        --reg ecx --why "..."
"""
import argparse
import os
import re
import sys

MARK_TEXT = "Manual guard (not in original x86)"
MARK = "/* " + MARK_TEXT   # what manual_edits.py greps for
LO, HI = "0x00880000u", "0x04000000u"


def wrap(text, indent, width=74):
    out, line = [], indent
    for word in text.split():
        if len(line) + len(word) + 1 > width and line.strip() != indent.strip():
            out.append(line.rstrip())
            line = indent + word + " "
        else:
            line += word + " "
    if line.strip():
        out.append(line.rstrip())
    return out


def comment(why, reg):
    # The body must NOT contain the comment opener - it is added to the first
    # line below. Including it here produced `/* /* Manual guard`, which the
    # self-check at the bottom of this file now catches.
    body = (f"{MARK_TEXT}: {reg} can be NULL or garbage here. The original "
            f"dereferences it unconditionally, which is safe on hardware but "
            f"not in this build, and the fake TIB at VA 0 turns the bad read "
            f"into plausible nonsense rather than a clean fault. {why}")
    lines = wrap(body, "     * ")
    lines[0] = "    /* " + lines[0][len("     * "):]
    return lines + ["     * Range matches every other guard in this tree; see"
                    " tools_data/add_guard.py. */"]


def action_code(action):
    if action == "return":
        return "{ esp += 4; return; }"
    if action == "eax0":
        return "{ eax = 0; }"
    if action.startswith("goto "):
        return f"{{ {action}; }}"
    raise SystemExit(f"unknown --action {action!r}; use return, eax0, or 'goto LABEL'")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--after", help="insert the guard after this exact line")
    g.add_argument("--before", help="insert the guard before this exact line")
    g.add_argument("--extend", help="add the range test to this existing check")
    ap.add_argument("--in-func", metavar="sub_XXXXXXXX",
                    help="restrict the anchor search to this function. Generated "
                         "lines repeat constantly - `eax = MEM32(edi + 8);` "
                         "occurs 5 times in one file - so an anchor is usually "
                         "only unique within its function")
    ap.add_argument("--occurrence", type=int, metavar="N",
                    help="when the anchor still matches several lines inside the "
                         "function, take the Nth (1-based). Prefer --in-func "
                         "alone; reach for this only when the site genuinely "
                         "repeats, and say which one in --why")
    ap.add_argument("--reg", required=True, help="register holding the pointer")
    ap.add_argument("--action", default="return",
                    help="return | eax0 | 'goto loc_XXXXXXXX'")
    ap.add_argument("--why", required=True,
                    help="one line: why skipping is safe here")
    a = ap.parse_args(argv)

    if not os.path.exists(a.file):
        raise SystemExit(f"{a.file} not found")

    lines = open(a.file, encoding="utf-8", errors="ignore").read().split("\n")
    anchor = a.after if a.after is not None else (a.before or a.extend)

    # Narrow to one function first. Generated code repeats the same statement
    # constantly - `eax = MEM32(edi + 8);` occurs five times in one file - so a
    # bare anchor is almost never unique file-wide.
    lo, hi = 0, len(lines)
    if a.in_func:
        want = f"void {a.in_func}(void)"
        starts = [i for i, l in enumerate(lines)
                  if re.match(r"^void sub_[0-9A-Fa-f]+\(void\)$", l)]
        here = next((i for i in starts if lines[i] == want), None)
        if here is None:
            raise SystemExit(f"{a.in_func} not found in {a.file}")
        lo = here
        hi = next((i for i in starts if i > here), len(lines))

    hits = [i for i in range(lo, hi) if lines[i].strip() == anchor.strip()]
    if a.occurrence:
        if not 1 <= a.occurrence <= len(hits):
            raise SystemExit(f"--occurrence {a.occurrence} out of range: anchor "
                             f"matched {len(hits)} line(s)")
        hits = [hits[a.occurrence - 1]]
    if len(hits) != 1:
        raise SystemExit(
            f"anchor matched {len(hits)} lines, need exactly 1: {anchor!r}"
            + (f"\n  at lines: {[h + 1 for h in hits[:8]]}" if hits else "")
            + ("\n  narrow it with --in-func, or pick one with --occurrence N"
               if len(hits) > 1 else ""))
    i = hits[0]

    test = f"{a.reg} >= {LO} && {a.reg} < {HI}"

    if a.extend:
        # Fold the range test into the existing condition rather than adding a
        # second branch, so the control flow stays identical to the original.
        old = lines[i]
        if "if (" not in old:
            raise SystemExit("--extend needs a line containing an `if (`")
        head, rest = old.split("if (", 1)
        cond, tail = rest.rsplit(")", 1)
        lines[i] = f"{head}if ({cond} || !({test}))" + tail
        lines[i:i] = comment(a.why, a.reg)
        note = f"extended the existing check at {a.file}:{i + 1}"
    else:
        block = comment(a.why, a.reg) + [
            f"    if (!({test})) {action_code(a.action)}"]
        at = i + 1 if a.after is not None else i
        lines[at:at] = block
        note = f"guard inserted at {a.file}:{at + 1}"

    # Self-check. A malformed comment here gets pasted into generated C and is
    # only caught by the compiler much later, or worse, silently swallows the
    # code after it. This shipped once as `/* /* Manual guard`.
    text = "\n".join(lines)
    if "/* /*" in text or text.count("/*") != text.count("*/"):
        raise SystemExit("internal error: generated comment is malformed "
                         "(unbalanced or nested /*) - refusing to write")

    with open(a.file, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print(note)
    print("verify with a build plus two runs before recording (Rules #1, #7)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
