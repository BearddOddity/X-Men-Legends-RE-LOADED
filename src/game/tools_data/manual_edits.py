"""
manual_edits.py - extract and re-apply the hand-written edits in gen/*.c.

Why this exists
---------------
`src/recomp/gen/*.c` is generated, but it is no longer *purely* generated: it
carries dozens of hand-written guards and fixes accumulated while debugging.
Those files are gitignored (they are mechanically derived from a copyrighted
XBE), so regenerating the pipeline silently destroys all of them.

That made regeneration effectively forbidden, which is bad - the pipeline's own
improvements (a better lifter, extra seeded functions) can only land via a
regeneration. This tool breaks that deadlock: extract the manual edits first,
regenerate, then re-apply them.

What counts as a manual edit
----------------------------
Every hand edit in gen/ is required to carry one of these markers on its first
line, which is what makes this tool possible:

    /* Manual guard (not in original x86): ...
    /* Manual fix (not in original x86): ...
    /* sub_XXXXXXXX: moved to src/d3d8_shim.c ...

Anything else in gen/ is assumed to be generator output.

Anchoring
---------
An edit is recorded as (enclosing function, the comment+code block, the exact
source line that FOLLOWS it). Re-applying means: find that function, find that
following line inside it, insert the block before it. Anchoring to the next line
rather than to a line number survives the generator shifting code around, which
line numbers do not.

Re-application is refused unless every edit is placed, so a partial restore can
never be mistaken for a complete one.

Usage (from src/game/):
    py -3 tools_data/manual_edits.py extract [-o edits.json]
    py -3 tools_data/manual_edits.py apply   [-i edits.json] [--gen-dir DIR]
    py -3 tools_data/manual_edits.py verify  [-i edits.json] [--gen-dir DIR]
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
DEFAULT_GEN = os.path.join(GAME_DIR, "src", "recomp", "gen")
DEFAULT_STORE = os.path.join(HERE, "manual_edits.json")

FUNC_RE = re.compile(r"^void (sub_[0-9A-F]+)\(void\)$")

MARKERS = (
    "/* Manual guard (not in original x86)",
    "/* Manual fix (not in original x86)",
    "/* Manual addition (not in original x86)",
)
# Single-line marker used where a stub was replaced by a native implementation.
MOVED_RE = re.compile(r"^/\* (sub_[0-9A-F]+): moved to src/\S+ .*\*/$")


def function_at(lines, idx):
    """Name of the function containing line `idx`, or None at file scope."""
    for i in range(idx, -1, -1):
        m = FUNC_RE.match(lines[i])
        if m:
            return m.group(1)
    return None


def extract_file(path):
    """Return the manual-edit records in one generated file."""
    lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
    edits = []
    i = 0
    while i < len(lines):
        line = lines[i]

        m = MOVED_RE.match(line.strip())
        if m:
            edits.append({
                "file": os.path.basename(path),
                "kind": "replace_line",
                "function": None,
                "block": [line],
                "match": rf"^void {m.group(1)}\(void\) \{{ /\*.*\*/ \}}$",
                "anchor": None,
            })
            i += 1
            continue

        if any(line.lstrip().startswith(mk) for mk in MARKERS):
            start = i
            # Consume the comment through its closing */ ...
            while i < len(lines) and "*/" not in lines[i]:
                i += 1
            i += 1
            # ...then the code lines it introduces, up to the first line that
            # already exists in generated output. Generated statements are what
            # we anchor on, so stop at the first line that is not obviously part
            # of the inserted block.
            while i < len(lines) and lines[i].strip() and _is_inserted(lines[i]):
                i += 1
            block = lines[start:i]
            # Anchor on the first NON-BLANK line after the block. A blank line
            # is useless as an anchor (not unique, and falsy), and the blank
            # separating a guard from the next label is cosmetic - re-inserting
            # just before that next real line reproduces the same behaviour.
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            anchor = lines[j] if j < len(lines) else None
            # A block ending in `{` WRAPS following generated code rather than
            # simply preceding it: its closing `}` sits further down, past lines
            # that are indistinguishable from generator output. Re-inserting
            # only the opening would leave unbalanced braces, so find the
            # matching close by brace depth and record a SECOND anchor for it.
            # Re-applying then means: insert the opening before `anchor`, and
            # insert the closing before `end_anchor`.
            if block and block[-1].rstrip().endswith("{"):
                depth = 0
                for l in block:
                    depth += l.count("{") - l.count("}")
                k, close_at = i, None
                while k < len(lines):
                    depth += lines[k].count("{") - lines[k].count("}")
                    if depth == 0:
                        close_at = k
                        break
                    k += 1
                if close_at is None:
                    edits.append({
                        "file": os.path.basename(path), "kind": "manual_review",
                        "function": function_at(lines, start), "block": block,
                        "match": None, "anchor": anchor, "end_anchor": None,
                        "why": "wrapping guard whose closing brace was not found",
                    })
                    continue
                # Record the wrapped region by CONTENT, not by an end-anchor
                # line. Guards frequently sit back to back, and an anchor-based
                # close lands in the wrong place when the following lines belong
                # to a neighbouring guard - producing misnested or unbalanced
                # braces. The enclosed lines are generator output, so they exist
                # verbatim in a fresh tree and can be located exactly.
                wrapped = [l for l in lines[i:close_at]
                           if not any(l.lstrip().startswith(mk) for mk in MARKERS)]
                edits.append({
                    "file": os.path.basename(path), "kind": "wrap",
                    "function": function_at(lines, start), "block": block,
                    "close": [lines[close_at]], "match": None,
                    "anchor": anchor,
                    "wrapped": wrapped,
                })
                continue

            edits.append({
                "file": os.path.basename(path),
                "kind": "insert_before",
                "function": function_at(lines, start),
                "block": block,
                "match": None,
                "anchor": anchor,
            })
            continue
        i += 1
    return edits


def _next_generated(lines, i):
    """First index at/after `i` that is generator output.

    Skips blank lines and any adjacent manual-edit block (its comment plus the
    guard lines it introduces). Used to pick an anchor that exists in a freshly
    regenerated tree, where no manual edits are present yet.
    """
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        if any(lines[i].lstrip().startswith(mk) for mk in MARKERS):
            while i < len(lines) and "*/" not in lines[i]:
                i += 1
            i += 1
            while i < len(lines) and lines[i].strip() and _is_inserted(lines[i]):
                i += 1
            continue
        if MOVED_RE.match(lines[i].strip()):
            i += 1
            continue
        return i
    return i


def _is_inserted(line):
    """Heuristic: does this line belong to a hand-written block?

    Hand edits are guards and redirects - conditionals, gotos, direct calls, and
    assignments to the frame-pointer bridge. A plain generated statement (a
    register assignment, MEM32 store, PUSH32/POP32) ends the block.
    """
    s = line.strip()
    if s.startswith(("if (", "goto ", "g_seh_ebp = ", "}", "{")):
        return True
    if re.match(r"^sub_[0-9A-F]+\(\); return;$", s):
        return True
    return False


def audit_markers(gen_dir):
    """Every manual-edit marker in gen/ must be one this tool knows about.

    REGRESSION GUARD. If a future edit introduces a new marker wording, the
    extractor would silently treat it as generator output and drop that edit on
    regeneration - losing work with no error. Fail loudly instead.
    """
    seen = {}
    known = set(MARKERS)
    pat = re.compile(r"/\* Manual [a-z]+ \(not in original x86\)")
    for path in sorted(glob.glob(os.path.join(gen_dir, "recomp_*.c"))):
        for line in open(path, encoding="utf-8", errors="ignore"):
            m = pat.search(line)
            if m:
                seen[m.group(0)] = seen.get(m.group(0), 0) + 1
    unknown = {k: v for k, v in seen.items() if k not in known}
    return seen, unknown


def cmd_extract(args):
    seen, unknown = audit_markers(args.gen_dir)
    if unknown:
        print("ERROR: unrecognised manual-edit marker(s) in gen/.")
        print("Add them to MARKERS or these edits will be silently lost:")
        for k, v in sorted(unknown.items()):
            print(f"  {k}   ({v} occurrences)")
        return 2
    print("marker audit: " + ", ".join(f"{k.split('Manual ')[1].split(' ')[0]}={v}"
                                       for k, v in sorted(seen.items())))

    edits = []
    for path in sorted(glob.glob(os.path.join(args.gen_dir, "recomp_*.c"))):
        edits.extend(extract_file(path))
    by_kind = {}
    for e in edits:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    with open(args.store, "w", encoding="utf-8") as fh:
        json.dump(edits, fh, indent=1)
    print(f"extracted {len(edits)} manual edits -> {args.store}")
    for k, v in sorted(by_kind.items()):
        print(f"  {k}: {v}")
    unanchored = [e for e in edits if e["kind"] == "insert_before" and not e["anchor"]]
    if unanchored:
        print(f"  WARNING: {len(unanchored)} edits have no anchor line and cannot be re-applied")
    return 0


def cmd_apply(args, dry_run=False):
    edits = json.load(open(args.store, encoding="utf-8"))
    placed, failed = 0, []
    pending = {}

    by_file = {}
    for e in edits:
        by_file.setdefault(e["file"], []).append(e)

    for fname, file_edits in sorted(by_file.items()):
        path = os.path.join(args.gen_dir, fname)
        if not os.path.exists(path):
            failed.extend((e, "file missing") for e in file_edits)
            continue
        lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")

        for e in file_edits:
            if e["kind"] == "replace_line":
                pat = re.compile(e["match"])
                hit = next((i for i, l in enumerate(lines) if pat.match(l.strip())), None)
                if hit is None:
                    # Already applied is success, not failure.
                    if e["block"][0] in lines:
                        placed += 1
                    else:
                        failed.append((e, "no line matched"))
                    continue
                lines[hit] = e["block"][0]
                placed += 1
                continue

            if e["kind"] == "manual_review":
                failed.append((e, e.get("why") or "needs manual re-application"))
                continue

            # insert_before / wrap
            if not e["anchor"]:
                failed.append((e, "no anchor recorded"))
                continue
            lo, hi = _function_span(lines, e["function"])
            if lo is None:
                failed.append((e, f"function {e['function']} not found"))
                continue
            hit = _find_anchor(lines, lo, hi, e["anchor"])
            if hit is None:
                failed.append((e, "anchor line not found in function"))
                continue
            # Idempotency must be POSITIONAL, not "does this text appear
            # anywhere". Many guards are worded identically (all 50 dropped
            # tail-call fixes share one comment), so a whole-file text search
            # makes every duplicate look already-applied and silently drops it.
            n = len(e["block"])
            if hit >= n and lines[hit - n:hit] == e["block"]:
                placed += 1  # genuinely already applied at this site
                continue

            if e["kind"] == "wrap":
                wrapped = e.get("wrapped")
                if not wrapped:
                    failed.append((e, "wrapping guard recorded no enclosed lines"))
                    continue
                # The enclosed lines must start exactly at the anchor, since the
                # anchor IS the first wrapped line. Verify rather than search, so
                # a mismatch is reported instead of silently wrapping the wrong
                # region.
                end = _match_run(lines, hit, hi, wrapped)
                if end is None:
                    failed.append((e, "enclosed lines not found at anchor"))
                    continue
                # Insert the closing brace first: doing the opening first would
                # shift every later index by len(block).
                lines[end:end] = e["close"]
                lines[hit:hit] = e["block"]
                placed += 1
                continue

            lines[hit:hit] = e["block"]
            placed += 1

        pending[path] = lines

    # Write nothing unless EVERY edit placed. A partially-restored tree compiles
    # but is silently missing guards, which is worse than an obvious failure.
    if not failed and not dry_run:
        for path, lines in pending.items():
            open(path, "w", encoding="utf-8").write("\n".join(lines))

    total = len(edits)
    verb = "would place" if dry_run else "placed"
    print(f"{verb} {placed}/{total} manual edits")
    if failed:
        print(f"\nFAILED to place {len(failed)}:")
        for e, why in failed[:20]:
            print(f"  {e['file']} {e.get('function') or ''}: {why}")
        if len(failed) > 20:
            print(f"  ... and {len(failed)-20} more")
        print("\nRefusing to treat this as a successful restore.")
        return 1
    return 0


_ICALL_TARGET_RE = re.compile(
    r"uint32_t _icall_target = (?P<t>.+?);\s*PUSH32\(esp, 0\);\s*"
    r"RECOMP_ICALL_SAFE\(_icall_target,")


def _normalise(line):
    """Canonical form of a generated line, for anchor matching across versions.

    The lifter now captures an indirect call's target into a temp before the
    dummy push (so an esp-relative operand is read with the pre-push esp).
    That rewrote

        PUSH32(esp, 0); RECOMP_ICALL_SAFE(MEM32(edx + 0x50), _icall_esp);
    into
        uint32_t _icall_target = MEM32(edx + 0x50); PUSH32(esp, 0);
        RECOMP_ICALL_SAFE(_icall_target, _icall_esp);

    Both spellings normalise to the same string so an edit anchored to either
    still matches.
    """
    s = " ".join(line.split())
    m = _ICALL_TARGET_RE.search(s)
    if m:
        s = _ICALL_TARGET_RE.sub(f"RECOMP_ICALL_SAFE({m.group('t')},", s)
    return s


def _match_run(lines, start, hi, run):
    """End index of `run` if it appears at `start` (blank lines tolerated).

    Returns the index just past the last matched line, or None. Blanks are
    skipped on the file side because the extractor drops them from the recorded
    run, and the generator's blank-line placement is cosmetic.
    """
    i, k = start, 0
    while k < len(run) and i < hi:
        if not lines[i].strip() and run[k].strip():
            i += 1
            continue
        # Compare normalised: the generator's spelling of a line can change
        # between versions (e.g. the _icall_target rewrite), and an enclosed
        # line that merely got re-spelled is still the same statement.
        if lines[i] != run[k] and _normalise(lines[i]) != _normalise(run[k]):
            return None
        i += 1
        k += 1
    return i if k == len(run) else None


def _find_anchor(lines, lo, hi, anchor):
    """Index of `anchor` within [lo, hi), exact first then normalised."""
    hit = next((i for i in range(lo, hi) if lines[i] == anchor), None)
    if hit is not None:
        return hit
    want = _normalise(anchor)
    return next((i for i in range(lo, hi) if _normalise(lines[i]) == want), None)


def _function_span(lines, name):
    if name is None:
        return 0, len(lines)
    start = next((i for i, l in enumerate(lines) if FUNC_RE.match(l) and FUNC_RE.match(l).group(1) == name), None)
    if start is None:
        return None, None
    end = next((i for i in range(start + 1, len(lines)) if FUNC_RE.match(lines[i])), len(lines))
    return start, end


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["extract", "apply", "verify"])
    ap.add_argument("-o", "--store", default=DEFAULT_STORE)
    ap.add_argument("-i", "--input", dest="store_in")
    ap.add_argument("--gen-dir", default=DEFAULT_GEN)
    args = ap.parse_args()
    if args.store_in:
        args.store = args.store_in

    if args.command == "extract":
        return cmd_extract(args)
    if args.command == "apply":
        return cmd_apply(args, dry_run=False)
    return cmd_apply(args, dry_run=True)


if __name__ == "__main__":
    sys.exit(main())
