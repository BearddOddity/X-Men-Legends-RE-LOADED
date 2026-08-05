#!/usr/bin/env python3
"""Check the `manual_edits.py lost` audit against a tree we control.

"Verify the instrument first." Three measurement bugs in one session all came
from trusting a count without checking how it was produced, so the audit that
is meant to name lost guards gets its own test before it is believed.

The fixture is a two-file synthetic gen/ tree with edits in a known state:
one guard applied, one withheld, one wrap applied, one wrap withheld, one
replace_line applied, one withheld, one edit in a renamed function, and one
in a deleted file. `lost` must name exactly the withheld ones - no more, and
no fewer.

    py -3 tools_data/test_manual_edits_lost.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "manual_edits.py")

GUARD_IN = [
    "    /* Manual guard (not in original x86): applied. */",
    "    if (!recomp_plausible(eax)) goto loc_00100010;",
]
GUARD_OUT = [
    "    /* Manual guard (not in original x86): withheld. */",
    "    if (!recomp_plausible(ebx)) goto loc_00100010;",
]
WRAP_IN = [
    "    /* Manual guard (not in original x86): wrap, applied. */",
    "    if (MEM32(esi) != 0) {",
]
WRAP_OUT = [
    "    /* Manual guard (not in original x86): wrap, withheld. */",
    "    if (MEM32(edi) != 0) {",
]

FILE_A = """void sub_00100000(void)
{
{APPLIED_GUARD}
    eax = MEM32(esp);
    esi = eax;
}

void sub_00100100(void)
{
    ebx = MEM32(esp);
    edi = ebx;
}

void sub_00100200(void)
{
{APPLIED_WRAP}
    ecx = MEM32(edx);
    edx = ecx;
{APPLIED_CLOSE}
}

void sub_00100300(void)
{
    ebp = MEM32(esp);
    esp = ebp;
}

/* sub_00100400: moved to src/d3d8_shim.c (native implementation) */
void sub_00100500(void) { /* stub */ }
"""

# sub_00200100 is deliberately absent - the store references it, standing in
# for the XAPI rename, where 35 functions vanished under new names.
FILE_B = """void sub_00200000(void)
{
    eax = 1;
}
"""


def build_tree(gen):
    a = (FILE_A
         .replace("{APPLIED_GUARD}", "\n".join(GUARD_IN))
         .replace("{APPLIED_WRAP}", "\n".join(WRAP_IN))
         .replace("{APPLIED_CLOSE}", "    }"))
    open(os.path.join(gen, "recomp_0001.c"), "w").write(a)
    open(os.path.join(gen, "recomp_0002.c"), "w").write(FILE_B)
    # recomp_0003.c is never written: the store references it, standing in for
    # a file the regeneration did not produce.


def build_store(path):
    edits = [
        # applied
        {"file": "recomp_0001.c", "kind": "insert_before",
         "function": "sub_00100000", "block": GUARD_IN,
         "anchor": "    eax = MEM32(esp);", "match": None},
        # withheld -> must be reported
        {"file": "recomp_0001.c", "kind": "insert_before",
         "function": "sub_00100100", "block": GUARD_OUT,
         "anchor": "    ebx = MEM32(esp);", "match": None},
        # applied wrap
        {"file": "recomp_0001.c", "kind": "wrap",
         "function": "sub_00100200", "block": WRAP_IN,
         "wrapped": ["    ecx = MEM32(edx);", "    edx = ecx;"],
         "close": ["    }"], "anchor": "    ecx = MEM32(edx);", "match": None},
        # withheld wrap -> must be reported
        {"file": "recomp_0001.c", "kind": "wrap",
         "function": "sub_00100300", "block": WRAP_OUT,
         "wrapped": ["    ebp = MEM32(esp);", "    esp = ebp;"],
         "close": ["    }"], "anchor": "    ebp = MEM32(esp);", "match": None},
        # applied replace_line
        {"file": "recomp_0001.c", "kind": "replace_line", "function": None,
         "block": ["/* sub_00100400: moved to src/d3d8_shim.c "
                   "(native implementation) */"],
         "match": r"^void sub_00100400\(void\) \{ /\*.*\*/ \}$",
         "anchor": None},
        # withheld replace_line -> must be reported
        {"file": "recomp_0001.c", "kind": "replace_line", "function": None,
         "block": ["/* sub_00100600: moved to src/audio_shim.c "
                   "(native implementation) */"],
         "match": r"^void sub_00100600\(void\) \{ /\*.*\*/ \}$",
         "anchor": None},
        # function absent from the tree -> reported separately from a lost guard
        {"file": "recomp_0002.c", "kind": "insert_before",
         "function": "sub_00200100", "block": GUARD_OUT,
         "anchor": "    eax = 2;", "match": None},
        # file absent from the tree -> its own bucket
        {"file": "recomp_0003.c", "kind": "insert_before",
         "function": "sub_00300000", "block": GUARD_OUT,
         "anchor": "    eax = 3;", "match": None},
    ]
    json.dump(edits, open(path, "w"), indent=1)


def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}")
    if not ok:
        print(f"       wanted {want!r}")
    return ok


def main():
    with tempfile.TemporaryDirectory() as tmp:
        gen = os.path.join(tmp, "gen")
        os.makedirs(gen)
        store = os.path.join(tmp, "edits.json")
        build_tree(gen)
        build_store(store)

        report = os.path.join(tmp, "lost.json")
        proc = subprocess.run(
            [sys.executable, TOOL, "lost", "-i", store, "--gen-dir", gen,
             "--json", report],
            capture_output=True, text=True)
        print(proc.stdout)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)

        r = json.load(open(report))
        lost_fns = sorted(e.get("function") or e["block"][0] for e in r["lost"])
        missing_fns = sorted(e["function"] for e in r["function_missing"])
        missing_files = sorted(e["file"] for e in r["file_missing"])

        results = [
            check("exit code is non-zero when edits are lost",
                  proc.returncode, 1),
            check("present count", r["present"], 3),
            check("lost edits named",
                  lost_fns,
                  ["/* sub_00100600: moved to src/audio_shim.c "
                   "(native implementation) */",
                   "sub_00100100", "sub_00100300"]),
            check("missing function reported separately",
                  missing_fns, ["sub_00200100"]),
            check("missing file reported separately",
                  missing_files, ["recomp_0003.c"]),
        ]

        # A clean tree must report nothing. Withheld edits removed from the
        # store, so every remaining record is genuinely in place.
        clean = json.load(open(store))
        keep = [e for e in clean
                if e["function"] in ("sub_00100000", "sub_00100200")
                or (e["kind"] == "replace_line" and "00100400" in e["block"][0])]
        json.dump(keep, open(store, "w"), indent=1)
        proc2 = subprocess.run(
            [sys.executable, TOOL, "lost", "-i", store, "--gen-dir", gen],
            capture_output=True, text=True)
        results.append(check("clean tree exits 0", proc2.returncode, 0))
        results.append(check("clean tree reports nothing lost",
                             "LOST" in proc2.stdout, False))

    print("\nall checks passed" if all(results) else "\nFAILURES above")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
