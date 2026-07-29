"""
Reusable fixup for a systematic lifter bug found while debugging X-Men
Legends: at certain mid-function boundary splits, the lifter drops the
tail-call/fallthrough into the next function entirely, leaving a
generated C function that ends with a bare PUSH32(...) statement and no
call, tail-call, or return. This leaks simulated stack (each dropped
push is never popped) on every invocation.

Verified (2026-07-29 session): all 50 occurrences found in the codebase
have the next function's "Original: START - ..." address exactly equal
to this function's own "Original: ... - END" address, confirming the
lifter intended a direct fallthrough/tail-call at every site (not
legitimate dead code). Run this after any regeneration of
src/recomp/gen/*.c via the disasm/func_id/recomp pipeline to reapply.

Usage: run from src/game/ (or point glob at wherever recomp_*.c live).
    py -3 fix_dropped_tailcalls.py
"""
import re
import glob

FUNC_PAT = re.compile(
    r"/\*\*\n"
    r" \* (sub_[0-9A-F]+)\n"
    r"(?: \* [^\n]*\n)*?"
    r" \* Original: (0x[0-9A-F]+) - (0x[0-9A-F]+) [^\n]*\n"
    r"(?: \* [^\n]*\n)*"
    r" \*/\n"
    r"(?:#if 0\n)?"
    r"void \1\(void\)\n"
    r"\{\n"
    r"((?:.*\n)*?)"
    r"\}\n"
)

DROP_PAT = re.compile(r"(    PUSH32\(esp, [^;]+\);\n)\n$")


def fix_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    funcs = list(FUNC_PAT.finditer(content))

    fixes = []
    for i, m in enumerate(funcs):
        name, start, end, body = m.groups()
        dm = DROP_PAT.search(body)
        if not dm:
            continue
        if i + 1 >= len(funcs):
            continue
        next_name, next_start = funcs[i + 1].group(1), funcs[i + 1].group(2)
        if next_start != end:
            continue  # not a contiguous split - leave alone, needs manual review
        has_ebp = "uint32_t ebp;" in body
        fixes.append((m.start(4), m.end(4), name, next_name, has_ebp))

    if not fixes:
        return 0

    new_content = content
    for body_start, body_end, name, next_name, has_ebp in reversed(fixes):
        body = new_content[body_start:body_end]
        dm2 = DROP_PAT.search(body)
        assert dm2, (name, "pattern vanished on reapply")
        insertion_point = body_start + dm2.end(1)
        comment = (
            f"    /* Manual fix (not in original x86): the lifter cut this\n"
            f"     * function off here, dropping the tail-call into {next_name}\n"
            f"     * (confirmed by matching original address ranges - this\n"
            f"     * function's own \"Original\" end address equals {next_name}'s\n"
            f"     * start address). See DEBUGGING_NOTES.md - one instance of a\n"
            f"     * systematic lifter bug found at 50 sites across the codebase. */\n"
        )
        call_line = (
            f"    g_seh_ebp = ebp; {next_name}(); return;\n"
            if has_ebp
            else f"    {next_name}(); return;\n"
        )
        new_content = (
            new_content[:insertion_point] + comment + call_line + new_content[insertion_point:]
        )

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    return len(fixes)


if __name__ == "__main__":
    grand_total = 0
    for f in sorted(glob.glob("src/recomp/gen/recomp_*.c")):
        n = fix_file(f)
        if n:
            print(f, "fixed", n)
            grand_total += n
    print("GRAND TOTAL:", grand_total)
