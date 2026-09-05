"""Re-record the `wrapped` line list of each wrap edit against the current tree.

Why this is needed
------------------
A `wrap` edit stores the exact lines it encloses. Any generator change that
emits an extra line inside that region - the add/sub carry assignment, for
instance - makes the recorded list stop matching, and manual_edits.py then
refuses to write the whole file, dropping every edit it holds. That is a
generator improvement being blocked by a bookkeeping format, not by anything
about the code.

What it does
------------
For each wrap edit that no longer matches, find the recorded first line inside
the edit's own function, then walk forward accepting the recorded lines in
order while skipping lines the generator newly inserted (`_cf = ...` carry
assignments and blank lines are the only ones allowed to appear). If every
recorded line is found in order, the span from first to last becomes the new
`wrapped` list.

Refuses to touch an edit whose recorded lines are not all found, so a genuinely
moved edit still surfaces as a failure rather than being silently re-anchored.

Usage (from src/game/):
    py -3 refresh_wraps.py tools_data/manual_edits.remapped.json src/recomp/gen
"""
import json
import re
import sys

STORE = sys.argv[1]
GEN = sys.argv[2]

SKIPPABLE = re.compile(r"^\s*(_cf = |$)")


def function_span(lines, func):
    start = None
    for i, l in enumerate(lines):
        if l == "void %s(void)" % func:
            start = i
            break
    if start is None:
        return None, None
    for j in range(start, len(lines)):
        if lines[j] == "}":
            return start, j
    return start, len(lines) - 1


def main():
    edits = json.load(open(STORE, encoding="utf-8"))
    files = {}
    refreshed = skipped = 0

    for e in edits:
        if e["kind"] != "wrap":
            continue
        path = "%s/%s" % (GEN, e["file"])
        if path not in files:
            files[path] = open(path, encoding="utf-8", errors="ignore").read().split("\n")
        lines = files[path]
        lo, hi = function_span(lines, e["function"])
        if lo is None:
            skipped += 1
            continue

        want = e["wrapped"]
        # Already correct?
        joined = "\n".join(lines[lo:hi])
        if "\n".join(want) in joined:
            continue

        # Find the first recorded line, then walk both lists together.
        for anchor in range(lo, hi):
            if lines[anchor] != want[0]:
                continue
            k = 1
            j = anchor + 1
            while j < hi and k < len(want):
                if lines[j] == want[k]:
                    k += 1
                elif SKIPPABLE.match(lines[j]) and lines[j] != want[k]:
                    pass                      # generator inserted this line
                else:
                    break
                j += 1
            if k == len(want):
                e["wrapped"] = lines[anchor:j]
                refreshed += 1
                break
        else:
            skipped += 1

    json.dump(edits, open(STORE, "w", encoding="utf-8"), indent=1)
    print("refreshed %d wrap edit(s), %d left alone" % (refreshed, skipped))


if __name__ == "__main__":
    main()
