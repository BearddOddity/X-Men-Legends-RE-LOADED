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
            while True:
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
                # Where does the next real line sit, and is it another guard?
                j = i
                while j < len(lines) and not lines[j].strip():
                    j += 1
                nxt_is_manual = (j < len(lines) and
                                 any(lines[j].lstrip().startswith(mk)
                                     for mk in MARKERS))
                last = next((l for l in reversed(lines[start:i]) if l.strip()), "")
                # COALESCE back-to-back guards into ONE record. Recording them
                # separately anchors each to the other's comment text, and for a
                # pair that is circular: neither can place in a fresh tree
                # because each waits for the other. sub_001E8E20 (5 records) and
                # sub_0020E520 (3) were unrestorable for exactly this reason -
                # and because `apply` withholds any file with an unplaced edit,
                # that silently discarded all 10 guards in recomp_0014.c.
                #
                # A block ending in `{` is a wrap: the following marker belongs
                # inside the region it guards, so stop and let the wrap path
                # take it.
                if nxt_is_manual and not last.rstrip().endswith("{"):
                    i = j
                    continue
                break
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
                # Split the guarded region into three parts:
                #
                #   prefix   = comment + `if (cond) {`          (hand-written)
                #   wrapped  = the generated statements it guards
                #   suffix   = `}` or `} else { ... }` + close  (hand-written)
                #
                # Recording `wrapped` by CONTENT rather than by an end-anchor
                # matters: guards frequently sit back to back, and an anchor
                # based close lands in the wrong place when the following lines
                # belong to a neighbouring guard. The enclosed statements are
                # generator output, so they exist verbatim in a fresh tree.
                #
                # The generated region ends at the first line beginning with
                # `}` - either the plain close or `} else {` introducing a
                # hand-written alternative branch. Everything from there to the
                # matching close is hand-written and travels with the prefix.
                j = i
                while j < close_at and not lines[j].lstrip().startswith("}"):
                    j += 1
                edits.append({
                    "file": os.path.basename(path), "kind": "wrap",
                    "function": function_at(lines, start), "block": block,
                    "close": lines[j:close_at + 1], "match": None,
                    "anchor": anchor,
                    "wrapped": lines[i:j],
                })
                # Skip past the whole construct. The suffix often contains its
                # own `Manual addition` comment (the else branch); without this
                # the scanner would find that marker again and emit a second,
                # standalone edit for text this wrap already carries - which
                # then fails to apply because it is not independently anchored.
                i = close_at + 1
                continue

            # Record the generated line that PRECEDED the edit too. Anchor lines
            # are often not unique within a function, and the preceding line
            # disambiguates which occurrence this edit belongs to.
            p = start - 1
            while p >= 0 and not lines[p].strip():
                p -= 1
            edits.append({
                "file": os.path.basename(path),
                "kind": "insert_before",
                "function": function_at(lines, start),
                "block": block,
                "match": None,
                "anchor": anchor,
                "pre": lines[p] if p >= 0 else None,
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


# Runtime instrumentation written directly into generated code.
#
# These are PLAIN ASSIGNMENTS, so the "a generated statement ends the block"
# rule below would cut the block off at the end of its comment and then anchor
# the record on the instrumentation's own first line. That line does not exist
# in a freshly generated tree, so the edit could never be re-applied - the
# record would look fine and silently restore nothing. Exactly the failure
# audit_markers() guards against for marker wording, one level down.
#
# Kept as an explicit allowlist rather than a general `g_` prefix: the lifter
# emits g_esp, g_eax and friends constantly, and swallowing those would run a
# block on into real generated code.
INSTRUMENTATION_GLOBALS = ("g_alloc_trace", "g_alloc_trace_idx")


def _is_inserted(line):
    """Heuristic: does this line belong to a hand-written block?

    Hand edits are guards and redirects - conditionals, gotos, direct calls, and
    assignments to the frame-pointer bridge. A plain generated statement (a
    register assignment, MEM32 store, PUSH32/POP32) ends the block.
    """
    s = line.strip()
    if s.startswith(("if (", "goto ", "g_seh_ebp = ", "}", "{")):
        return True
    if s.startswith(INSTRUMENTATION_GLOBALS):
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


def _braces_balanced(lines):
    """True if every generated function in `lines` has balanced braces.

    Comments are stripped first, so a `{` inside a guard's explanation does not
    count. This is the safety net deciding whether a partially-applied file is
    safe to write.
    """
    depth, in_func = 0, False
    for l in lines:
        if FUNC_RE.match(l):
            if in_func and depth != 0:
                return False
            in_func, depth = True, 0
        code = re.sub(r"/\*.*?\*/", "", l)
        code = re.sub(r"/\*.*$", "", code)
        depth += code.count("{") - code.count("}")
    return depth == 0


def _count_block(lines, lo, hi, block):
    """How many times `block` appears verbatim inside [lo, hi)."""
    b = [l.strip() for l in block]
    n = len(b)
    if not n:
        return 0
    stripped = [l.strip() for l in lines]
    return sum(1 for i in range(lo, max(lo, hi - n + 1))
               if stripped[i:i + n] == b)


def _insert_quota(lines, edits):
    """How many copies of each (function, block) still need inserting.

    Positional idempotency cannot handle guards that are textually identical -
    and many are, deliberately: sub_001E8E20 carries the same pointer-range
    guard at four sites. _find_anchor picks A site, the "is my block just
    above?" test looks at THAT site, and with several interchangeable copies
    the answer is not reliable either way. Observed: with 4 recorded and 3 in
    the tree, apply inserted 2 and left 5.

    Counting sidesteps ordering entirely, and a count is also the only thing
    that answers the question that matters - how many are MISSING. Insert
    exactly the deficit and no more; whichever copy belongs to whichever
    record is not a question worth asking.
    """
    groups = {}
    for e in edits:
        if e["kind"] not in ("insert_before", "wrap"):
            continue
        groups.setdefault((e.get("function"), tuple(e["block"])), []).append(e)
    quota = {}
    for key, group in groups.items():
        fn, block = key
        lo, hi = _function_span(lines, fn)
        have = 0 if lo is None else _count_block(lines, lo, hi, list(block))
        quota[key] = max(0, len(group) - have)
    return quota


def _moved_already(lines, e):
    """Is this stub already replaced by its native implementation?

    Exact-text idempotency is too brittle for these: all 67 moved-to-shim
    records were re-worded in the tree ("(native PC implementation)" became
    "- generated body removed so the hand-written definition links"), so the
    text compare failed on the PROSE while the edit was fully in place. Key on
    the symbol instead - the only thing that carries meaning here.
    """
    m = MOVED_RE.match(e["block"][0].strip())
    if not m:
        return False
    sym = m.group(1)
    return any(MOVED_RE.match(l.strip()) and MOVED_RE.match(l.strip()).group(1) == sym
               for l in lines)


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
        # Computed once against the file as it arrives, before any insertion of
        # ours can inflate the counts.
        quota = _insert_quota(lines, file_edits)

        for e in file_edits:
            # The count decides for insert_before/wrap, so the per-site
            # positional idempotency checks below are bypassed for them - both
            # answering the same question would let them disagree, and the
            # count is the one that is right when copies are interchangeable.
            counted = e["kind"] in ("insert_before", "wrap")
            if counted:
                key = (e.get("function"), tuple(e["block"]))
                if quota.get(key, 0) <= 0:
                    placed += 1          # already present the recorded number of times
                    continue
                quota[key] -= 1

            if e["kind"] == "replace_line":
                pat = re.compile(e["match"])
                hit = next((i for i, l in enumerate(lines) if pat.match(l.strip())), None)
                if hit is None:
                    # Already applied is success, not failure.
                    if e["block"][0] in lines or _moved_already(lines, e):
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
            hit = None
            if e["kind"] != "wrap":
                hit = _find_anchor(lines, lo, hi, e["anchor"], e.get("pre"))
                if hit is None:
                    failed.append((e, "anchor line not found in function"))
                    continue
                # Idempotency must be POSITIONAL, not "does this text appear
                # anywhere". Many guards are worded identically (all 50 dropped
                # tail-call fixes share one comment), so a whole-file text
                # search makes every duplicate look already-applied and
                # silently drops it. (wrap does its own check below, once the
                # enclosed run has located the site.)
                n = len(e["block"])
                if not counted and hit >= n and lines[hit - n:hit] == e["block"]:
                    placed += 1  # genuinely already applied at this site
                    continue

            if e["kind"] == "wrap":
                wrapped = e.get("wrapped")
                if not wrapped:
                    failed.append((e, "wrapping guard recorded no enclosed lines"))
                    continue
                # Locate by the whole enclosed RUN, not by the anchor line. A
                # single line like `PUSH32(esp, eax);` recurs many times in one
                # function, so anchoring picks the first occurrence and wraps the
                # wrong region; a multi-line run is far more distinctive.
                hits = []
                for i2 in range(lo, hi):
                    end2 = _match_run(lines, i2, hi, wrapped)
                    if end2 is not None:
                        hits.append((i2, end2))
                if not hits:
                    failed.append((e, "enclosed lines not found in function"))
                    continue
                if len(hits) > 1:
                    # Guessing here could wrap unrelated code. Report instead.
                    failed.append((e, f"enclosed lines match {len(hits)} places - ambiguous"))
                    continue
                hit, end = hits[0]
                live = _wrap_crosses_live_label(lines, lo, hi, hit, end)
                if live:
                    failed.append((e, f"enclosed run spans {live}, which is still "
                                      "a goto target - wrapping it would let that "
                                      "path skip the guard"))
                    continue
                # Idempotency, positionally: is the prefix already right above?
                n = len(e["block"])
                if not counted and hit >= n and lines[hit - n:hit] == e["block"]:
                    placed += 1
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
    #
    # --partial relaxes that for the one case where it is the right call: after
    # a full regeneration, where a handful of edits are replacements the
    # insert-before-anchor model cannot express and must be redone by hand.
    # Refusing everything then discards 200+ good placements to protect a
    # handful.
    #
    # The discriminator is BRACE BALANCE, not "did every edit place". A
    # wrapping guard inserts an opening brace and its matching close as one
    # operation, so a file where some wraps placed and others did not ends up
    # with unmatched braces and will not compile - that happened here, +2. An
    # insert-before-anchor edit only adds a comment block and cannot unbalance
    # anything, so a file whose only failures are of that kind is still valid.
    if not dry_run:
        bad = {e["file"] for e, _ in failed}
        for path, lines in pending.items():
            # The discriminator is the failed edit's KIND, which is already
            # recorded - no brace parsing needed.
            #
            # A `wrap` inserts an opening brace and its matching close as one
            # operation, so a file where some wraps placed and others did not
            # has unmatched braces and will not compile (observed: +2). Every
            # other kind only inserts or replaces a comment block and cannot
            # unbalance anything.
            #
            # So a file is written unless one of ITS failures was a wrap. That
            # keeps the ~100 guards in files that failed only on
            # insert-before-anchor edits, instead of discarding them to protect
            # a file that was never at risk.
            ok = os.path.basename(path) not in bad
            if not ok and getattr(args, "partial", False):
                ok = not any(e["kind"] == "wrap" for e, _ in failed
                             if e["file"] == os.path.basename(path))
                if not ok and getattr(args, "force", False):
                    # --force writes even the wrap-affected files. Use it only
                    # with `check-braces` afterwards, which names any function
                    # left unbalanced so it can be repaired by hand. Preferable
                    # to discarding ~100 good guards in a file that failed on a
                    # handful of wraps.
                    print(f"  {os.path.basename(path)}: WRITTEN under --force; "
                          f"run `check-braces` and repair by hand")
                    ok = True
                elif not ok:
                    print(f"  {os.path.basename(path)}: left as generated - a "
                          f"failed wrap would unbalance its braces")
            if ok:
                open(path, "w", encoding="utf-8").write("\n".join(lines))

    total = len(edits)
    verb = "would place" if dry_run else "placed"
    print(f"{verb} {placed}/{total} manual edits")
    if failed:
        # "Failed to place" is two different things and reporting them as one
        # number is why the loss was mis-sized twice. Split on whether the
        # guard is actually in the tree.
        gone = [(e, why) for e, why in failed
                if _marker_present(args.gen_dir, e) is False]
        noise = [(e, why) for e, why in failed if (e, why) not in gone]
        print(f"\nFAILED to place {len(failed)}: "
              f"{len(gone)} MISSING from the tree, "
              f"{len(noise)} already present (re-run artefact)")
        if gone:
            print("\nMISSING - these guards are not in gen/ and are the worklist:")
            for e, why in gone:
                print(f"  {e['file']} {e.get('function') or ''} "
                      f"[{e['kind']}]: {why}")
        if noise:
            print(f"\nalready present, anchor context moved ({len(noise)}):")
            for e, why in noise[:10]:
                print(f"  {e['file']} {e.get('function') or ''}: {why}")
            if len(noise) > 10:
                print(f"  ... and {len(noise)-10} more")
        print("\nRefusing to treat this as a successful restore.")
        return 1
    return 0


_ICALL_TARGET_RE = re.compile(
    r"uint32_t _icall_target = (?P<t>.+?);\s*PUSH32\(esp, 0\);\s*"
    r"RECOMP_ICALL_SAFE\(_icall_target,")

# Direct calls used to be emitted bare and are now routed through the ABI
# wrapper: `sub_00119900();` became `RECOMP_ABI_CALL(sub_00119900);`. Every
# `wrapped` run recorded before that change carries the old spelling, and
# _match_run compares the run line by line, so ONE re-spelled call line makes
# the whole run miss and the wrapping guard drops. That is what silently lost
# all 18 "dependency type" D3D-null guards - the guards were fine, the
# recording was written against a spelling the generator no longer emits.
_ABI_CALL_RE = re.compile(r"RECOMP_ABI_CALL\((?P<f>sub_[0-9A-Fa-f]+)\)")


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
    still matches. Direct calls get the same treatment - see _ABI_CALL_RE.
    """
    s = " ".join(line.split())
    m = _ICALL_TARGET_RE.search(s)
    if m:
        s = _ICALL_TARGET_RE.sub(f"RECOMP_ICALL_SAFE({m.group('t')},", s)
    # Normalise towards the OLD bare-call spelling, which is what the stored
    # records hold. Direction does not matter as long as both sides agree.
    s = _ABI_CALL_RE.sub(lambda mm: f"{mm.group('f')}()", s)
    return s


_LABEL = re.compile(r"^loc_[0-9A-Fa-f]+: ;\s*$")


def _wrap_crosses_live_label(lines, lo, hi, start, end):
    """Name a label inside [start,end) that something still jumps to.

    Wrapping code in an `if/else` is only behaviour-preserving while nothing
    branches into the middle of it. C permits a goto into a block, but it would
    skip the condition we just added, so the guard silently would not apply on
    that path. Report instead of guessing.
    """
    for i in range(start, end):
        m = _LABEL.match(lines[i])
        if not m:
            continue
        label = lines[i].split(":")[0].strip()
        for j in range(lo, hi):
            if j == i:
                continue
            if ("goto " + label) in lines[j]:
                return label
    return None


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
        # Skip label-only lines on the file side too. A hand edit restructures
        # the code it wraps, so the recorded run often has no label where the
        # freshly generated tree emits one - that alone broke 69 of 222
        # re-applications after the full regeneration. Whether wrapping across
        # a label is *safe* is checked separately by _wrap_crosses_live_label().
        if _LABEL.match(lines[i]) and not _LABEL.match(run[k]):
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


def _find_anchor(lines, lo, hi, anchor, pre=None):
    """Index of `anchor` within [lo, hi), exact first then normalised.

    Anchor lines are frequently NOT unique inside a function (`PUSH32(esp, eax);`
    can occur a dozen times), so when a `pre` line is recorded - the generated
    line that preceded the edit - candidates are filtered to those immediately
    following it. A single unfiltered match is still accepted, since most edits
    do have a unique anchor.
    """
    def candidates(cmp):
        return [i for i in range(lo, hi) if cmp(lines[i])]

    hits = candidates(lambda l: l == anchor)
    if not hits:
        want = _normalise(anchor)
        hits = candidates(lambda l: _normalise(l) == want)
    if not hits:
        return None
    if len(hits) > 1 and pre:
        want_pre = _normalise(pre)
        narrowed = []
        for i in hits:
            j = i - 1
            while j >= lo and not lines[j].strip():
                j -= 1
            if j >= lo and _normalise(lines[j]) == want_pre:
                narrowed.append(i)
        if len(narrowed) == 1:
            return narrowed[0]
        if narrowed:
            hits = narrowed
    return hits[0]


def _function_span(lines, name):
    if name is None:
        return 0, len(lines)
    start = next((i for i, l in enumerate(lines) if FUNC_RE.match(l) and FUNC_RE.match(l).group(1) == name), None)
    if start is None:
        return None, None
    end = next((i for i in range(start + 1, len(lines)) if FUNC_RE.match(lines[i])), len(lines))
    return start, end


def _marker_present(gen_dir, e):
    """Is this edit's marker comment already inside its function in the tree?

    An anchor-based failure is ambiguous on its own. Re-running against an
    already-edited tree makes an edit that IS correctly in place report as
    failed, because the line preceding its anchor is now its own block, so the
    disambiguating context no longer matches. That artefact is what made the
    85 failures look like 85 losses.

    This answers the only question that matters - is the guard in the tree or
    not - by looking for the marker comment itself, which no anchor logic
    touches. A failure whose marker is present is noise; one whose marker is
    absent is a real, load-bearing guard that is gone.
    """
    mark = next((l for l in (e.get("block") or [])
                 if any(l.lstrip().startswith(mk) for mk in MARKERS)), None)
    if mark is None:
        return None          # nothing to look for - cannot classify
    try:
        lines = open(os.path.join(gen_dir, e["file"]),
                     encoding="utf-8", errors="ignore").read().split("\n")
    except OSError:
        return False
    lo, hi = _function_span(lines, e.get("function") or None)
    if lo is None:
        return False         # function itself is missing, so the guard is too
    return mark in lines[lo:hi]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=["extract", "apply", "verify", "check-braces"])
    ap.add_argument("-o", "--store", default=DEFAULT_STORE)
    ap.add_argument("-i", "--input", dest="store_in")
    ap.add_argument("--gen-dir", default=DEFAULT_GEN)
    ap.add_argument("--force", action="store_true",
                    help="with --partial, also write wrap-affected files; "
                         "follow with `check-braces`")
    ap.add_argument("--partial", action="store_true",
                    help="write the edits that placed and list the rest "
                         "as a to-do (default refuses a partial restore)")
    args = ap.parse_args()
    if args.store_in:
        args.store = args.store_in

    if args.command == "extract":
        return cmd_extract(args)
    if args.command == "apply":
        return cmd_apply(args, dry_run=False)
    if args.command == "check-braces":
        bad = 0
        for path in sorted(glob.glob(os.path.join(args.gen_dir, "recomp_*.c"))):
            lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
            starts = [(i, m.group(1)) for i, l in enumerate(lines)
                      if (m := FUNC_RE.match(l))]
            for n, (i, name) in enumerate(starts):
                end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
                d = 0
                for l in lines[i:end]:
                    code = re.sub(r"/\*.*?\*/", "", l)
                    d += code.count("{") - code.count("}")
                if d:
                    print(f"{os.path.basename(path)}:{i + 1} {name}: "
                          f"brace delta {d:+d}")
                    bad += 1
        print("all functions balanced" if not bad else f"{bad} unbalanced")
        return 1 if bad else 0
    return cmd_apply(args, dry_run=True)


if __name__ == "__main__":
    sys.exit(main())
