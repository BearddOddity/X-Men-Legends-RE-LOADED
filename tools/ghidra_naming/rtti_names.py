#!/usr/bin/env python3
"""
Recover C++ class names from the MSVC RTTI embedded in an XBE.

The XBE is a release build with source paths stripped, but it was compiled
with RTTI enabled, so every polymorphic class left a type descriptor holding
its mangled name. Walking from those descriptors to the vtables that
reference them names the vtables, and through them the virtual methods -
turning `sub_00204800` into `CMemory::vf12`.

Ghidra will not do this for us: its MSVC RTTI analyzer is PE-specific and an
XBE is loaded by a custom loader, so the analyzer never applies (confirmed -
`list-analyzers` offers 31 analyzers and none is an RTTI analyzer). The data
is still in standard MSVC layout, so we parse it directly out of the file.

THE STRUCTURES (32-bit MSVC, all little-endian)

    _TypeDescriptor                     the class's name
        +0x00  void*  pVFTable          type_info's vftable
        +0x04  void*  spare             runtime scratch, 0 in the image
        +0x08  char   name[]            ".?AVCMemory@@", NUL-terminated

    _RTTICompleteObjectLocator (COL)    ties a vtable to a type
        +0x00  DWORD  signature         0 on x86
        +0x04  DWORD  offset            this vftable's offset in the class
        +0x08  DWORD  cdOffset
        +0x0C  _TypeDescriptor*         -> the name
        +0x10  _RTTIClassHierarchyDescriptor*

    vtable
        [-4]   COL*                     <- the link we follow backwards
        [ 0]   first virtual method

So the walk is: find name -> find COL pointing at it -> find the pointer to
that COL -> the vtable starts 4 bytes later.

WHY THE VALIDATION MATTERS: a bare "find me a dword equal to X" scan over 7MB
finds coincidences. Every candidate is checked (signature must be zero, the
hierarchy pointer must land in a real section, vtable slots must point into
executable memory) and anything failing is dropped rather than guessed at.
The project has been burned before by a tool that reported plausible-looking
addresses without proving them (see the 25x regression in the progress log,
2026-08-05), so this one reports what it proved and counts what it rejected.

METHOD NAMING IS DELIBERATELY CONSERVATIVE. A function reached through two
different vtables is an inherited or shared implementation, and picking one
class for it would invent a fact. Those are reported as ambiguous and left
unnamed. Only functions appearing in exactly one vtable get a name.

Usage:
    py -3 tools/ghidra_naming/rtti_names.py <path-to.xbe>
    py -3 tools/ghidra_naming/rtti_names.py <path-to.xbe> --json out.json
    py -3 tools/ghidra_naming/rtti_names.py --self-check
"""
import argparse
import json
import os
import re
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.normpath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_REPO, "tools"))

# ---------------------------------------------------------------- structures

COL_SIZE = 0x14
COL_OFF_SIGNATURE = 0x00
COL_OFF_TYPEDESC = 0x0C
COL_OFF_HIERARCHY = 0x10

TYPEDESC_NAME_OFF = 0x08  # name[] sits 8 bytes past the descriptor start

RTTI_PREFIXES = (b".?AV", b".?AU")  # AV = class, AU = struct

# THE XBE's EXECUTABLE FLAG IS USELESS AS A CODE TEST, and trusting it was a
# real bug here, not a theoretical one. Measured on X-Men Legends: every
# section except the two $$X image blobs sets flag bit 2, including `.rdata`
# and `.data`. Reading "is executable" as "is code" therefore let the vtable
# walk run straight off the end of each table into read-only data, which both
# inflated method counts (one class read 41 slots against the sequel's 1) and
# produced 454 "function" candidates that were really data pointers - 57% of
# the list.
#
# So code membership is decided by section NAME instead. These are the XBE's
# code sections: `.text` plus the statically linked XDK libraries. Anything
# else - `.rdata`, `.data`, `DOLBY`, the image blobs - is data no matter what
# its flags claim.
CODE_SECTIONS = {
    ".text", "D3D", "DSOUND", "WMADEC", "D3DX", "XGRPH", "XPP",
    "PSFD00", "PSFD_I", "PSFD_B", "PSFD_P",
}


def demangle(mangled):
    """`.?AVIMemoryPoolInfo@CMemory@@` -> `CMemory::IMemoryPoolInfo`.

    MSVC stores the qualified name innermost-first, so the components are
    reversed on the way out. Anything that does not fit the simple shape is
    returned untouched rather than mangled further by a partial guess.
    """
    if not mangled.startswith(".?A") or len(mangled) < 5:
        return mangled
    body = mangled[4:]
    if body.endswith("@@"):
        body = body[:-2]
    parts = [p for p in body.split("@") if p]
    if not parts:
        return mangled
    return "::".join(reversed(parts))


def sanitize(name):
    """A valid C identifier, matching what merge_names.py expects."""
    out = re.sub(r"[^0-9A-Za-z_]", "_", name)
    if out and out[0].isdigit():
        out = "_" + out
    return out or "unnamed"


# ------------------------------------------------------------------ the walk

class Image:
    """Flat VA-addressable view of the XBE's loaded sections."""

    def __init__(self, sections, data):
        # each entry: (va_start, va_end, file_off, size, name, executable)
        self.sections = sections
        self.data = data

    def va_to_off(self, va):
        for s in self.sections:
            if s["va"] <= va < s["va"] + s["size"]:
                return s["off"] + (va - s["va"])
        return None

    def is_mapped(self, va):
        return self.va_to_off(va) is not None

    def is_code(self, va):
        """True only for real code sections - see CODE_SECTIONS on why the
        XBE's own executable flag cannot be used for this."""
        for s in self.sections:
            if s["va"] <= va < s["va"] + s["size"]:
                return s["name"] in CODE_SECTIONS
        return False

    def u32(self, va):
        off = self.va_to_off(va)
        if off is None or off + 4 > len(self.data):
            return None
        return struct.unpack_from("<I", self.data, off)[0]

    def cstr(self, va, limit=512):
        off = self.va_to_off(va)
        if off is None:
            return None
        end = self.data.find(b"\x00", off, off + limit)
        if end < 0:
            return None
        try:
            return self.data[off:end].decode("ascii")
        except UnicodeDecodeError:
            return None


def load_xbe(path):
    from xbe_parser.xbe_parser import XBEParser

    parser = XBEParser(path)
    xbe = parser.parse()
    data = open(path, "rb").read()

    sections = []
    for s in xbe.sections:
        # bit 2 of the section flags is EXECUTABLE in the XBE header
        sections.append({
            "name": s.name,
            "va": s.virtual_addr,
            "size": min(s.virtual_size, s.raw_size) or s.raw_size,
            "off": s.raw_addr,
            "exec": bool(s.flags & 0x00000004),  # unreliable; see CODE_SECTIONS
        })
    sections = [s for s in sections if s["size"] > 0 and s["off"] > 0]
    return Image(sections, data), xbe


def find_type_descriptors(img):
    """Every `.?AV`/`.?AU` string, mapped back to its descriptor's VA."""
    found = {}  # typedesc_va -> (mangled, demangled)
    for s in img.sections:
        blob = img.data[s["off"]:s["off"] + s["size"]]
        for prefix in RTTI_PREFIXES:
            start = 0
            while True:
                i = blob.find(prefix, start)
                if i < 0:
                    break
                start = i + 1
                end = blob.find(b"\x00", i, i + 512)
                if end < 0:
                    continue
                try:
                    mangled = blob[i:end].decode("ascii")
                except UnicodeDecodeError:
                    continue
                name_va = s["va"] + i
                desc_va = name_va - TYPEDESC_NAME_OFF
                if not img.is_mapped(desc_va):
                    continue
                found[desc_va] = (mangled, demangle(mangled))
    return found


def index_dwords(img):
    """value -> [VAs holding it], over 4-byte-aligned words in every section.

    One pass, because doing a fresh scan per descriptor would be 563 passes
    over 7MB.
    """
    index = {}
    for s in img.sections:
        blob = img.data[s["off"]:s["off"] + s["size"]]
        base = s["va"]
        n = len(blob) & ~3
        for off in range(0, n, 4):
            val = int.from_bytes(blob[off:off + 4], "little")
            if val:
                index.setdefault(val, []).append(base + off)
    return index


def find_cols(img, typedescs, index):
    """COLs whose +0x0C points at a known type descriptor, validated."""
    cols = {}      # col_va -> typedesc_va
    rejected = 0
    for desc_va in typedescs:
        for ptr_va in index.get(desc_va, ()):
            col_va = ptr_va - COL_OFF_TYPEDESC
            sig = img.u32(col_va + COL_OFF_SIGNATURE)
            hier = img.u32(col_va + COL_OFF_HIERARCHY)
            if sig != 0 or not hier or not img.is_mapped(hier):
                rejected += 1
                continue
            cols[col_va] = desc_va
    return cols, rejected


def find_vtables(img, cols, index, max_slots=512):
    """Vtables are the 4 bytes after any pointer to a COL."""
    vtables = {}   # vtable_va -> (col_va, [method VAs])
    rejected = 0
    for col_va in cols:
        for ptr_va in index.get(col_va, ()):
            vt_va = ptr_va + 4
            methods = []
            va = vt_va
            while len(methods) < max_slots:
                slot = img.u32(va)
                if slot is None or not img.is_code(slot):
                    break
                methods.append(slot)
                va += 4
            if not methods:
                rejected += 1
                continue
            vtables[vt_va] = (col_va, methods)
    return vtables, rejected


def recover(path):
    img, xbe = load_xbe(path)
    typedescs = find_type_descriptors(img)
    index = index_dwords(img)
    cols, col_rej = find_cols(img, typedescs, index)
    vtables, vt_rej = find_vtables(img, cols, index)

    # vtable -> class
    vt_class = {}
    for vt_va, (col_va, _methods) in vtables.items():
        desc_va = cols[col_va]
        vt_class[vt_va] = typedescs[desc_va][1]

    # method -> set of owning classes; only unambiguous ones get named
    owners = {}
    for vt_va, (_col, methods) in vtables.items():
        cls = vt_class[vt_va]
        for slot, fn in enumerate(methods):
            owners.setdefault(fn, set()).add((cls, slot))

    names = {}
    ambiguous = {}
    for fn, own in owners.items():
        classes = {c for c, _ in own}
        if len(classes) == 1:
            cls, slot = sorted(own)[0]
            names["0x%08X" % fn] = sanitize("%s__vf%d" % (cls, slot))
        else:
            ambiguous["0x%08X" % fn] = sorted(classes)

    for vt_va, cls in vt_class.items():
        names["0x%08X" % vt_va] = sanitize("%s__vftable" % cls)

    return {
        "xbe": os.path.basename(path),
        "counts": {
            "type_descriptors": len(typedescs),
            "complete_object_locators": len(cols),
            "vtables": len(vtables),
            "named_functions": sum(1 for k in names
                                   if not names[k].endswith("__vftable")),
            "named_vtables": len(vt_class),
            "ambiguous_functions": len(ambiguous),
            "rejected_col_candidates": col_rej,
            "rejected_vtable_candidates": vt_rej,
        },
        "classes": sorted({c for c in vt_class.values()}),
        "names": names,
        "ambiguous": ambiguous,
        "vtables": {"0x%08X" % vt: {"class": vt_class[vt],
                                    "methods": ["0x%08X" % m
                                                for m in vtables[vt][1]]}
                    for vt in sorted(vtables)},
    }


# ----------------------------------------------------------------- self-check

def self_check():
    """Guards the two things most likely to rot: the demangler and the
    conservative-naming rule."""
    cases = [
        (".?AVCMemory@@", "CMemory"),
        (".?AVIAlchemyObjectPool@@", "IAlchemyObjectPool"),
        (".?AVIMemoryPoolInfo@CMemory@@", "CMemory::IMemoryPoolInfo"),
        (".?AUSomeStruct@@", "SomeStruct"),
    ]
    ok = True
    for mangled, want in cases:
        got = demangle(mangled)
        if got != want:
            print("FAIL demangle(%s) = %s, want %s" % (mangled, got, want))
            ok = False
    if sanitize("CMemory::IMemoryPoolInfo__vf3") != "CMemory__IMemoryPoolInfo__vf3":
        print("FAIL sanitize did not flatten ::")
        ok = False
    print("self-check:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xbe", nargs="?", help="path to default.xbe")
    ap.add_argument("--json", help="write the full result here")
    ap.add_argument("--names-only", help="write just {addr: name} here")
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args(argv)

    if a.self_check:
        return self_check()
    if not a.xbe:
        ap.error("need an XBE path (or --self-check)")

    res = recover(a.xbe)
    c = res["counts"]
    print("%s" % res["xbe"])
    print("  type descriptors            %6d" % c["type_descriptors"])
    print("  complete object locators    %6d  (rejected %d)"
          % (c["complete_object_locators"], c["rejected_col_candidates"]))
    print("  vtables                     %6d  (rejected %d)"
          % (c["vtables"], c["rejected_vtable_candidates"]))
    print("  distinct classes w/ vtable  %6d" % len(res["classes"]))
    print("  functions named             %6d" % c["named_functions"])
    print("  functions left ambiguous    %6d  (in >1 vtable, not named)"
          % c["ambiguous_functions"])

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)
        print("  wrote %s" % a.json)
    if a.names_only:
        with open(a.names_only, "w", encoding="utf-8") as f:
            json.dump(res["names"], f, indent=2)
        print("  wrote %s" % a.names_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
