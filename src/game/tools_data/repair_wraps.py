"""
repair_wraps.py - close wrapping guards whose closing brace failed to re-apply.

Why this exists
---------------
After a full regeneration, `manual_edits.py apply` re-places most edits but some
wrapping guards fail: the guard opens with `if (cond) {`, and its matching close
sits further down past lines that are indistinguishable from generator output.
When the enclosed run has shifted, the opening can land while the close does
not, leaving a function with unbalanced braces that will not compile.

Hand-repairing those means reading each site, working out where the guarded
region ends, and inserting the brace - done three times in one migration, which
is exactly what Rule #4 says to stop doing.

Method
------
For every function `manual_edits.py check-braces` reports as unbalanced:

  1. Find the manual guard lines inside it that open a brace and are never
     closed (tracked by depth from the guard's own opening).
  2. Look the guard up in manual_edits.json by function and block text to get
     the `close` lines and the `wrapped` run that was recorded for it.
  3. Locate that run immediately after the opening, tolerating blank and label
     lines exactly as manual_edits.py does, and insert the recorded close after
     its last line.
  4. Re-check the function. If it is still unbalanced, revert the file and
     report - never leave a half-repaired tree.

Usage (from src/game/):
    py -3 tools_data/repair_wraps.py            # report what it would do
    py -3 tools_data/repair_wraps.py --apply
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
GEN = os.path.join(GAME_DIR, "src", "recomp", "gen")
STORE = os.path.join(HERE, "manual_edits.json")

sys.path.insert(0, HERE)
import manual_edits as me  # noqa: E402

# A manual block can open with `if (...) {`, `} else {`, or a bare
# `{ uint32_t _flag_cl = ...;`. Matching only `if`/`else` lines ending in `{`
# missed the bare-block form. The net-brace check in find_unclosed() is what
# actually decides, so this only needs to be permissive.
GUARD_OPEN = re.compile(r"^\s*(\}?\s*(if|else)\b|\{)")
LABEL = re.compile(r"^loc_[0-9A-Fa-f]+: ;\s*$")


def strip_comment(line):
    return re.sub(r"/\*.*?\*/", "", re.sub(r"/\*.*$", "", line))


def func_spans(lines):
    starts = [(i, m.group(1)) for i, l in enumerate(lines)
              if (m := me.FUNC_RE.match(l))]
    for n, (i, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        yield name, i, end


def delta(lines, lo, hi):
    d = 0
    for l in lines[lo:hi]:
        c = strip_comment(l)
        d += c.count("{") - c.count("}")
    return d


def match_run(lines, start, hi, run):
    """Index just past `run` if it starts at `start`, blanks/labels skipped."""
    i, k = start, 0
    while k < len(run) and i < hi:
        if (not lines[i].strip() or LABEL.match(lines[i].strip())) and run[k].strip():
            if not LABEL.match(run[k].strip()):
                i += 1
                continue
        if lines[i].rstrip() != run[k].rstrip():
            return None
        i += 1
        k += 1
    return i if k == len(run) else None


def find_unclosed(lines, lo, hi):
    """Manual guard openings inside [lo,hi) that are never closed."""
    out = []
    for i in range(lo, hi):
        if not GUARD_OPEN.match(lines[i]):
            continue
        c = strip_comment(lines[i])
        if c.count("{") - c.count("}") <= 0:
            continue
        # Only consider guards introduced by a manual marker just above.
        j = i - 1
        while j > lo and (lines[j].lstrip().startswith("*")
                          or lines[j].lstrip().startswith("/*")):
            if any(m in lines[j] for m in me.MARKERS):
                break
            j -= 1
        else:
            continue
        if not any(m in lines[j] for m in me.MARKERS):
            continue
        # Stop before the brace that ends the FUNCTION. When a guard's close is
        # missing, the depth only returns to zero at that final `}` - counting
        # it makes every unclosed guard look closed, which is why this found
        # nothing on its first run.
        body_end = hi
        for k in range(hi - 1, i, -1):
            if lines[k].strip() == "}":
                body_end = k
                break
        d, closed = 1, False
        for k in range(i + 1, body_end):
            c = strip_comment(lines[k])
            d += c.count("{") - c.count("}")
            if d == 0:
                closed = True
                break
        if not closed:
            out.append((j, i))
    return out


def find_duplicated_opening(lines, lo, hi):
    """Index of a generated block opening that the guard below it duplicates.

    Shape, seen in both functions left unbalanced by one migration:

        { uint32_t _icall_esp = g_esp;     <- generated, now orphaned
        /* Manual guard ... */
        if (cond) {                        <- guard, re-applied
        { uint32_t _icall_esp = g_esp;     <- the guard's own copy
        ...
        }                                  <- closes the guard, not the block

    The guard was recorded wrapping the whole block, so its stored text
    includes the opening. Re-applying re-inserts it while the generated one
    stays, leaving the function one brace short. Removing the orphan restores
    the intended nesting exactly.
    """
    for i in range(lo + 1, hi - 2):
        if not lines[i].lstrip().startswith("/*"):
            continue
        if not any(m in lines[i] for m in me.MARKERS):
            continue
        orphan = lines[i - 1]
        # The orphan is `{ uint32_t _icall_esp = g_esp;` - it OPENS a brace but
        # ends with a semicolon, so testing endswith("{") never matched and this
        # detector silently found nothing on its first two runs.
        if "{" not in orphan or "_icall_esp = g_esp" not in orphan:
            continue
        # The guard's own copy must appear within a few lines below its `if`.
        for k in range(i + 1, min(i + 20, hi)):
            if lines[k].rstrip() == orphan.rstrip():
                return i - 1
            if GUARD_OPEN.match(lines[k]):
                continue
    return None


GUARDED_STMT = re.compile(r"^(\s*)if \(.*?\)\s+(\S.*)$")


def dedup_guarded_statements(lines):
    """Remove originals left above a guard that was meant to replace them.

    Some guards do not wrap a block; they replace a single generated statement
    with a conditional version of itself:

        MEM32(esi + ecx * 4) = edi;        ->   if (ecx < 0x40u) MEM32(...) = edi;

    manual_edits.py records that as an insert-before-anchor edit, so re-applying
    it after a regeneration inserts the guarded line but leaves the original
    sitting directly above the guard comment. The unguarded statement then runs
    first and faults exactly as it did before the guard existed - silently, since
    the guard is visibly present in the source.

    Returns the indices removed.
    """
    removed = []
    for i in range(len(lines) - 1, 0, -1):
        m = GUARDED_STMT.match(lines[i])
        if not m:
            continue
        stmt = m.group(2).strip()
        # Walk back over the guard's comment to the line above it.
        j = i - 1
        saw_marker = False
        while j >= 0 and (lines[j].lstrip().startswith("*")
                          or lines[j].lstrip().startswith("/*")):
            if any(mk in lines[j] for mk in me.MARKERS):
                saw_marker = True
            j -= 1
        if not saw_marker or j < 0:
            continue
        if lines[j].strip() == stmt:
            removed.append(j)
    for j in removed:
        del lines[j]
    return removed


def main(argv):
    apply = "--apply" in argv
    edits = json.load(open(STORE, encoding="utf-8"))
    wraps = [e for e in edits if e.get("kind") == "wrap"]
    repaired = failed = 0

    for path in sorted(f for f in os.listdir(GEN) if f.startswith("recomp_")
                       and f.endswith(".c")):
        full = os.path.join(GEN, path)
        original = open(full, encoding="utf-8", errors="ignore").read()
        lines = original.splitlines()
        changed = False

        # Unguarded originals left above a replacing guard. Not a brace problem,
        # so this runs over every file rather than only unbalanced ones.
        if "--dedup-guarded" in argv:
            dups = dedup_guarded_statements(lines)
            if dups:
                for j in dups:
                    print(f"{path}:{j+1}: removing unguarded original left "
                          f"above its guard")
                repaired += len(dups)
                changed = apply

        # Recompute spans after every mutation. Deleting or inserting lines
        # invalidates every later index, and iterating a precomputed span list
        # while editing walked off the end of the file on the first run.
        while True:
            target = next(((n, a, b) for n, a, b in func_spans(lines)
                           if delta(lines, a, b) != 0), None)
            if target is None:
                break
            name, lo, hi = target
            print(f"{path} {name}: brace delta {delta(lines, lo, hi):+d}")
            dup = find_duplicated_opening(lines, lo, hi)
            if dup is not None:
                # A wrapping guard whose recorded block carries the block opening
                # it guards (`{ uint32_t _icall_esp = g_esp;`) leaves the
                # generated copy orphaned directly above the guard comment, so
                # the block's single `}` closes the guard instead and the
                # function ends up one brace short. Drop the orphan.
                print(f"  line {dup+1}: block opening duplicated by the guard "
                      f"below it - removing the orphaned generated copy")
                if apply:
                    del lines[dup]
                    changed = True
                    repaired += 1
                    continue
                repaired += 1
                break

            todo = find_unclosed(lines, lo, hi)
            if not todo:
                print(f"  no unclosed manual guard found - needs hand review")
                failed += 1
                break
            progressed = False
            for cstart, opening in reversed(todo):
                cand = [e for e in wraps if e.get("function") == name
                        and e["block"] and e["block"][0].strip()
                        == lines[cstart].strip()]
                if not cand:
                    if "--drop-unclosed" in argv:
                        # No recorded wrap to close it with, so remove the guard
                        # entirely - comment plus its `if (...) {` - restoring
                        # exactly the generated code. A dropped guard is a known
                        # loss that will resurface as its own crash, which is far
                        # better than a tree that will not compile or a hand
                        # repair that silently changes a generated line (that
                        # already happened once, removing a `this` setup).
                        print(f"  line {opening+1}: unclosed and unmatched - "
                              f"dropping the guard, back to generated code")
                        if apply:
                            del lines[cstart:opening + 1]
                            changed = True
                            progressed = True
                        repaired += 1
                        continue
                    print(f"  line {opening+1}: unclosed, but no recorded wrap "
                          f"matches - re-run with --drop-unclosed to remove it")
                    failed += 1
                    continue
                e = cand[0]
                end = match_run(lines, opening + 1, hi, e["wrapped"])
                if end is None:
                    print(f"  line {opening+1}: recorded enclosed run no longer "
                          f"matches - repair by hand")
                    failed += 1
                    continue
                print(f"  line {opening+1}: closing after the recorded run "
                      f"({len(e['wrapped'])} lines) with {len(e['close'])} line(s)")
                if apply:
                    lines[end:end] = e["close"]
                    changed = True
                    progressed = True
                repaired += 1

            if not progressed:
                break

        if apply and changed:
            # Never leave a half-repaired file: verify before writing.
            still = [n for n, a, b in func_spans(lines) if delta(lines, a, b)]
            if still:
                print(f"  {path}: still unbalanced after repair ({', '.join(still)})"
                      f" - leaving the file untouched")
                failed += len(still)
                continue
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write("\n".join(lines) + "\n")
            print(f"  {path}: repaired and verified balanced")

    if not repaired and not failed:
        print("no unbalanced functions - nothing to repair")
    elif not apply:
        print(f"\n{repaired} repairable, {failed} need hand work; "
              f"re-run with --apply")
    else:
        print(f"\nrepaired {repaired}, {failed} still need hand work")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
