"""sweep_jumptables.py - find jump-table targets no dispatch path can reach.

The bug
-------
The lifter emits switch cases as unreachable blocks inside their owning
function. If the dispatch is a BARE indirect tail jump

    RECOMP_ITAIL(MEM32(reg * 4 + 0xBASE))

then nothing resolves those targets, every jump through the table fails, and
the owning function's pushes are stranded - a stack leak plus a clobbered
callee-saved register, surfacing far from the switch. That is ledger #54:
seeding one target (sub_001F840B) was worth reached 47 -> 54.

Two things this tool must NOT get wrong, both learned the hard way
------------------------------------------------------------------
1. The OTHER emitted form is safe and must not be counted:

       { uint32_t _jt = MEM32(reg * 4 + 0xBASE);
         if (_jt == 0x002164CDu) goto loc_002164CD;
         ...
         RECOMP_ITAIL(_jt); }

   Every target is resolved inline by an explicit goto, so the ITAIL is only
   a fallback. Counting these inflated the first sweep from 83 sites to 272.

2. Seeded functions register in src/recomp_manual.c via
   `if (xbox_va == 0x...u) return sub_...;`, NOT in recomp_dispatch.c.
   Checking only the dispatch table reports already-seeded targets as missing.

Reads the tables straight out of game/default.xbe and self-checks its
VA->offset arithmetic against the known table at 0x001F84AC first, so a wrong
mapping cannot produce a plausible-looking list.

Usage (from src/game/):
    py -3 tools_data/sweep_jumptables.py
    py -3 tools_data/sweep_jumptables.py --func sub_001F83D0
"""
import argparse, os, re, glob, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
GEN = os.path.join(GAME, "src", "recomp", "gen")
XBE = os.path.join(GAME, "game", "default.xbe")
MANUAL = os.path.join(GAME, "src", "recomp_manual.c")

TEXT_VA, TEXT_RAW, TEXT_SIZE = 0x00011000, 0x00001000, 3448212
IMG_LO, IMG_HI = 0x00011000, 0x0035ADA0

# only the BARE form - see the header note
BARE = re.compile(r'RECOMP_ITAIL\(MEM32\([a-z]+ \* 4 \+ 0x([0-9A-Fa-f]+)\)\)')


def make_rd32(blob):
    def rd32(va):
        if not (TEXT_VA <= va < TEXT_VA + TEXT_SIZE):
            return None
        off = va - TEXT_VA + TEXT_RAW
        return struct.unpack_from("<I", blob, off)[0] if off + 4 <= len(blob) else None
    return rd32


def registered_addresses():
    """Everything reachable by dispatch: the table AND the manual overrides."""
    out = set()
    disp = os.path.join(GEN, "recomp_dispatch.c")
    if os.path.exists(disp):
        with open(disp, errors="ignore") as f:
            for m in re.finditer(r'\{\s*0x([0-9A-Fa-f]+)u', f.read()):
                out.add(int(m.group(1), 16))
    if os.path.exists(MANUAL):
        with open(MANUAL, errors="ignore") as f:
            for m in re.finditer(r'xbox_va\s*==\s*0x([0-9A-Fa-f]+)u', f.read()):
                out.add(int(m.group(1), 16))
    return out


def find_sites():
    """func -> {table base}, bare ITAIL dispatches only."""
    out = {}
    for path in sorted(glob.glob(os.path.join(GEN, "recomp_*.c"))):
        fn = None
        with open(path, errors="ignore") as f:
            for line in f:
                m = re.match(r'void (sub_[0-9A-Fa-f]+)\(void\)', line)
                if m:
                    fn = m.group(1)
                for mm in BARE.finditer(line):
                    out.setdefault(fn, set()).add(int(mm.group(1), 16))
    return out


def table_entries(rd32, base, cap=64):
    ents = []
    for i in range(cap):
        v = rd32(base + 4 * i)
        if v is None or not (IMG_LO <= v < IMG_HI):
            break
        ents.append(v)
    return ents


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--func", help="only report this function")
    args = ap.parse_args(argv)

    with open(XBE, "rb") as f:
        blob = f.read()
    rd32 = make_rd32(blob)
    known = [rd32(0x001F84AC + 4 * i) for i in range(3)]
    if known != [0x001F83EF, 0x001F83F6, 0x001F83FD]:
        sys.exit("VA->offset self-check FAILED: got %s" % [hex(x or 0) for x in known])
    print("self-check OK (table 0x001F84AC reads back correctly)")

    reg = registered_addresses()
    sites = find_sites()
    print("registered (dispatch + manual): %d" % len(reg))
    print("bare-ITAIL jump-table sites    : %d in %d function(s)\n"
          % (sum(len(v) for v in sites.values()), len(sites)))

    total_missing = 0
    tables_bad = 0
    for fn in sorted(sites, key=lambda x: (x is None, x)):
        if args.func and fn != args.func:
            continue
        for base in sorted(sites[fn]):
            ents = table_entries(rd32, base)
            if not ents:
                continue
            missing = sorted({v for v in ents if v not in reg})
            if not missing:
                continue
            tables_bad += 1
            total_missing += len(missing)
            print("  %-16s table 0x%08X  %d of %d unregistered"
                  % (fn or "?", base, len(missing), len(ents)))
            print("       " + " ".join("0x%08X" % v for v in missing))
    print("\ntables with unregistered targets: %d" % tables_bad)
    print("distinct unregistered targets   : %d" % total_missing)


if __name__ == "__main__":
    main()
