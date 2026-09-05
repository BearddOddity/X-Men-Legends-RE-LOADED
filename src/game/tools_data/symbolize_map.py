"""symbolize_map.py - turn the RVAs in a crash dump into function names.

The map's first column is SECTION-RELATIVE (`0001:0106ce50`), not an RVA. The
third column is the real address (`000000014106de50`), so the RVA is that minus
the preferred load address in the map header - 0x1000 more than the first
column in this build. Reading the first column as an RVA silently names the
function BELOW the true one, which sends every probe to the wrong place.

Usage (from src/game/):
    py -3 tools_data/symbolize_map.py 0x106CED2 [more RVAs...]
    py -3 tools_data/symbolize_map.py --stderr stderr.txt
"""
import re
import sys
import bisect

MAP = "build/xmen_legends_recomp.map"
ROW = re.compile(r'^\s*[0-9A-Fa-f]{4}:[0-9A-Fa-f]{8}\s+(\S+)\s+([0-9A-Fa-f]{16})\s')
BASE = re.compile(r'Preferred load address is ([0-9A-Fa-f]+)')


def load(path=MAP):
    base = None
    syms = []
    for line in open(path, encoding="utf-8", errors="ignore"):
        if base is None:
            m = BASE.search(line)
            if m:
                base = int(m.group(1), 16)
                continue
        m = ROW.match(line)
        if m:
            syms.append((int(m.group(2), 16) - base, m.group(1)))
    syms.sort()
    return syms


def name(syms, starts, rva):
    i = bisect.bisect_right(starts, rva) - 1
    if i < 0:
        return "?"
    a, n = syms[i]
    return "%s +0x%X" % (n, rva - a)


def main(argv):
    syms = load()
    starts = [a for a, _ in syms]
    if argv and argv[0] == "--stderr":
        text = open(argv[1], encoding="utf-8", errors="ignore").read()
        i = text.find("[CRASH]")
        seg = text[i:i + 3000] if i >= 0 else text
        for m in re.finditer(r'RVA[ =]0x([0-9A-Fa-f]+)', seg):
            r = int(m.group(1), 16)
            print("RVA %08X -> %s" % (r, name(syms, starts, r)))
        return 0
    for a in argv:
        r = int(a, 16)
        print("RVA %08X -> %s" % (r, name(syms, starts, r)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
