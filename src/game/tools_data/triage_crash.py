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
    m = re.search(r"\[CRASH\][^\n]*?fault addr=0x([0-9A-Fa-f]+) \((read|write)\)", text)
    if not m:
        return None
    crash = {"access": m.group(2)}
    # RVA is only present - and only meaningful - when RIP is inside our image.
    rva = re.search(r"\[CRASH\][^\n]*RVA=0x([0-9A-Fa-f]+)\)", text)
    crash["rva"] = int(rva.group(1), 16) if rva else None
    ext = re.search(r"\[CRASH\][^\n]*NOT in this image - inside ([^;]+);", text)
    crash["foreign_module"] = ext.group(1).strip() if ext else None
    # The crash handler's own stack walk, which names the recompiled caller
    # when the fault itself happened inside a system DLL.
    tail = text.split("[CRASH]", 1)[1] if "[CRASH]" in text else ""
    # Prefer the real frame walk; the raw scan below it contains stale
    # return addresses and has already misled one investigation.
    if "Call stack, innermost first" in tail:
        seg = tail.split("Call stack, innermost first", 1)[1]
        seg = seg.split("Raw stack scan", 1)[0]
        crash["stack_is_real"] = True
    else:
        seg = tail.split("Native stack", 1)
        seg = seg[1] if len(seg) > 1 else ""
        crash["stack_is_real"] = False
    crash["stack"] = [int(x, 16) for x in re.findall(r"RVA 0x([0-9A-Fa-f]+)", seg)]
    va = re.search(r"Xbox VA of fault: 0x([0-9A-Fa-f]+)", text)
    crash["fault_va"] = int(va.group(1), 16) if va else None
    regs = {}
    for rm in re.finditer(r"\b(e[a-z][a-z])=0x([0-9A-Fa-f]{8})", text):
        regs[rm.group(1)] = int(rm.group(2), 16)
    crash["regs"] = regs
    crash["kernel_calls"] = len(re.findall(r"\[KERNEL\] #", text))
    crash["icall_fails"] = re.findall(r"Failed to resolve VA 0x([0-9A-Fa-f]+)", text)
    crash["icalls"] = parse_icalls(text)
    crash["esp_escape"] = find_esp_escape(text)
    return crash


STACK_LO, STACK_HI = 0x00780000, 0x00F80000


def find_esp_escape(text):
    """First kernel call whose esp is outside the 8 MB simulated stack.

    The bridge already logs esp on every kernel call, so the moment the
    simulated stack pointer leaves its region is sitting in the log for free -
    it just was never checked. Finding it by hand with awk located a runaway
    between calls #70 and #71 in one go, which is exactly the sort of thing
    that should not need re-deriving.

    Returns (call_number, last_good_esp, first_bad_esp) or None.
    """
    calls = re.findall(r"\[KERNEL\] #(\d+):[^\n]*esp=0x([0-9A-Fa-f]+)", text)
    prev = None
    for num, esp in calls:
        v = int(esp, 16)
        if not (STACK_LO <= v < STACK_HI):
            return int(num), prev, v
        prev = v
    return None


def parse_icalls(text):
    """Pair each ICALL failure with the backtrace RVAs logged beneath it.

    recomp_icall_fail_log() already captures a native stack, but prints it as
    raw RVAs - resolving them by hand against build/*.map was the slow step.
    """
    out = []
    blocks = re.split(r"\n(?=\[)", text)
    for b in blocks:
        m = re.match(r"\[ICALL\] Failed to resolve VA 0x([0-9A-Fa-f]+)", b)
        if not m:
            continue
        stack = b.split("Native call stack", 1)
        rvas = ([int(x, 16) for x in re.findall(r"RVA 0x([0-9A-Fa-f]+)", stack[1])]
                if len(stack) > 1 else [])
        out.append({"va": int(m.group(1), 16), "stack": rvas})
    return out


# ── symbolisation ───────────────────────────────────────────

def load_map():
    """Every symbol in the linker map, sorted by address.

    An MSVC map has TWO symbol sections: "Publics by Value", then - after the
    "entry point at" line - "Static symbols". This used to stop at that line
    and read only the first, which in this build means 30,582 of 55,789
    symbols. The other 25,207 did not simply go missing: symbolise() picks the
    nearest PRECEDING known symbol, so every address inside an unlisted
    function was silently attributed to whatever public happened to sit below
    it, complete with a plausible-looking offset.

    That is how a backtrace came to read `sub_001A016A+0x1CB` for a function
    that is only 0x60 bytes long. Offsets past the end of a function are the
    tell, so symbolise() now flags them rather than printing them straight.
    """
    files = glob.glob(MAP_GLOB)
    if not files:
        return []
    lines = open(files[0], encoding="utf-8", errors="ignore").readlines()

    syms, in_section = [], False
    for l in lines:
        if "Publics by Value" in l or "Static symbols" in l:
            in_section = True
            continue
        if not in_section:
            continue
        if "entry point at" in l:
            in_section = False          # the Static symbols header re-arms it
            continue
        parts = l.split()
        if len(parts) < 3:
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
    size = (nxt - addr) if nxt else None
    off = IMAGE_BASE + rva - addr
    # An offset past the next symbol means the real owner is not in the map.
    # Say so rather than printing a confident, wrong name+offset.
    #
    # THE OFFSET NAMES A FUNCTION, NEVER AN INSTRUCTION. The native body is far
    # larger than the original x86 and the two do not correspond, so "+0x24D"
    # cannot be mapped to a particular line or a particular store. Deriving an
    # object base from an assumed field offset that way produced three wrong
    # conclusions on 2026-09-04 alone (ledger #212, #227, and twice more the
    # same evening). To identify a specific instruction, put a probe on the
    # candidate and condition it on the actual address - and let it fail to
    # fire, which is itself the answer.
    return {"name": name, "offset": off, "start": addr, "size": size,
            "uncertain": size is not None and off >= size}


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


def triage_hang(path):
    """Report a watchdog hang the way parse_crash reports a fault."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        print(f"cannot read {path}: {e}")
        return 1

    if "[WATCHDOG]" not in text:
        print(f"no [CRASH] block and no [WATCHDOG] line in {path} -")
        print("the run neither faulted nor hung; check it started at all.")
        return 1

    kern = len(re.findall(r"\[KERNEL\] #", text))
    heap = len(re.findall(r"\[HEAP\] #", text))
    print(f"progress     : {kern} kernel calls, {heap} heap allocs before the hang")

    m = re.search(r"\[WATCHDOG\][^\n]*No progress after (\d+) ms[^\n]*", text)
    if m:
        print(f"ending       : HANG - watchdog fired after {m.group(1)} ms")
    else:
        print("ending       : HANG - watchdog fired")

    m = re.search(r"Total ICALL dispatches: (\d+)", text)
    if m:
        n = int(m.group(1))
        print(f"dispatches   : {n:,} indirect calls")
        print("               a spin inflates this without bound - it counts")
        print("               dispatches, not work done. Read it next to the")
        print("               kernel-call and heap counts above.")

    # The ring buffer is the last 16 targets before the watchdog fired, so a
    # repeated or nonsensical run of them names the loop.
    # Anchor on the last "No progress after" line, NOT the last "[WATCHDOG]"
    # line. The watchdog prints several [WATCHDOG] lines after the ring dump
    # (the RIP capture, or the hint when it is disabled), so anchoring on the
    # last one truncated the tail to that single line and silently dropped the
    # ICALL ring and the whole stack from this report.
    anchor = text.rfind("No progress after")
    if anchor < 0:
        anchor = text.rfind("[WATCHDOG]")
    tail = text[anchor:]
    targets = re.findall(r"\[\s*\d+\]\s+0x([0-9A-Fa-f]{8})", tail)
    if targets:
        print("\nlast ICALL targets before the hang (most recent last):")
        syms = load_map()
        for t in targets:
            va = int(t, 16)
            note = ""
            if va < 0x10000:
                note = "  <- not a code address"
            elif 0xFE000000 <= va < 0xFE010000:
                note = f"  <- kernel thunk slot {(va - 0xFE000000) // 4}"
            elif va >= 0x80000000:
                note = "  <- garbage (often instruction bytes read as a pointer)"
            print(f"    0x{va:08X}{note}")

    # The hung thread's own RIP, when the capture was enabled. This is the
    # single most useful line in a hang report - it names the spinning
    # function outright - so symbolise it before the stack.
    #
    # Accept `RVA=0x` as well as `RVA 0x`: the watchdog prints the former and
    # the crash path prints the latter, and matching only one silently dropped
    # every watchdog stack from this report.
    syms = load_map()
    m = re.search(r"\[WATCHDOG\] RIP=0x([0-9A-Fa-f]+) \(RVA=0x([0-9A-Fa-f]+)\)", tail)
    if m:
        rva = int(m.group(2), 16)
        s = symbolise(syms, rva)
        print("\nHUNG AT       : ", end="")
        if s and not s.get("uncertain"):
            print(f"{s['name']}+0x{s['offset']:X}  (RVA 0x{rva:X})")
            lo, hi = find_source(s["name"]) if s else (None, None)
            if lo:
                print(f"source        : lines {lo}..{hi}")
        else:
            print(f"RVA 0x{rva:X} - outside this image (a system DLL). "
                  "The recompiled frame is in the stack below.")

    rvas = re.findall(r"RVA[= ]0x([0-9A-Fa-f]+)", tail)
    if rvas:
        print("\nnative call stack at the hang (innermost first):")
        seen = set()
        for r in rvas:
            rva = int(r, 16)
            if rva in seen:
                continue
            seen.add(rva)
            s = symbolise(syms, rva)
            print(f"    RVA 0x{rva:08X}  "
                  + (f"{s['name']}+0x{s['offset']:X}" if s else "(unresolved)"))

    print("\nA hang has no faulting address, so there is nothing to guard.")
    if m:
        print("The RIP above names the loop - read that function and find what")
        print("its exit condition depends on.")
    else:
        print("Capture the hung thread's RIP, which names the loop outright:")
        print("    set RECOMP_HANG_RIP=1     (cmd)   or")
        print("    $env:RECOMP_HANG_RIP=1    (PowerShell)")
        print("then re-run and triage again. No rebuild needed. Expect a")
        print("secondary access violation after the dump - the diagnostics")
        print("print first, so it is harmless.")
    return 0


def report_where(path, only_tag=""):
    """Resolve the backtraces printed by recomp_where() into function names.

    recomp_where emits raw RVAs because the runtime has no symbol table. That
    made every use of it a two-step job - run, then resolve by hand against
    build/*.map - which is exactly the manual step this file exists to remove.
    """
    if not os.path.exists(path):
        print(f"no {path} - run first")
        return 1

    text = open(path, encoding="utf-8", errors="replace").read()
    syms = load_map()
    if not syms:
        print("no build/*.map - cannot resolve; is this a Release build?")
        return 1

    # Walked line by line rather than matched with one multi-line regex: the
    # block is a header followed by an indented run, which is trivial to walk
    # and horrible to express as a pattern.
    head = re.compile(r"^\[WHERE:(\w+)\] #(\d+)\s+(.*)$")
    frame = re.compile(r"^\s+\[\s*(\d+)\] RVA 0x([0-9A-Fa-f]+)\s*$")

    shown, seen_any, in_block, keep = 0, False, False, False
    for line in text.splitlines():
        m = head.match(line)
        if m:
            seen_any = True
            tag, n, vals = m.group(1), m.group(2), m.group(3).strip()
            in_block, keep = True, (not only_tag or tag == only_tag)
            if keep:
                shown += 1
                print("")
                print("[%s] call #%s   values: %s" % (tag, n, vals))
            continue
        if in_block:
            f = frame.match(line)
            if not f:
                in_block = False
                continue
            if not keep:
                continue
            rva = int(f.group(2), 16)
            sym = symbolise(syms, rva)
            name = sym["name"] if sym else "?"
            off = "+0x%X" % sym["offset"] if sym and sym["offset"] else ""
            warn = "   (?? past end - real owner not in map)" \
                if sym and sym.get("uncertain") else ""
            print("    [%2d] %s%s%s" % (int(f.group(1)), name, off, warn))

    if not seen_any:
        print("no [WHERE:...] blocks - add a probe with "
              "add_probe.py --where, then rebuild and run")
        return 1

    # The exit summary is the only place a capped probe reports its true
    # total, so surface it here rather than making the reader go find it.
    tail = re.search(r"^\[WHERE\] final counts:$", text, re.M)
    if tail:
        print("")
        print("final counts:")
        for line in text[tail.end():].splitlines():
            if not line.startswith("  "):
                break
            print(line)

    if not shown:
        print("no blocks tagged %r" % only_tag)
        return 1
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--file", default=DEFAULT_LOG)
    ap.add_argument("--grep", action="store_true",
                    help="grep the function for the derived expression")
    ap.add_argument("--icall", action="store_true",
                    help="resolve each failed indirect call's backtrace to a caller")
    ap.add_argument("--where", metavar="TAG", nargs="?", const="",
                    help="resolve recomp_where probe backtraces to function "
                         "names; optionally filter to one TAG")
    args = ap.parse_args(argv)

    if args.where is not None:
        return report_where(args.file, args.where)

    crash = parse_crash(args.file)
    if not crash:
        # No crash is not the same as nothing to report. Since the boot got
        # deep enough to hang instead of fault, hangs are the common ending -
        # and this used to just print "no [CRASH] block" and give up, so the
        # watchdog's backtrace had to be resolved by hand every time.
        return triage_hang(args.file)

    print(f"progress     : {crash['kernel_calls']} kernel calls before the crash")
    esc = crash.get("esp_escape")
    if esc:
        num, good, bad = esc
        print(f"STACK ESCAPE : esp left the 8 MB stack at kernel call #{num}"
              + (f"  (0x{good:08X} -> 0x{bad:08X})" if good else f"  (0x{bad:08X})"))
        print("               everything after this runs on a corrupt frame -")
        print("               guards placed downstream fight the symptom, not")
        print("               the cause. Find what runs away here first.")
    print(f"access       : {crash['access']} at Xbox VA 0x{crash['fault_va']:08X}")

    syms = load_map()

    if crash["foreign_module"]:
        print(f"faulted in   : {crash['foreign_module']}")
        print("               (not our code - a bad pointer was handed to a")
        print("                library routine; the caller is in the stack below)")
        if crash["stack"]:
            print("\ncallers (nearest first):")
            seen = set()
            for rva in crash["stack"]:
                s = symbolise(syms, rva)
                if s and s["name"].startswith("sub_") and s["name"] not in seen:
                    seen.add(s["name"])
                    print(f"  {s['name']} + 0x{s['offset']:X}")
        print()
        print("registers:")
        for name, val in sorted(crash["regs"].items()):
            print(f"  {name} = 0x{val:08X}   {classify(val)}")
        if crash["fault_va"] is not None:
            print(f"\nfault was at Xbox VA 0x{crash['fault_va']:08X}"
                  f"  {classify(crash['fault_va'])}")
        return 0

    # A fault inside a system DLL has no real frame walk to offer, because the
    # crash handler only emits "Call stack, innermost first" when the faulting
    # RIP is inside our image. Printing nothing in that case is what sent one
    # investigation to the raw stack scan instead, which is full of stale
    # return addresses and produced a call chain that had to be withdrawn.
    # So: still refuse to present the scan as callers, but SAY that is what
    # is happening, and name the honest alternative.
    if crash["stack"] and crash.get("stack_is_real"):
        print("callers (nearest first):")
        seen = set()
        for rva in crash["stack"]:
            s = symbolise(syms, rva)
            if s and s["name"].startswith("sub_") and s["name"] not in seen:
                seen.add(s["name"])
                print("  " + s["name"] + " + 0x%X" % s["offset"])
        print()
    elif crash["stack"]:
        print("callers: NOT SHOWN - this run has no real frame walk, only a")
        print("         raw stack scan, which carries stale return addresses.")
        print("         Reading a call chain off it has already produced a")
        print("         wrong answer once. To name the caller, put a probe at")
        print("         the suspect function and call recomp_where(), which")
        print("         walks real frames; resolve them with resolve_rva.py.")
        print()

    sym = symbolise(syms, crash["rva"]) if crash["rva"] is not None else None
    if sym:
        pct = f"{100.0 * sym['offset'] / sym['size']:.0f}%" if sym["size"] else "?"
        print(f"function     : {sym['name']} + 0x{sym['offset']:X}"
              + (f"  (~{pct} through)" if sym["size"] else ""))
        path, lo, hi, lines = find_source(sym["name"])
        if path and sym["size"]:
            approx = lo + int((hi - lo) * sym["offset"] / sym["size"])
            print(f"source (ESTIMATE - the offset names the function, not the line): {os.path.basename(path)} lines {lo}..{hi}, "
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
        if args.icall:
            for c in crash["icalls"]:
                print(f"\n  target 0x{c['va']:08X}   {classify(c['va'])}")
                for rva in c["stack"]:
                    s = symbolise(syms, rva)
                    if s and s["name"].startswith("sub_"):
                        print(f"    called from {s['name']} + 0x{s['offset']:X}")
                if not c["stack"]:
                    print("    (no backtrace logged for this one)")
        else:
            print("  " + ", ".join("0x" + v.upper() for v in crash["icall_fails"][-6:]))
            print("  (--icall resolves each failure's backtrace to a caller;")
            print("   classify targets with whatis.py - a clean disassembly means")
            print("   a genuinely missing function, nonsense means a bad pointer)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
