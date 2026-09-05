"""Extract MSVC RTTI classes and vtables from the X-Men Legends II PC build,
and compare them against the Xbox XML1 classes that tools.rtti recovers.

Why this exists
---------------
XML2 for PC (XMen2.exe, MSVC 7.10, September 2005) is the same Intrinsic
Alchemy codebase as the Xbox XML1 target, compiled for Win32 and shipped with
RTTI intact. It carries 1,058 type descriptors, so it can name what XML1's own
class registry leaves anonymous, and it can show what a method at a given
vtable slot actually does in a build we can run natively.

The comparison matters as much as the names. XML2 is a LATER engine revision:
of the 412 class names the two binaries share, only 193 have the same vtable
slot count. CGame has 110 virtuals on Xbox and 162 on PC. So a slot INDEX from
XML2 is trustworthy only for a class whose slot count matches - for the other
219 it will silently name the wrong method. The map this writes records both
counts side by side precisely so that check is cheap.

Usage (from the repo root):
    py -3 tools/rtti/xml2_pc.py <path to XMen2.exe> -o build/xml2_rtti.json
    py -3 tools/rtti/xml2_pc.py <path to XMen2.exe> --compare build/rtti.json \
        --map-out build/xml1_xml2_class_map.json

How the extraction works
------------------------
MSVC lays RTTI out as: vtable[-1] -> RTTICompleteObjectLocator, whose field at
+12 points to a TypeDescriptor, whose name begins 8 bytes into it. This walks
that chain backwards - names first, then the locators that reference them, then
the vtables that reference those - because the names are the only part that can
be found without already knowing where anything is.
"""
import argparse
import collections
import json
import re
import struct
import sys


def load_sections(data):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt = pe + 24
    base = struct.unpack_from("<I", data, opt + 28)[0]
    off = opt + struct.unpack_from("<H", data, pe + 20)[0]
    secs = []
    for i in range(nsec):
        s = data[off + i * 40: off + i * 40 + 40]
        name = s[:8].rstrip(b"\0").decode("latin1")
        vsize, va, rsize, raw = struct.unpack_from("<IIII", s, 8)
        secs.append({"name": name, "va": base + va, "vsize": vsize,
                     "raw": raw, "rsize": rsize})
    return base, secs


def make_v2o(secs):
    def v2o(va):
        for s in secs:
            if s["va"] <= va < s["va"] + max(s["vsize"], s["rsize"]):
                o = s["raw"] + (va - s["va"])
                return o if o + 4 <= s["raw"] + s["rsize"] else None
        return None
    return v2o


def o2v(secs, off):
    for s in secs:
        if s["raw"] <= off < s["raw"] + s["rsize"]:
            return s["va"] + (off - s["raw"])
    return None


NAME_RE = re.compile(rb"\.\?A[VU][A-Za-z0-9_@?$]{2,160}@@\x00")


def extract(data, verbose=False):
    base, secs = load_sections(data)
    v2o = make_v2o(secs)
    text = next(s for s in secs if s["name"] == ".text")
    text_lo, text_hi = text["va"], text["va"] + text["vsize"]

    # TypeDescriptor: the name string starts at +8, so the descriptor is 8
    # bytes before it.
    tds = {}
    for m in NAME_RE.finditer(data):
        va = o2v(secs, m.start())
        if va is not None:
            tds[va - 8] = m.group(0)[:-1].decode("latin1")

    # One pass over every aligned dword in the data sections; both the locator
    # and the vtable lookups below are "who points at this address" questions.
    refs = collections.defaultdict(list)
    for s in secs:
        if s["name"] not in (".rdata", ".data", ".data1"):
            continue
        for o in range(s["raw"], s["raw"] + s["rsize"] - 4, 4):
            val = struct.unpack_from("<I", data, o)[0]
            if val:
                refs[val].append(s["va"] + (o - s["raw"]))

    cols = {}
    for td_va, name in tds.items():
        for ref in refs.get(td_va, ()):
            col = ref - 12                     # pTypeDescriptor is COL + 12
            o = v2o(col)
            if o is None:
                continue
            signature, offset, _cd = struct.unpack_from("<III", data, o)
            if signature in (0, 1):            # 32-bit MSVC writes 0
                cols[col] = (name, offset)

    classes = {}
    for col, (name, offset) in cols.items():
        for ref in refs.get(col, ()):
            vt = ref + 4                       # the locator sits at vtable[-1]
            slots = 0
            while slots < 512:
                o = v2o(vt + slots * 4)
                if o is None:
                    break
                p = struct.unpack_from("<I", data, o)[0]
                if not (text_lo <= p < text_hi):
                    break
                slots += 1
            if not slots:
                continue
            # A class with multiple bases has one vtable per base. Keep the
            # widest - it is the complete object's own table.
            prev = classes.get(name)
            if prev is None or slots > prev["vtable_slots"]:
                classes[name] = {"vtable": "0x%x" % vt,
                                 "vtable_slots": slots,
                                 "base_offset": offset}
    if verbose:
        print("%d type descriptors, %d complete object locators, "
              "%d classes with vtables" % (len(tds), len(cols), len(classes)),
              file=sys.stderr)
    return classes


MANGLED = re.compile(r"^\.\?A[VU](.+)@@$")


def demangle(name):
    """`.?AVCActor@@` -> `CActor`, matching how tools.rtti names XML1 classes."""
    m = MANGLED.match(name)
    return m.group(1) if m else name


def compare(xml2, xml1_path, map_out=None):
    xml1 = json.load(open(xml1_path, encoding="utf-8"))["classes"]
    x2 = {demangle(k): v for k, v in xml2.items()}
    shared = sorted(set(x2) & set(xml1))
    rows = {}
    same = 0
    for k in shared:
        a, b = xml1[k]["vtable_slots"], x2[k]["vtable_slots"]
        same += (a == b)
        rows[k] = {"xml1_slots": a, "xml2_slots": b,
                   "xml1_vtable": xml1[k]["vtable"],
                   "xml2_vtable": x2[k]["vtable"],
                   "slots_match": a == b}
    print("XML1 classes %d, XML2 classes %d, shared names %d"
          % (len(xml1), len(x2), len(shared)))
    print("  identical vtable slot counts : %d  (slot indices transferable)"
          % same)
    print("  differing slot counts        : %d  (slot indices are NOT)"
          % (len(shared) - same))
    if map_out:
        json.dump(rows, open(map_out, "w", encoding="utf-8"), indent=1)
        print("  wrote %s" % map_out)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("exe", help="path to XMen2.exe (XML2 PC)")
    ap.add_argument("-o", "--out", default="build/xml2_rtti.json")
    ap.add_argument("--compare", metavar="RTTI_JSON",
                    help="tools.rtti output for the Xbox binary, e.g. "
                         "build/rtti.json")
    ap.add_argument("--map-out", metavar="FILE",
                    help="write the shared-class comparison here")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    data = open(args.exe, "rb").read()
    classes = extract(data, verbose=args.verbose)
    json.dump(classes, open(args.out, "w", encoding="utf-8"), indent=1)
    print("%d classes -> %s" % (len(classes), args.out))

    if args.compare:
        compare(classes, args.compare, args.map_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
