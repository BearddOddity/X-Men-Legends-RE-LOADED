"""
whatis.py - answer "what is this Xbox VA?" for one or more addresses.

Debugging the recompile constantly turns up a raw 32-bit value and the first
question is always the same: is it a heap pointer, a code address, read-only
static data, a stack address, or garbage? Doing that by hand means re-parsing
the XBE section table and grepping the generated sources every time.

Usage (from src/game/):
    py -3 tools_data/whatis.py 0x0013D370 0x003F3638 0xCCCCCCCC
    py -3 tools_data/whatis.py 13D370            # 0x prefix optional

For each address it reports:
  - which XBE section it lands in (or which runtime region: stack/heap/etc.)
  - whether it is inside a known recompiled function (and the offset into it)
  - for code addresses, the first few disassembled instructions
  - for .rdata/.data addresses, a hexdump plus an ASCII rendering, since these
    are usually vtables, descriptor structs, or string constants

Runtime region bounds mirror src/kernel/xbox_memory_layout.h. If that header
changes, update REGIONS below.
"""
import sys
import os
import re
import struct
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.dirname(HERE)
XBE = os.path.join(GAME_DIR, "game", "default.xbe")
GEN_GLOB = os.path.join(GAME_DIR, "src", "recomp", "gen", "recomp_*.c")

# Runtime-only regions (not XBE sections). See xbox_memory_layout.h.
REGIONS = [
    (0x00780000, 0x00F80000, "STACK (8MB simulated Xbox stack)"),
    (0x00F80000, 0x04000000, "HEAP (bump allocator)"),
    (0xFE000000, 0xFE001000, "KERNEL THUNK (synthetic VAs)"),
]


def load_sections():
    with open(XBE, "rb") as fh:
        data = fh.read()
    base = struct.unpack_from("<I", data, 0x104)[0]
    count = struct.unpack_from("<I", data, 0x11C)[0]
    hdr = struct.unpack_from("<I", data, 0x120)[0] - base
    secs = []
    for i in range(count):
        off = hdr + i * 56
        _flags, va, vsize, raw, rawsize, nameaddr = struct.unpack_from("<IIIIII", data, off)
        name_off = nameaddr - base
        name = data[name_off:name_off + 16].split(b"\x00")[0].decode("latin1", "replace")
        secs.append({"name": name, "va": va, "vsize": vsize, "raw": raw, "rawsize": rawsize})
    return data, secs


def find_section(secs, va):
    for s in secs:
        if s["va"] <= va < s["va"] + s["vsize"]:
            return s
    return None


def file_offset(sec, va):
    """File offset for a VA, or None if it lands in BSS (past raw data)."""
    delta = va - sec["va"]
    return sec["raw"] + delta if delta < sec["rawsize"] else None


_FUNC_RE = re.compile(
    r"^\s\*\s(sub_[0-9A-F]+)\n(?:\s\*[^\n]*\n)*?\s\*\sOriginal:\s0x([0-9A-F]+)\s-\s0x([0-9A-F]+)",
    re.MULTILINE,
)


def load_functions():
    """Map recompiled functions to (start, end, name) from the gen/ headers."""
    funcs = []
    for path in glob.glob(GEN_GLOB):
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
        for m in _FUNC_RE.finditer(content):
            funcs.append((int(m.group(2), 16), int(m.group(3), 16),
                          m.group(1), os.path.basename(path)))
    funcs.sort()
    return funcs


def find_function(funcs, va):
    lo, hi = 0, len(funcs) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end, _name, _f = funcs[mid]
        if va < start:
            hi = mid - 1
        elif va >= end:
            lo = mid + 1
        else:
            return funcs[mid]
    return None


def describe(va, data, secs, funcs):
    print(f"\n=== 0x{va:08X} ===")

    for lo, hi, label in REGIONS:
        if lo <= va < hi:
            print(f"  region : {label}")
            print(f"  offset : +0x{va - lo:X} into region")
            return

    sec = find_section(secs, va)
    if sec is None:
        print("  region : NOT MAPPED - garbage / uninitialized")
        if va in (0xCCCCCCCC, 0xCDCDCDCD, 0xFEEEFEEE, 0xBAADF00D):
            print("  note   : classic uninitialized-memory fill pattern")
        return

    delta = va - sec["va"]
    is_bss = delta >= sec["rawsize"]
    print(f"  section: {sec['name']}  (+0x{delta:X})" + ("  [BSS - zero-init, no file data]" if is_bss else ""))

    hit = find_function(funcs, va)
    if hit:
        start, end, name, srcfile = hit
        tag = "start of" if va == start else f"+0x{va - start:X} into"
        print(f"  code   : {tag} {name}   ({srcfile}, 0x{start:08X}-0x{end:08X})")
    elif sec["name"] == ".text":
        print("  code   : in .text but NOT a known recompiled function")
        print("           (an ICALL to this VA would fail to resolve)")

    off = file_offset(sec, va)
    if off is None:
        return

    if sec["name"] == ".text" or hit:
        try:
            import capstone
        except ImportError:
            return
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        print("  disasm :")
        for i, insn in enumerate(md.disasm(data[off:off + 32], va)):
            print(f"           {insn.address:#010x}: {insn.mnemonic} {insn.op_str}")
            if i >= 5:
                break
    else:
        chunk = data[off:off + 32]
        words = struct.unpack_from("<8I", chunk)
        print("  dwords : " + " ".join(f"{w:08X}" for w in words[:4]))
        print("           " + " ".join(f"{w:08X}" for w in words[4:]))
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  ascii  : {ascii_}")
        # Static tables here are usually vtables/descriptors - flag code pointers.
        ptrs = [(i * 4, w) for i, w in enumerate(words) if 0x00011000 <= w < 0x0035AD94]
        if ptrs:
            print("  note   : contains .text pointers (likely a vtable or descriptor):")
            for offset, w in ptrs:
                fn = find_function(funcs, w)
                label = fn[2] if fn else "unknown"
                print(f"           +0x{offset:02X} -> 0x{w:08X}  {label}")


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    data, secs = load_sections()
    funcs = load_functions()
    for arg in argv:
        try:
            va = int(arg, 16) if not arg.lower().startswith("0x") else int(arg, 16)
        except ValueError:
            print(f"skipping unparseable address: {arg}")
            continue
        describe(va, data, secs, funcs)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
