"""
triage_crash.py - turn a crash dump in stderr.txt into a starting point.

Every crash this session has been triaged the same way by hand: resolve the
native RVA to a function via the linker map, work out roughly where in the
generated C that lands, classify each register, and - the step that actually
cracks most of them - notice that the faulting address is some register plus a
small constant, which names the faulting expression.

That last step found the current blocker in one line: fault 0xFFFFFFFD with
eax = 0xFFFFFFF8 is `eax + 5`, and the only `MEM8(eax + 5)` reads in the
function are the crash site.

Usage (from src/game/):
    py -3 tools_data/triage_crash.py                 # reads stderr.txt
    py -3 tools_data/triage_crash.py -f other.txt
    py -3 tools_data/triage_crash.py --grep          # also grep the function
                                                     # for the derived expression
"""
import argparse
import bisect
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
DEFAULT_LOG = os.path.join(GAME_DIR, "stderr.txt")
MAP_GLOB = os.path.join(GAME_DIR, "build", "*.map")
GEN_GLOB = os.path.join(GAME_DIR, "src", "recomp", "gen", "recomp_*.c")

IMAGE_BASE = 0x140000000

# Runtime regions, mirroring src/kernel/xbox_memory_layout.h.
REGIONS = [
    (0x00011000, 0x0035AD94, "code (.text)"),
    (0x0035ADA0, 0x0036F2E8, "code (D3D lib)"),
    (0x003C6BA0, 0x0044A56C, "rodata (.rdata)"),
    (0x0044A580, 0x006F12FC, "data (.data)"),
    (0x00780000, 0x00F80000, "STACK"),
    (0x00F80000, 0x04000000, "heap"),
    (0xFE000000, 0xFE001000, "kernel thunk"),
]

FILL_PATTERNS = {
    0xCCCCCCCC: "int3 padding - read past a function, or through a `ret`-only stub",
    0xCDCDCDCD: "uninitialised heap fill",
    0xFEEEFEEE: "freed heap fill",
    0xBAADF00D: "uninitialised allocation fill",
}


# ── crash-dump parsing ──────────────────────────────────────

def parse_crash(path):
    text = open(path, encoding="utf-8", errors="ignore").read()
    m = re.search(
        r"\[CRASH\][^\n]*RVA=0x([0-9A-Fa-f]+)\), fault addr=0x([0-9A-Fa-f]+) \((read|write)\)",
        text)
    if not m:
        return None
    crash = {"rva": int(m.group(1), 16), "access": m.group(3)}
    va = re.search(r"Xbox VA of fault: 0x([0-9A-Fa-f]+)", text)
    crash["fault_va"] = int(va.group(1), 16) if va else None
    regs = {}
    for rm in re.finditer(r"\b(e[a-z][a-z])=0x([0-9A-Fa-f]{8})", text):
        regs[rm.group(1)] = int(rm.group(2), 16)
    crash["regs"] = regs
    crash["kernel_calls"] = len(re.findall(r"\[KERNEL\] #", text))
    crash["icall_fails"] = re.findall(r"Failed to resolve VA 0x([0-9A-Fa-f]+)", text)
    return crash


# ── symbolisation ───────────────────────────────────────────

def load_map():
    files = glob.glob(MAP_GLOB)
    if not files:
        return []
    lines = open(files[0], encoding="utf-8", errors="ignore").readlines()
    try:
        start = next(i for i, l in enumerate(lines) if "Publics by Value" in l)
    except StopIteration:
        return []
    syms = []
    for l in lines[start + 2:]:
        if not l.strip():
            continue
        if "entry point at" in l:
            break
        parts = l.split()
        if len(parts) < 4:
            continue
        try:
            syms.append((int(parts[2], 16), parts[1]))
        except ValueError:
            pass
    syms.sort()
    return syms


def symbolise(syms, rva):
    if not syms:
        return None
    keys = [a for a, _ in syms]
    i = bisect.bisect_right(keys, IMAGE_BASE + rva) - 1
    if i < 0:
        return None
    addr, name = syms[i]
    nxt = syms[i + 1][0] if i + 1 < len(syms) else None
    return {"name": name, "offset": IMAGE_BASE + rva - addr,
            "start": addr, "size": (nxt - addr) if nxt else None}


def find_source(name):
    """Locate a generated function's source line range."""
    for path in sorted(glob.glob(GEN_GLOB)):
        lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
        for i, l in enumerate(lines):
            if l == f"void {name}(void)":
                end = next((j for j in range(i + 1, len(lines))
                            if re.match(r"^void sub_[0-9A-F]+\(void\)$", lines[j])), len(lines))
                return path, i + 1, end, lines
    return None, None, None, None


# ── analysis ────────────────────────────────────────────────

def classify(value):
    for pat, why in FILL_PATTERNS.items():
        if value == pat:
            return f"FILL PATTERN - {why}"
    for lo, hi, label in REGIONS:
        if lo <= value < hi:
            return label
    if value == 0:
        return "NULL"
    if value >= 0xFFFFF000:
        return f"NEGATIVE ({value - (1 << 32)}) - likely `ptr - N` computed from NULL"
    return "unmapped / garbage"


def derive_expression(fault, regs):
    """Find registers where fault = reg + small offset - names the expression."""
    out = []
    for name, val in sorted(regs.items()):
        delta = (fault - val) & 0xFFFFFFFF
        if delta <= 0x200:
            out.append((name, delta))
        delta_neg = (val - fault) & 0xFFFFFFFF
        if 0 < delta_neg <= 0x200:
            out.append((name, -delta_neg))
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--file", default=DEFAULT_LOG)
    ap.add_argument("--grep", action="store_true",
                    help="grep the function for the derived expression")
    args = ap.parse_args(argv)

    crash = parse_crash(args.file)
    if not crash:
        print(f"no [CRASH] block found in {args.file}")
        return 1

    print(f"progress     : {crash['kernel_calls']} kernel calls before the crash")
    print(f"access       : {crash['access']} at Xbox VA 0x{crash['fault_va']:08X}")

    syms = load_map()
    sym = symbolise(syms, crash["rva"])
    if sym:
        pct = f"{100.0 * sym['offset'] / sym['size']:.0f}%" if sym["size"] else "?"
        print(f"function     : {sym['name']} + 0x{sym['offset']:X}"
              + (f"  (~{pct} through)" if sym["size"] else ""))
        path, lo, hi, lines = find_source(sym["name"])
        if path and sym["size"]:
            approx = lo + int((hi - lo) * sym["offset"] / sym["size"])
            print(f"source       : {os.path.basename(path)} lines {lo}..{hi}, "
                  f"crash near line ~{approx}")
    print()

    print("registers:")
    for name, val in sorted(crash["regs"].items()):
        print(f"  {name} = 0x{val:08X}   {classify(val)}")
    print()

    if crash["fault_va"] is not None:
        derived = derive_expression(crash["fault_va"], crash["regs"])
        if derived:
            print("faulting expression (fault address relative to a register):")
            for name, off in derived:
                sign = "+" if off >= 0 else "-"
                print(f"  fault == {name} {sign} 0x{abs(off):X}"
                      f"   -> look for MEM8/16/32({name} {sign} 0x{abs(off):X})")
            if args.grep and sym:
                path, lo, hi, lines = find_source(sym["name"])
                if lines:
                    print("\n  matches in the function:")
                    for name, off in derived:
                        # gen/ writes small displacements in decimal and large
                        # ones in hex, and folds `+ -N` for negatives.
                        sign = "+" if off >= 0 else "-"
                        forms = "|".join({f"0x{abs(off):X}", f"0x{abs(off):x}",
                                          str(abs(off))})
                        pat = re.compile(
                            rf"MEM(?:8|16|32)\({name} \+ -?(?:{forms})\)"
                            if sign == "-" else
                            rf"MEM(?:8|16|32)\({name} \+ (?:{forms})\)")
                        for i in range(lo - 1, hi):
                            if pat.search(lines[i]):
                                print(f"    {i+1}: {lines[i].strip()}")
        else:
            print("faulting address is not near any register - "
                  "the base was probably computed and discarded")

    if crash["icall_fails"]:
        print(f"\nfailed indirect calls this run: {len(crash['icall_fails'])}")
        print("  " + ", ".join("0x" + v.upper() for v in crash["icall_fails"][-6:]))
        print("  (classify each with whatis.py - a clean disassembly means a")
        print("   genuinely missing function; nonsense means a bad pointer)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
