"""
seed_missing_functions.py - recompile functions the disassembler never found,
without regenerating the tree.

Why not just regenerate
-----------------------
A full regeneration would pick these up, but it also zeroes the 214 hand-written
guards in gen/ - manual_edits.py re-applies most, not all - and this project has
been burned by wide-scope changes before (61 -> 44). Seeding is purely additive:
new functions land in their own gen/recomp_seed.c, and nothing that already
works is touched.

How it works
------------
1. Compute each function's extent by disassembling to its terminating `ret`.
2. Write an augmented functions.json with those entries appended.
3. Run `tools.recomp -f <addr>` per function against that augmented list, which
   prints the translated C to stdout.
4. Emit gen/recomp_seed.c, and the registration lines for
   recomp_lookup_manual() in src/recomp_manual.c.

Registration goes through recomp_lookup_manual() - the designed extension point,
tried before the generated dispatch table - so no generated file is edited and
the wiring survives a future regeneration.

Usage (from src/game/):
    py -3 tools_data/seed_missing_functions.py --observed          # dry run
    py -3 tools_data/seed_missing_functions.py --observed --apply
    py -3 tools_data/seed_missing_functions.py --va 0x00227F50 --apply
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(GAME_DIR))
XBE = os.path.join(GAME_DIR, "game", "default.xbe")
FUNCS_JSON = os.path.join(REPO, "tools", "disasm", "output", "functions.json")
GEN_DIR = os.path.join(GAME_DIR, "src", "recomp", "gen")
SEED_C = os.path.join(GEN_DIR, "recomp_seed.c")
MANUAL_C = os.path.join(GAME_DIR, "src", "recomp_manual.c")

sys.path.insert(0, HERE)
from whatis import load_sections  # noqa: E402

MAX_FUNC_BYTES = 0x800


def function_extent(md, data, text, va):
    """Disassemble from va to the ret that ends the function.

    A function can contain several `ret`s; the last one is the end. Stop only
    at a ret/jmp that no forward branch jumps past, and never run into the
    int3 padding that separates functions.
    """
    off = text["raw"] + (va - text["va"])
    furthest = va
    end = None
    for ins in md.disasm(data[off:off + MAX_FUNC_BYTES], va):
        if ins.mnemonic == "int3":
            break
        for op in re.findall(r"0x([0-9a-f]+)", ins.op_str):
            t = int(op, 16)
            if va < t < va + MAX_FUNC_BYTES and ins.mnemonic.startswith(("j", "loop")):
                furthest = max(furthest, t)
        nxt = ins.address + ins.size
        if ins.mnemonic in ("ret", "retf") and nxt > furthest:
            end = nxt
            break
        if ins.mnemonic == "jmp" and nxt > furthest:
            end = nxt
            break
        furthest = max(furthest, nxt)
    return end


def observed_failures():
    log = os.path.join(GAME_DIR, "stderr.txt")
    if not os.path.exists(log):
        return []
    text = open(log, encoding="utf-8", errors="ignore").read()
    return sorted({int(x, 16)
                   for x in re.findall(r"Failed to resolve VA 0x([0-9A-Fa-f]+)", text)})


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--va", action="append", default=[],
                    help="hex VA to seed (repeatable)")
    ap.add_argument("--observed", action="store_true",
                    help="seed every real .text VA that failed in stderr.txt")
    ap.add_argument("--apply", action="store_true",
                    help="write recomp_seed.c and patch recomp_manual.c")
    args = ap.parse_args(argv)

    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)

    data, secs = load_sections()
    text = next(s for s in secs if s["name"] == ".text")
    tlo, thi = text["va"], text["va"] + text["vsize"]

    known = json.load(open(FUNCS_JSON, encoding="utf-8"))
    starts = {int(f["start"], 16) for f in known}

    wanted = [int(v, 16) for v in args.va]
    if args.observed:
        wanted += [v for v in observed_failures() if tlo <= v < thi]
    wanted = sorted(set(wanted) - starts)
    if not wanted:
        print("nothing to seed")
        return 0

    entries, skipped = [], []
    for va in wanted:
        end = function_extent(md, data, text, va)
        if end is None:
            skipped.append((va, "no terminating ret found"))
            continue
        entries.append({
            "start": f"0x{va:08X}", "end": f"0x{end:08X}", "size": end - va,
            "name": f"sub_{va:08X}", "section": ".text", "confidence": 0.8,
            "detection_method": "seeded_from_data_pointer",
            "num_instructions": 0, "has_prologue": False,
            "calls_to": [], "called_by": [],
        })

    print(f"seeding {len(entries)} function(s):")
    for e in entries:
        print(f"  {e['name']}  {e['start']}-{e['end']}  ({e['size']} bytes)")
    for va, why in skipped:
        print(f"  SKIP 0x{va:08X} - {why}")
    if not args.apply:
        print("\ndry run; re-run with --apply")
        return 0

    # Augmented function list, sorted by start as the loader expects.
    aug = sorted(known + entries, key=lambda f: int(f["start"], 16))
    aug_path = os.path.join(GEN_DIR, "..", "..", "..",
                            "seeded_functions.json")
    aug_path = os.path.abspath(aug_path)
    with open(aug_path, "w", encoding="utf-8") as f:
        json.dump(aug, f)

    bodies = []
    for e in entries:
        r = subprocess.run(
            [sys.executable, "-m", "tools.recomp", XBE,
             "--functions", aug_path, "--skip-binary-check",
             "-f", e["start"]],
            cwd=REPO, capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            print(f"  FAILED {e['name']}: {r.stderr.strip()[-300:]}")
            continue
        bodies.append((e["name"], r.stdout))
        print(f"  translated {e['name']} ({len(r.stdout)} bytes of C)")

    if not bodies:
        print("nothing translated; leaving the tree alone")
        return 1

    with open(SEED_C, "w", encoding="utf-8", newline="") as f:
        f.write("/**\n"
                " * Functions the disassembler never discovered.\n"
                " *\n"
                " * These are reachable only through data-section pointers - CRT\n"
                " * static-initializer tables and vtables - so a call-target scan\n"
                " * never found them, and every indirect call to one failed and\n"
                " * returned NULL. See tools_data/find_missing_functions.py and\n"
                " * DEBUGGING_NOTES.md.\n"
                " *\n"
                " * Generated by tools_data/seed_missing_functions.py. Additive:\n"
                " * no existing generated file is modified. Registered through\n"
                " * recomp_lookup_manual() in src/recomp_manual.c.\n"
                " */\n\n"
                "#define RECOMP_GENERATED_CODE\n"
                '#include "recomp_funcs.h"\n'
                "#include <math.h>\n\n")
        for name, code in bodies:
            f.write(f"void {name}(void);\n")
        f.write("\n")
        for name, code in bodies:
            f.write(code.rstrip() + "\n\n")
    print(f"\nwrote {SEED_C}")

    # Register through the manual hook.
    src = open(MANUAL_C, encoding="utf-8", errors="ignore").read()
    decls = "".join(f"extern void {n}(void);\n" for n, _ in bodies)
    hooks = "".join(f"    if (xbox_va == 0x{n[4:]}u) return {n};\n"
                    for n, _ in bodies)
    marker = "recomp_func_t recomp_lookup_manual(uint32_t xbox_va)\n{\n"
    assert marker in src, "recomp_lookup_manual not found in recomp_manual.c"
    block = (
        "/* ── Seeded functions (tools_data/seed_missing_functions.py) ── */\n"
        "/*\n"
        " * Reachable only via data-section pointers, so function discovery\n"
        " * missed them and every indirect call here returned NULL. Declared\n"
        " * here rather than in the generated recomp_funcs.h so the wiring\n"
        " * survives a regeneration.\n"
        " */\n" + decls + "\n")
    if "Seeded functions (tools_data" in src:
        src = re.sub(r"/\* ── Seeded functions.*?\n\n",
                     block, src, count=1, flags=re.S)
        src = re.sub(r"(recomp_func_t recomp_lookup_manual\(uint32_t xbox_va\)\n\{\n)"
                     r"(?:    if \(xbox_va == 0x[0-9A-F]+u\) return sub_[0-9A-F]+;\n)*",
                     lambda m: m.group(1) + hooks, src, count=1)
    else:
        src = src.replace(marker, block + marker + hooks, 1)
    with open(MANUAL_C, "w", encoding="utf-8", newline="") as f:
        f.write(src)
    print(f"registered {len(bodies)} function(s) in {MANUAL_C}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
