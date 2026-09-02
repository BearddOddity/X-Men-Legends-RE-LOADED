#!/usr/bin/env python3
"""vtab25.py - read a class vtable out of an Alchemy 2.5 DLL by its exported
??_7 vftable symbol, and name each slot from the export table.

The Alchemy 2.5 DLLs export both the vftable symbol and most virtual methods,
so slot order does not have to be inferred from headers - it can be read from
the binary that the compiler actually produced.

    ./vtab25.py libIGSg.dll igNode@Sg@Gap
    ./vtab25.py libIGSg.dll --list-vftables
"""
import argparse
import sys

import pefile


def demangle_short(sym):
    """The bare method name out of an MSVC mangled symbol.

    Enough to rename a vfuncN slot. Full signature decoding needs undname.
    """
    if not sym.startswith("?"):
        return sym
    if sym.startswith("??_7"):
        return "vftable"
    special = {
        "??0": "ctor", "??1": "dtor",
        "??_G": "scalar_deleting_dtor", "??_E": "vector_deleting_dtor",
        "??_D": "vbase_dtor", "??_F": "default_ctor_closure",
        "??4": "operator=", "??2": "operator new", "??3": "operator delete",
        "??8": "operator==", "??9": "operator!=",
        "??A": "operator[]", "??B": "operator_cast", "??C": "operator->",
        "??6": "operator<<", "??5": "operator>>",
    }
    for pfx, nm in special.items():
        if sym.startswith(pfx):
            rest = sym[len(pfx):]
            cls = rest.split("@")[0] if rest else ""
            return "%s::%s" % (cls, nm) if cls else nm
    body = sym[1:]
    return body.split("@")[0] or sym


class Image:
    def __init__(self, path):
        self.pe = pefile.PE(path, fast_load=True)
        self.pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.by_name = {}
        self.by_rva = {}
        exp = getattr(self.pe, "DIRECTORY_ENTRY_EXPORT", None)
        if exp:
            for s in exp.symbols:
                if not s.name:
                    continue
                n = s.name.decode("ascii", "replace")
                self.by_name[n] = s.address
                self.by_rva.setdefault(s.address, []).append(n)

    def u32(self, rva):
        return int.from_bytes(self.pe.get_data(rva, 4), "little")

    def is_code(self, rva):
        for s in self.pe.sections:
            if s.VirtualAddress <= rva < s.VirtualAddress + max(s.Misc_VirtualSize,
                                                               s.SizeOfRawData):
                return bool(s.Characteristics & 0x20000000)  # MEM_EXECUTE
        return False


def read_vtable(img, vft_rva, limit=4096):
    """Slots from vft_rva until something that is not a code pointer."""
    slots = []
    rva = vft_rva
    while len(slots) < limit:
        try:
            va = img.u32(rva)
        except Exception:
            break
        if va == 0:
            break
        target = va - img.base
        if not img.is_code(target):
            break
        # Another exported vftable starting here means this one ended.
        if rva != vft_rva:
            names = img.by_rva.get(rva, [])
            if any(n.startswith("??_7") for n in names):
                break
        slots.append(target)
        rva += 4
    return slots


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dll")
    ap.add_argument("cls", nargs="?", help="e.g. igNode@Sg@Gap")
    ap.add_argument("--list-vftables", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()

    img = Image(a.dll)

    if a.list_vftables:
        for n in sorted(n for n in img.by_name if n.startswith("??_7")):
            print("%08x  %s" % (img.by_name[n], n))
        return

    if not a.cls:
        ap.error("give a class, or --list-vftables")

    sym = "??_7%s@@6B@" % a.cls
    if sym not in img.by_name:
        cand = [n for n in img.by_name
                if n.startswith("??_7") and a.cls.split("@")[0] in n]
        print("no exported vftable %r" % sym, file=sys.stderr)
        for c in cand[:15]:
            print("  did you mean: %s" % c, file=sys.stderr)
        sys.exit(2)

    slots = read_vtable(img, img.by_name[sym])
    # Identical small functions get folded by the linker, so one RVA can carry
    # several unrelated exported names. Prefer a name that belongs to this
    # class; otherwise say the slot is ambiguous rather than picking a stranger.
    owner = "@%s@@" % a.cls
    out = open(a.out, "w") if a.out else sys.stdout
    print("# %s  %d slots" % (sym, len(slots)), file=out)
    for i, rva in enumerate(slots):
        names = img.by_rva.get(rva, [])
        mine = [n for n in names if owner in n]
        if mine:
            pick, tag = sorted(mine, key=len)[0], "own"
        elif names:
            pick, tag = sorted(names, key=len)[0], "folded?"
        else:
            pick, tag = "", "-"
        print("%d\t%08x\t%s\t%s\t%s" % (i, rva, demangle_short(pick) if pick else "-",
                                        tag, pick), file=out)
    if a.out:
        out.close()
        print("wrote %s (%d slots)" % (a.out, len(slots)))


if __name__ == "__main__":
    main()
