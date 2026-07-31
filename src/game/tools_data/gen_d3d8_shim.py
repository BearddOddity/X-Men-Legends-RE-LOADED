"""
gen_d3d8_shim.py - generate src/d3d8_shim.c, the native replacement layer for
the Xbox D3D8 library functions the game calls.

Background
----------
Xbox D3D8 is statically linked into the XBE, so the game calls it by DIRECT
call, not through COM vtables. The recomp pipeline never disassembles the D3D
section, so every one of those entry points currently lands in
recomp_stubs_unresolved.c as a truly empty function:

    void sub_0035D900(void) { /* 0x0035D900: not detected */ }

That is wrong in two separate ways:

1. **Stack leak.** In this recompile model the simulated stack is explicit:
   the caller does `PUSH32(esp, 0)` to push a dummy return address, and the
   callee is responsible for popping it (plus its own __stdcall arguments).
   An empty stub pops nothing, so *every call leaks 4 + N bytes* of simulated
   stack. Across hundreds of calls that silently corrupts the stack.

2. **Stale return value.** The stubs never assign g_eax, so a caller reading
   the "return value" gets whatever the previous function happened to leave
   behind. That is the source of much of the garbage-pointer behaviour seen
   while debugging (objects that are actually vtables, code addresses used as
   `this`, and so on).

This generator emits a shim that fixes both: correct __stdcall cleanup derived
from each function's real `ret N` in the XBE, and an explicit return value.

Target is a native PC port, NOT Xbox emulation - so these are reimplemented
against a modern graphics backend rather than reproducing NV2A behaviour. This
file generates PHASE 1: correct calling convention plus safe neutral returns,
which is what unblocks boot. Real rendering is layered in afterwards by editing
the generated file's bodies (it is generated once, then hand-maintained).

Usage (from src/game/):
    py -3 tools_data/gen_d3d8_shim.py            # writes src/d3d8_shim.c
    py -3 tools_data/gen_d3d8_shim.py --check    # report only, write nothing
"""
import glob
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
XBE = os.path.join(GAME_DIR, "game", "default.xbe")
GEN_GLOB = os.path.join(GAME_DIR, "src", "recomp", "gen", "recomp_*.c")
STUB_FILE = os.path.join(GAME_DIR, "src", "recomp", "gen", "recomp_stubs_unresolved.c")
OUT_FILE = os.path.join(GAME_DIR, "src", "d3d8_shim.c")

D3D_SECTIONS = {"D3D", "D3DX"}


def load_xbe():
    with open(XBE, "rb") as fh:
        data = fh.read()
    base = struct.unpack_from("<I", data, 0x104)[0]
    count = struct.unpack_from("<I", data, 0x11C)[0]
    hdr = struct.unpack_from("<I", data, 0x120)[0] - base
    secs = []
    for i in range(count):
        off = hdr + i * 56
        _f, va, vsize, raw, rawsize, nameaddr = struct.unpack_from("<IIIIII", data, off)
        name = data[nameaddr - base: nameaddr - base + 16].split(b"\x00")[0].decode("latin1", "replace")
        secs.append({"name": name, "va": va, "vsize": vsize, "raw": raw, "rawsize": rawsize})
    return data, secs


def section_of(secs, va):
    for s in secs:
        if s["va"] <= va < s["va"] + s["vsize"]:
            return s
    return None


def find_called_d3d(secs):
    """Every D3D-section address the translated game code actually calls."""
    ranges = [(s["va"], s["va"] + s["vsize"]) for s in secs if s["name"] in D3D_SECTIONS]
    pat = re.compile(r"sub_(00[0-9A-F]{6})\(\)")
    called = {}
    for path in glob.glob(GEN_GLOB):
        if os.path.basename(path) == "recomp_stubs_unresolved.c":
            continue
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.lstrip().startswith("void sub_"):
                    continue
                for m in pat.finditer(line):
                    va = int(m.group(1), 16)
                    if any(lo <= va < hi for lo, hi in ranges):
                        called[va] = called.get(va, 0) + 1
    return called


def stdcall_bytes(data, secs, va, limit=0x1200):
    """
    Determine a function's __stdcall argument cleanup by finding its `ret N`.

    Linear sweep from the entry point, tracking forward branch targets so we
    don't stop at a `ret` that belongs to an earlier basic block than a branch
    still pending. Returns N (bytes of args the callee pops), or None if no
    `ret` was found within `limit` bytes.
    """
    try:
        import capstone
    except ImportError:
        return None
    sec = section_of(secs, va)
    if sec is None:
        return None
    delta = va - sec["va"]
    if delta >= sec["rawsize"]:
        return None
    off = sec["raw"] + delta
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    furthest_branch = va
    for insn in md.disasm(data[off:off + limit], va):
        if insn.mnemonic.startswith("j") and insn.op_str.startswith("0x"):
            try:
                furthest_branch = max(furthest_branch, int(insn.op_str, 16))
            except ValueError:
                pass
        if insn.mnemonic == "ret":
            # Only trust this ret if no forward branch jumps past it.
            if insn.address >= furthest_branch:
                if insn.op_str:
                    try:
                        return int(insn.op_str, 16)
                    except ValueError:
                        return None
                return 0
    return None


def main(argv):
    check_only = "--check" in argv
    data, secs = load_xbe()
    called = find_called_d3d(secs)
    if not called:
        print("No D3D-section calls found - nothing to generate.")
        return 1

    entries = []
    unknown = []
    for va in sorted(called):
        n = stdcall_bytes(data, secs, va)
        sec = section_of(secs, va)
        if n is None:
            unknown.append(va)
        entries.append({"va": va, "ret_imm": n, "calls": called[va],
                        "section": sec["name"] if sec else "?"})

    print(f"D3D entry points called by game code : {len(entries)}")
    print(f"total call sites                     : {sum(e['calls'] for e in entries)}")
    print(f"stdcall cleanup resolved             : {len(entries) - len(unknown)}")
    if unknown:
        print(f"UNRESOLVED (will use safe default)   : {len(unknown)} -> "
              + ", ".join(hex(v) for v in unknown))

    if check_only:
        return 0

    lines = []
    lines.append("/*")
    lines.append(" * d3d8_shim.c - native replacement for the Xbox D3D8 entry points.")
    lines.append(" *")
    lines.append(" * GENERATED SCAFFOLD (tools_data/gen_d3d8_shim.py), then hand-maintained.")
    lines.append(" * Regenerating overwrites hand-written bodies - diff before you do.")
    lines.append(" *")
    lines.append(" * Why this file exists")
    lines.append(" * ---------------------")
    lines.append(" * Xbox D3D8 is statically linked into the XBE and called directly (not via")
    lines.append(" * COM vtables), but the recomp pipeline never disassembles the D3D section.")
    lines.append(" * Those entry points therefore fell through to recomp_stubs_unresolved.c as")
    lines.append(" * genuinely empty functions, which broke two invariants:")
    lines.append(" *")
    lines.append(" *   1. Stack. The caller pushes a dummy return address and the callee must")
    lines.append(" *      pop it plus its own __stdcall arguments. An empty stub popped")
    lines.append(" *      nothing, leaking 4+N bytes of simulated stack on EVERY call.")
    lines.append(" *   2. Return value. An empty stub never sets g_eax, so callers read a")
    lines.append(" *      stale register - the source of many garbage-pointer crashes.")
    lines.append(" *")
    lines.append(" * Each function below performs the correct cleanup (esp += 4 + ret_imm,")
    lines.append(" * matching the real `ret N` in the XBE) and returns an explicit value.")
    lines.append(" *")
    lines.append(" * PHASE 1 (current): correct calling convention + neutral return values.")
    lines.append(" * This is what unblocks boot; nothing renders yet.")
    lines.append(" * PHASE 2: implement real behaviour against a modern PC graphics backend.")
    lines.append(" * This is a PC PORT, not Xbox emulation - do not reproduce NV2A pushbuffer")
    lines.append(" * semantics here; translate to the backend's own state model instead.")
    lines.append(" */")
    lines.append("")
    lines.append("/*")
    lines.append(" * NOTE: deliberately NOT defining RECOMP_GENERATED_CODE. That macro turns on")
    lines.append(" * register aliasing (bare `eax` -> `g_eax`), which is convenient inside")
    lines.append(" * mechanically generated code but hostile in hand-written C - it silently")
    lines.append(" * captures any local named eax/ecx/esp. This file uses the g_-prefixed")
    lines.append(" * globals explicitly, matching recomp_manual.c.")
    lines.append(" */")
    lines.append('#include "recomp_types.h"')
    lines.append('#include "recomp/gen/recomp_funcs.h"  /* declarations, so signatures are checked */')
    lines.append("")
    lines.append("/* Count of shim calls, for diagnostics. */")
    lines.append("unsigned long g_d3d8_shim_calls = 0;")
    lines.append("")
    lines.append("/*")
    lines.append(" * D3D8_SHIM_ENTER / D3D8_SHIM_RET")
    lines.append(" *")
    lines.append(" * ret_imm is the callee's __stdcall argument cleanup, read from the real")
    lines.append(" * `ret N` in the XBE. Total esp adjustment is 4 (dummy return address the")
    lines.append(" * caller pushed) + ret_imm (the arguments).")
    lines.append(" */")
    lines.append("#define D3D8_SHIM_RET(ret_imm, value)  \\")
    lines.append("    do { g_d3d8_shim_calls++;          \\")
    lines.append("         g_eax = (value);              \\")
    lines.append("         g_esp += 4 + (ret_imm); } while (0)")
    lines.append("")
    lines.append("/* S_OK - most D3D8 calls that report status succeed trivially in Phase 1. */")
    lines.append("#define D3D8_OK 0u")
    lines.append("")

    for e in entries:
        va = e["va"]
        n = e["ret_imm"]
        note = ""
        if n is None:
            n = 0
            note = "  /* cleanup UNRESOLVED - assuming cdecl (0); verify if this misbehaves */"
        lines.append(f"/* 0x{va:08X}  [{e['section']}]  {e['calls']} call site(s){note} */")
        lines.append(f"void sub_{va:08X}(void)")
        lines.append("{")
        lines.append(f"    D3D8_SHIM_RET({n}, D3D8_OK);")
        lines.append("}")
        lines.append("")

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\nwrote {OUT_FILE}  ({len(entries)} functions)")

    # The generated definitions collide with the empty stubs unless those are
    # removed. Report which ones the caller still needs to disable.
    with open(STUB_FILE, "r", encoding="utf-8", errors="ignore") as fh:
        stub_src = fh.read()
    clash = [e["va"] for e in entries
             if re.search(rf"^void sub_{e['va']:08X}\(void\) {{", stub_src, re.MULTILINE)]
    print(f"stubs to disable in recomp_stubs_unresolved.c: {len(clash)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
