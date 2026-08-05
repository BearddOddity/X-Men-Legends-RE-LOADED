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


def stub_addresses(tail_only=False):
    """Addresses defined as empty bodies in recomp_stubs_unresolved.c.

    With tail_only, keep just those reached by a lifter tail call
    (`sub_XXXX(); return;`). Those terminate a fragment chain, so every
    PUSH32 still pending in the chain never gets its POP32 - the caller's
    ebx/esi/edi are silently lost, which is how _initterm's table cursor and
    loop bound were both destroyed.
    """
    import glob
    stub_file = os.path.join(GEN_DIR, "recomp_stubs_unresolved.c")
    if not os.path.exists(stub_file):
        return []
    stub_re = re.compile(r"^void sub_([0-9A-Fa-f]+)\(void\) \{")
    stubs = {}
    for line in open(stub_file, encoding="utf-8", errors="replace"):
        m = stub_re.match(line)
        if m:
            stubs["sub_" + m.group(1)] = int(m.group(1), 16)
    if not tail_only:
        return sorted(stubs.values())

    tail_re = re.compile(
        r"^\s*(?:g_seh_ebp = ebp; )?(?:RECOMP_ABI_CALL\()?"
        r"(sub_[0-9A-Fa-f]+)\)?\(?\)?; return;")
    hit = set()
    for path in glob.glob(os.path.join(GEN_DIR, "*.c")):
        if path.endswith("recomp_stubs_unresolved.c"):
            continue
        for line in open(path, encoding="utf-8", errors="replace"):
            m = tail_re.match(line)
            if m and m.group(1) in stubs:
                hit.add(stubs[m.group(1)])
    return sorted(hit)


def containing_function(va, known):
    """Return the entry of the known function containing va, or None.

    An address that falls INSIDE another function is a label, not a function
    start. Seeding it fabricates a function that begins mid-instruction-stream
    and ends at whatever ret it happens to reach - which is why these never
    resolved no matter how many closure rounds ran. sub_0023BF80 is +0xA0 into
    sub_0023BEE0; sub_00397554 and sub_00397555 are +0xD4 into sub_00397480 and
    one byte apart, i.e. scan artefacts.

    The lifter already handles genuine mid-function entry points by splitting
    the function into fragments. When one of these turns up unresolved it means
    the split did not happen, and the fix belongs in the lifter - not in a
    fabricated seed.
    """
    best = None
    for f in known:
        start = int(f["start"], 16)
        end_s = f.get("end")
        if end_s is None:
            continue
        end = int(end_s, 16) if isinstance(end_s, str) else end_s
        if start < va < end:
            if best is None or start > best:
                best = start
    return best


def overridden_names():
    """Functions implemented by hand - never seed over these."""
    names = set()
    for rel in ("src/recomp_manual.c", "src/d3d8_shim.c"):
        p = os.path.join(GAME_DIR, rel)
        if not os.path.exists(p):
            continue
        for line in open(p, encoding="utf-8", errors="replace"):
            m = re.match(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*$", line)
            if m:
                names.add(m.group(1))
    return names


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--va", action="append", default=[],
                    help="hex VA to seed (repeatable)")
    ap.add_argument("--observed", action="store_true",
                    help="seed every real .text VA that failed in stderr.txt")
    ap.add_argument("--stubs", action="store_true",
                    help="seed every address that recomp_stubs_unresolved.c "
                         "defines as an empty body. Those are not harmless: in "
                         "this calling convention the callee pops the fake "
                         "return address, so a do-nothing stub pops nothing "
                         "and leaks simulated stack on every call - and when "
                         "one terminates a lifter tail-call chain it also "
                         "swallows the PUSH32/POP32 pairs that restore "
                         "ebx/esi/edi for the whole chain.")
    ap.add_argument("--tail-only", action="store_true",
                    help="with --stubs, restrict to stubs reached by a tail "
                         "call - the ones that provably swallow a restore")
    ap.add_argument("--force", action="store_true",
                    help="seed addresses given with --va even if the function "
                         "list already records them. Needed to rebuild "
                         "recomp_seed.c after a regeneration, because the "
                         "recorded entries otherwise filter themselves out.")
    ap.add_argument("--record", metavar="FILE", default="seed_list.json",
                    help="write the resolved seed address list here (default "
                         "seed_list.json). This record is what makes a rebuild "
                         "reproducible.")
    ap.add_argument("--from-list", metavar="FILE",
                    help="seed exactly the addresses in FILE and nothing else. "
                         "Implies --force. This is how recomp_seed.c is rebuilt "
                         "after a regeneration: one pass from a recorded list, "
                         "instead of iterating on whatever the linker happens "
                         "to complain about next.")
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

    if args.from_list:
        with open(args.from_list, encoding="utf-8") as f:
            args.va = ["0x%08X" % v for v in json.load(f)["addresses"]]
        args.force = True
        args.stubs = args.observed = False

    wanted = [int(v, 16) for v in args.va]
    if args.observed:
        wanted += [v for v in observed_failures() if tlo <= v < thi]
    if args.stubs:
        wanted += [v for v in stub_addresses(args.tail_only)
                   if tlo <= v < thi]
    # `starts` is every address the function list already knows about, which
    # after one seeding run INCLUDES everything seeded. Filtering against it
    # makes re-seeding a silent no-op - and if a regeneration has since wiped
    # recomp_seed.c, those addresses can never get a body again. That is what
    # stalled the closure loop at 14 unresolved symbols, which then became
    # purging stubs instead of real functions and cost 54 kernel calls -> 3.
    #
    # --force seeds explicitly named addresses regardless, which is what makes
    # rebuilding recomp_seed.c from a recorded list deterministic.
    if args.force:
        explicit = {int(v, 16) for v in args.va}
        wanted = sorted(set(wanted) - (starts - explicit))
    else:
        wanted = sorted(set(wanted) - starts)
    if not wanted:
        print("nothing to seed")
        return 0

    overrides = overridden_names()

    entries, skipped = [], []
    for va in wanted:
        if "sub_%08X" % va in overrides:
            skipped.append((va, "hand-written override - must not be seeded"))
            continue
        owner = containing_function(va, known)
        if owner is not None:
            skipped.append((va, "mid-function: +0x%X into sub_%08X - the lifter "
                                "should split, not the seeder" % (va - owner, owner)))
            continue
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

    # Carry forward anything already seeded.
    #
    # Seeding is inherently iterative: a newly seeded body introduces call
    # targets nothing had declared before, so the next link names more
    # addresses to seed. This file used to be overwritten each run, which
    # silently discarded the previous round - and because the addresses are
    # also recorded in seeded_functions.json, re-running did NOT bring them
    # back: they now count as known starts and get filtered out. The result
    # linked against 121 functions where the previous run had 908.
    existing = []
    if os.path.exists(SEED_C):
        old = open(SEED_C, encoding="utf-8", errors="replace").read().split("\n")
        i = 0
        while i < len(old):
            m = re.match(r"^void (sub_[0-9A-Fa-f]+)\(void\)\s*$", old[i])
            if not m:
                i += 1
                continue
            j = i + 1
            while j < len(old) and old[j] != "}":
                j += 1
            # Keep the doc comment that precedes the signature.
            k = i
            while k > 0 and (old[k - 1].startswith(" *") or
                             old[k - 1].startswith("/**")):
                k -= 1
            existing.append((m.group(1), "\n".join(old[k:j + 1])))
            i = j + 1

    have = {n for n, _ in bodies}
    if args.from_list:
        # Rebuilding from a recorded list must produce EXACTLY that list.
        # Carrying forward makes the file grow on every replay (1059 -> 2118
        # when this was first measured), so the output depends on what was
        # already there - which is the non-reproducibility this flag exists
        # to remove.
        carried = []
    else:
        carried = [(n, c) for n, c in existing if n not in have]
    if carried:
        print(f"  carrying forward {len(carried)} previously seeded function(s)")
    bodies = carried + bodies

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

    if args.record:
        # Sorted, so the same set always produces the same file. This is the
        # artefact that makes a rebuild reproducible: without it, which
        # addresses got seeded depended on the order the linker complained,
        # and a regeneration could not reproduce the tree it replaced.
        rec = os.path.join(GAME_DIR, args.record)
        addrs = sorted(int(n[4:], 16) for n, _ in bodies)
        with open(rec, "w", encoding="utf-8", newline="") as f:
            json.dump({"count": len(addrs), "addresses": addrs}, f, indent=1)
        print("recorded %d address(es) -> %s" % (len(addrs), args.record))
        print("rebuild this exact set with:")
        print("  py -3 tools_data/seed_missing_functions.py "
              "--from-list %s --apply" % args.record)

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
