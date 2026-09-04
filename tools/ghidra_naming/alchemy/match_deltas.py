#!/usr/bin/env python3
"""match_deltas.py - identify a function by its member-offset signature.

Why this exists
---------------
A whole family of Alchemy functions has one shape. Every destructor is the
same "release a refcounted member" block repeated, so the shape fingerprint
that match_bodies.py uses collapses them onto each other: eighteen different
game destructors all claimed to be igResource::dtor, and none of them was.

What actually distinguishes those functions is *which* members they touch. The
absolute offsets are not comparable between SDK versions - the base differs by
a constant - but the differences between consecutive offsets are, because a
constant shift cancels. A destructor's delta sequence is therefore a
fingerprint of its class's refcounted-member layout.

This searches the reference set on that signature instead of on shape. Shape is
still required as a prefilter - the answer has to be the same kind of function -
but the decision is the offsets.

    ./match_deltas.py --ref a25=DIR --ref a50=DIR --game game_func_bytes.tsv \
        --addrs 0020b970,002135b0,... --out delta_names.tsv
"""
import argparse
import collections
import difflib
import os
import sys

import pefile

import bodyfp

MIN_SHAPE = 0.65      # the candidate must still be the same kind of function
MIN_DELTA = 0.85      # offsets must agree strongly - this is the decision
MIN_MARGIN = 0.10     # and beat the runner-up by this much
MIN_DELTAS = 6        # too few offsets to be a signature


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def load_reference(label, dll_dir, exclude):
    refs = []
    for fn in sorted(os.listdir(dll_dir)):
        if not fn.lower().endswith(".dll"):
            continue
        if any(x and x in fn for x in exclude):
            continue
        try:
            pe = pefile.PE(os.path.join(dll_dir, fn), fast_load=True)
            pe.parse_data_directories(directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
        except Exception:
            continue
        exp = getattr(pe, "DIRECTORY_ENTRY_EXPORT", None)
        if not exp:
            continue
        base = pe.OPTIONAL_HEADER.ImageBase
        by_rva = collections.defaultdict(list)
        for s in exp.symbols:
            if s.name and s.address:
                by_rva[s.address].append(s.name.decode("ascii", "replace"))
        for rva, names in by_rva.items():
            try:
                code = pe.get_data(rva, 4096)
            except Exception:
                continue
            d = bodyfp.disp_deltas(code, base + rva)
            if len(d) < MIN_DELTAS:
                continue
            refs.append({
                "src": label, "dll": fn, "names": names, "disp": d,
                "loose": bodyfp.tokens(code, base + rva, strict=False),
            })
        print("  [%s] %s" % (label, fn), file=sys.stderr)
    return refs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", action="append", required=True)
    ap.add_argument("--exclude", default="_patched,libdbg")
    ap.add_argument("--game", required=True)
    ap.add_argument("--addrs", required=True,
                    help="comma separated game addresses, or a file of them")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    exclude = [x.strip() for x in a.exclude.split(",") if x.strip()]

    if os.path.exists(a.addrs):
        want = {l.strip().lower() for l in open(a.addrs) if l.strip()}
    else:
        want = {x.strip().lower() for x in a.addrs.split(",") if x.strip()}

    targets = []
    for line in open(a.game):
        if line.startswith("#"):
            continue
        f = line.rstrip("\n").split("\t")
        if len(f) < 4 or f[0].lower() not in want:
            continue
        code = bytes.fromhex(f[3])
        addr = int(f[0], 16)
        targets.append({
            "addr": f[0], "name": f[2],
            "disp": bodyfp.disp_deltas(code, addr),
            "loose": bodyfp.tokens(code, addr, strict=False),
        })
    print("targets: %d of %d requested" % (len(targets), len(want)), file=sys.stderr)

    refs = []
    print("reference sides:", file=sys.stderr)
    for spec in a.ref:
        label, _, d = spec.partition("=")
        refs.extend(load_reference(label or "ref", d, exclude))
    print("  %d reference bodies with an offset signature" % len(refs), file=sys.stderr)

    out = open(a.out, "w")
    out.write("#addr\tcurrent\tproposed\tdelta\tshape\trunner_up\tmargin\tverdict\tdll\n")
    named = 0
    for t in targets:
        if len(t["disp"]) < MIN_DELTAS:
            out.write("%s\t%s\t-\t-\t-\t-\t-\tno signature\t-\n" % (t["addr"], t["name"]))
            continue
        scored = []
        for r in refs:
            sh = ratio(t["loose"], r["loose"])
            if sh < MIN_SHAPE:
                continue
            scored.append((ratio(t["disp"], r["disp"]), sh, r))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            out.write("%s\t%s\t-\t-\t-\t-\t-\tno candidate of this shape\t-\n"
                      % (t["addr"], t["name"]))
            print("  %s  no candidate" % t["addr"], file=sys.stderr)
            continue
        best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        margin = best[0] - second
        ok = best[0] >= MIN_DELTA and margin >= MIN_MARGIN
        verdict = "NAMED" if ok else "no confident match"
        if ok:
            named += 1
        out.write("%s\t%s\t%s\t%.2f\t%.2f\t%.2f\t%.2f\t%s\t%s:%s\n" % (
            t["addr"], t["name"], best[2]["names"][0], best[0], best[1],
            second, margin, verdict, best[2]["src"], best[2]["dll"]))
        print("  %s  best=%.2f margin=%.2f  %s  %s" % (
            t["addr"], best[0], margin, verdict, best[2]["names"][0][:60]),
            file=sys.stderr)
    out.close()
    print("named=%d of %d -> %s" % (named, len(targets), a.out), file=sys.stderr)


if __name__ == "__main__":
    main()
