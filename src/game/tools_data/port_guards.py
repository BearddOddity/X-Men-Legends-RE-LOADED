#!/usr/bin/env python3
"""port_guards.py - carry hand-written guards from a pre-regeneration backup
into a freshly generated file.

Why this exists
---------------
A regeneration changed the lifter's output enough that manual_edits.py could no
longer anchor 53 of the tree's 66 guards, and it reported them as "already
present" when they were not. The guards are real defensive code built over many
sessions, and they are concentrated in exactly the files the boot runs through.

This ports them by a different anchor. Instead of the stored anchor line, it
uses the first generated line AFTER the guard - that line is lifter output, so
it exists in both trees - and requires that line to be UNIQUE within its
function in the new file. Anything ambiguous or missing is reported, never
guessed at.

    python3 tools_data/port_guards.py OLD.c NEW.c            # dry run
    python3 tools_data/port_guards.py OLD.c NEW.c --apply

Self-check:  python3 tools_data/port_guards.py --selftest
"""
import argparse
import io
import re
import sys

FUNC = re.compile(r"^void (sub_[0-9A-Fa-f]+)\(void\)$")
START = re.compile(r"^\s*/\* Manual (guard|fix) \(not in original x86\)")
LABEL = re.compile(r"^loc_[0-9A-Fa-f]+: ;")


def split_functions(lines):
    """name -> (start, end) over the given lines."""
    out, cur, start = {}, None, None
    for i, l in enumerate(lines):
        m = FUNC.match(l)
        if m:
            if cur:
                out[cur] = (start, i)
            cur, start = m.group(1), i
    if cur:
        out[cur] = (start, len(lines))
    return out


def extract(lines):
    """Every guard block, with its enclosing function and following anchor."""
    cur, guards, i = None, [], 0
    while i < len(lines):
        m = FUNC.match(lines[i])
        if m:
            cur = m.group(1)
        if START.match(lines[i]):
            start = i
            j = i
            while j < len(lines) and "*/" not in lines[j]:
                j += 1
            j += 1
            end, depth = j, 0
            while end < len(lines):
                l = lines[end]
                if l.strip() == "" or LABEL.match(l):
                    break
                depth += l.count("{") - l.count("}")
                end += 1
                if depth <= 0:
                    # A guard written as `if (...) { ... }` is often followed by
                    # its own `else ...;` line. Stopping at the closing brace
                    # would split the statement and leave the else stranded, and
                    # would then anchor the guard to that else.
                    if end < len(lines) and lines[end].lstrip().startswith("else"):
                        end += 1
                    break
            # The anchor must be GENERATED code, so skip blank lines and any
            # further guard blocks stacked immediately after this one. In the
            # old tree guards are often back to back, and taking the next line
            # blindly anchors a guard to another guard's comment - which then
            # cannot be found in a freshly generated file.
            after = end
            while after < len(lines):
                if lines[after].strip() == "":
                    after += 1
                    continue
                if START.match(lines[after]):
                    k = after
                    while k < len(lines) and "*/" not in lines[k]:
                        k += 1
                    k += 1
                    d = 0
                    while k < len(lines):
                        l2 = lines[k]
                        if l2.strip() == "" or LABEL.match(l2):
                            break
                        d += l2.count("{") - l2.count("}")
                        k += 1
                        if d <= 0:
                            break
                    after = k
                    continue
                break
            guards.append({"func": cur, "text": lines[start:end],
                           "anchor": lines[after] if after < len(lines) else None})
            i = end
            continue
        i += 1
    return guards


def port(old_lines, new_lines):
    """Return (new lines, placed, skipped[reasons])."""
    guards = extract(old_lines)
    funcs = split_functions(new_lines)
    out = list(new_lines)
    placed, skipped = [], []

    # Insert from the bottom up so earlier indices stay valid.
    plan = []
    for g in guards:
        # Skip the save-point fixes. Their only code is the _icall_esp
        # declaration, and the regenerated lifter already emits that in the
        # right place (ledger #197: 759 defective sites -> 0). Porting them
        # adds a second declaration in the same scope: C2374 redefinition.
        code = [l for l in g["text"]
                if l.strip() and not l.lstrip().startswith(("/*", "*"))]
        if code and all("_icall_esp = g_esp;" in l for l in code):
            skipped.append((g, "obsolete save-point fix; the lifter now does this"))
            continue
        if g["anchor"] is None or not g["anchor"].strip():
            skipped.append((g, "guard has no following code line"))
            continue
        if g["func"] not in funcs:
            skipped.append((g, f"{g['func']} not in the new file"))
            continue
        s, e = funcs[g["func"]]
        hits = [i for i in range(s, e) if out[i] == g["anchor"]]
        if not hits:
            skipped.append((g, "anchor line not present in the new function"))
            continue
        if len(hits) > 1:
            skipped.append((g, f"anchor appears {len(hits)}x in the function"))
            continue
        if any(START.match(x) for x in out[max(s, hits[0] - 25):hits[0]]):
            skipped.append((g, "a guard is already present just above"))
            continue
        plan.append((hits[0], g))

    for at, g in sorted(plan, key=lambda p: -p[0]):
        out[at:at] = g["text"]
        placed.append(g)
    return out, placed, skipped


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("old", nargs="?")
    ap.add_argument("new", nargs="?")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.old and a.new):
        ap.error("give OLD.c and NEW.c, or --selftest")

    old_lines = io.open(a.old, encoding="utf-8", errors="replace").read().split("\n")
    new_lines = io.open(a.new, encoding="utf-8", errors="replace").read().split("\n")
    out, placed, skipped = port(old_lines, new_lines)

    print(f"{len(extract(old_lines))} guard(s) in the backup")
    print(f"  placed  {len(placed)}")
    print(f"  skipped {len(skipped)}\n")
    for g, why in skipped:
        print(f"  SKIP {g['func']:<16} {why}")
        print(f"       anchor: {(g['anchor'] or '')[:80]}")
    if a.apply:
        io.open(a.new, "w", encoding="utf-8", newline="\n").write("\n".join(out))
        print(f"\nwrote {a.new}")
    else:
        print("\ndry run; re-run with --apply")
    return 0


def selftest():
    old = [
        "void sub_00000001(void)",
        "{",
        "    /* Manual guard (not in original x86): check it. */",
        "    if (!(esi >= 1u)) { esi = 0; }",
        "    eax = MEM32(esi + 4);",
        "}",
    ]
    new = [
        "void sub_00000001(void)",
        "{",
        "    eax = MEM32(esi + 4);",
        "}",
    ]
    out, placed, skipped = port(old, new)
    assert len(placed) == 1, (len(placed), skipped)
    assert not skipped, skipped
    assert "    if (!(esi >= 1u)) { esi = 0; }" in out
    assert out.index("    if (!(esi >= 1u)) { esi = 0; }") < \
        out.index("    eax = MEM32(esi + 4);"), "guard must precede its anchor"
    # idempotent: porting again must not double it
    out2, placed2, _ = port(old, out)
    assert placed2 == [], "second pass must place nothing"
    # ambiguous anchor is refused, not guessed
    amb = ["void sub_00000001(void)", "{", "    eax = MEM32(esi + 4);",
           "    eax = MEM32(esi + 4);", "}"]
    _, p3, s3 = port(old, amb)
    assert p3 == [] and s3 and "2x" in s3[0][1], (p3, s3)
    print("selftest ok - places before anchor, idempotent, refuses ambiguity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
