#!/usr/bin/env python3
"""match_bodies.py - name game functions by matching them against the
Alchemy SDK DLLs.

Position cannot be used. The game's Alchemy is neither 2.5 nor 5.0: vtable
lengths disagree (igNamedObject 17 slots in 2.5, 21 in the game) and the object
layout runs the other way (a member at this+0x14 in 2.5 sits at this+0x10 in
the game). So slot arithmetic is out and the code itself has to do the talking.

Matching is fuzzy on purpose. These are different builds of nearby source, not
the same binary: an instruction gets inserted, a register allocation changes, a
branch inverts. Anything that demands an exact body match finds nothing.

Reference sources
-----------------
More than one SDK version can be given, each with a label:

    --ref a25=/path/to/2.5/dlls --ref a50=/path/to/5.0/dlls

That is not just more volume. The game sits somewhere between the two, so a
function that drifted away from 2.5 may still match 5.0. More usefully, when
two independently built versions propose the *same* name for one game
function, that agreement is corroboration in its own right - and it does not
depend on the call graph, which the version gap keeps breaking.

Stages
------
1. reference sides - every exported function in every reference DLL
2. target side     - every game function from ExportFunctionBytes.java
3. candidates      - inverted index over *rare* 4-grams of the loose token
                     stream. Common n-grams (every MSVC prologue, every
                     refcount bump) are dropped: they cost time and carry no
                     identifying information
4. score           - difflib ratio over the full token stream, which tolerates
                     an inserted or deleted instruction where positional
                     comparison would not
5. corroborate     - accept on cross-version agreement, or on the call graph,
                     or on a high bar for a leaf with nothing to check against

Identical code folding means one address legitimately carries several exported
names, so an ambiguous group is reported, never guessed.
"""
import argparse
import collections
import difflib
import os
import sys

import pefile

import bodyfp

MIN_INSNS = 8         # below this a body is too generic to identify
NGRAM = 4
MAX_DF = 200          # an n-gram in more than this many refs identifies nothing
TOPK = 25             # candidates scored per game function per source
MIN_LOOSE = 0.72      # shape agreement needed to consider a pair at all
MIN_STRICT = 0.55     # register/immediate agreement on top of that
MIN_CALLS_OK = 0.50   # share of callees that must themselves match
DISP_MIN = 0.70       # delta-sequence agreement needed to break a tie
DISP_MARGIN = 0.15    # by how much the winner must beat the runner-up
LEAF_LOOSE = 0.90     # a leaf has nothing to corroborate with, so it is held
LEAF_STRICT = 0.80    # to the body alone


def ngrams(toks, n=NGRAM):
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)}


def short_name(sym):
    """Class::method out of a mangled symbol, for comparing across versions.

    Signatures change between SDK versions, so the full mangled string is not
    comparable; the scope and the method name are.
    """
    if not sym.startswith("?"):
        return sym
    if sym.startswith("??"):
        head, rest = sym[:3], sym[3:]
    else:
        parts = sym[1:].split("@")
        head, rest = parts[0], "@".join(parts[1:])
    scope = []
    for p in rest.split("@"):
        if not p or p.startswith("$"):
            break
        if len(p) <= 3 and p.isupper() and scope:
            break
        scope.append(p)
    return "::".join(list(reversed(scope)) + [head])


def load_reference(label, dll_dir, exclude):
    refs = []
    for fn in sorted(os.listdir(dll_dir)):
        if not fn.lower().endswith(".dll"):
            continue
        if any(x and x in fn for x in exclude):
            print("  %-24s excluded" % fn, file=sys.stderr)
            continue
        path = os.path.join(dll_dir, fn)
        try:
            pe = pefile.PE(path, fast_load=True)
            pe.parse_data_directories(directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
        except Exception as e:
            print("  skip %s: %s" % (fn, e), file=sys.stderr)
            continue
        exp = getattr(pe, "DIRECTORY_ENTRY_EXPORT", None)
        if not exp:
            continue
        base = pe.OPTIONAL_HEADER.ImageBase

        by_rva = collections.defaultdict(list)
        for s in exp.symbols:
            if s.name and s.address:
                by_rva[s.address].append(s.name.decode("ascii", "replace"))

        n = 0
        for rva, names in by_rva.items():
            try:
                code = pe.get_data(rva, 4096)
            except Exception:
                continue
            loose = bodyfp.tokens(code, base + rva, strict=False)
            if len(loose) < MIN_INSNS:
                continue
            refs.append({
                "src": label, "dll": fn, "names": names,
                "loose": loose,
                "strict": bodyfp.tokens(code, base + rva, strict=True),
                "disp": bodyfp.disp_deltas(code, base + rva),
            })
            n += 1
        print("  [%s] %-24s %5d bodies" % (label, fn, n), file=sys.stderr)
    return refs


def load_game(tsv):
    out = []
    with open(tsv) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.rstrip("\n").split("\t")
            if len(f) < 4:
                continue
            addr = int(f[0], 16)
            code = bytes.fromhex(f[3])
            loose = bodyfp.tokens(code, addr, strict=False)
            if len(loose) < MIN_INSNS:
                continue
            out.append({
                "addr": addr, "name": f[2], "loose": loose,
                "strict": bodyfp.tokens(code, addr, strict=True),
                "disp": bodyfp.disp_deltas(code, addr),
                "calls": bodyfp.call_targets(code, addr),
            })
    return out


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def build_index(refs):
    idx = collections.defaultdict(list)
    for i, r in enumerate(refs):
        for g in ngrams(r["loose"]):
            idx[g].append(i)
    dropped = 0
    for g in list(idx):
        if len(idx[g]) > MAX_DF:
            del idx[g]
            dropped += 1
    print("  index: %d rare n-grams kept, %d common dropped"
          % (len(idx), dropped), file=sys.stderr)
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", action="append", required=True,
                    metavar="LABEL=DIR", help="reference DLL directory")
    ap.add_argument("--exclude", default="_patched,libdbg",
                    help="comma separated filename substrings to skip")
    ap.add_argument("--game", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", default=None)
    a = ap.parse_args()

    exclude = [x.strip() for x in a.exclude.split(",") if x.strip()]

    refs = []
    print("reference sides:", file=sys.stderr)
    for spec in a.ref:
        label, _, d = spec.partition("=")
        if not d:
            label, d = "ref%d" % len(refs), spec
        refs.extend(load_reference(label, d, exclude))
    print("  %d reference bodies total" % len(refs), file=sys.stderr)

    print("target side:", file=sys.stderr)
    game = load_game(a.game)
    print("  %d game bodies" % len(game), file=sys.stderr)

    print("indexing:", file=sys.stderr)
    idx = build_index(refs)

    game_by_addr = {g["addr"]: g for g in game}
    sources = sorted({r["src"] for r in refs})

    pairs = {}
    disp_broke = [0]
    for n, g in enumerate(game):
        if n % 2000 == 0:
            print("  scoring %d/%d" % (n, len(game)), file=sys.stderr)
        counts = collections.Counter()
        for gram in ngrams(g["loose"]):
            for i in idx.get(gram, ()):
                counts[i] += 1
        if not counts:
            continue

        # Score the best candidates from each source separately, so that
        # agreement between sources can be seen.
        per_src = {}
        seen = collections.Counter()
        for i, _ in counts.most_common(TOPK * max(1, len(sources))):
            r = refs[i]
            if seen[r["src"]] >= TOPK:
                continue
            seen[r["src"]] += 1
            lo = ratio(g["loose"], r["loose"])
            if lo < MIN_LOOSE:
                continue
            st = ratio(g["strict"], r["strict"])
            if st < MIN_STRICT:
                continue
            cur = per_src.get(r["src"])
            if cur is None or lo + st > cur[0]:
                per_src[r["src"]] = (lo + st, lo, st, r)
        if not per_src:
            continue

        best = max(per_src.values(), key=lambda t: t[0])
        best_short = short_name(best[3]["names"][0])

        # Which other sources landed on the same class::method?
        agree = sorted(s for s, v in per_src.items()
                       if any(short_name(nm) == best_short for nm in v[3]["names"]))

        # Folded code: several distinct names at the winning score.
        top = [v for v in per_src.values() if v[0] >= best[0] - 0.02]
        top_names = {nm for v in top for nm in v[3]["names"]}
        ambiguous = len({short_name(n) for n in top_names}) > 1

        if ambiguous and g["disp"]:
            # Same shape, different class. The offsets decide - as deltas, so
            # the constant base skew between versions cancels out.
            ranked = sorted(
                ((ratio(g["disp"], v[3]["disp"]), v) for v in top),
                key=lambda t: -t[0])
            if len(ranked) > 1 and ranked[0][0] - ranked[1][0] >= DISP_MARGIN                     and ranked[0][0] >= DISP_MIN:
                best = ranked[0][1]
                top_names = set(best[3]["names"])
                ambiguous = len({short_name(n) for n in top_names}) > 1
                disp_broke[0] += 1

        pairs[g["addr"]] = {
            "ref": best[3], "loose": best[1], "strict": best[2],
            "names": sorted(top_names), "ambiguous": ambiguous,
            "agree": agree, "short": best_short,
        }

    print("  %d candidate pairs before corroboration (%d ties broken by offsets)"
          % (len(pairs), disp_broke[0]), file=sys.stderr)

    confirmed, weak = {}, {}
    for addr, p in pairs.items():
        g = game_by_addr[addr]
        gcalls = [c for c in g["calls"] if c in game_by_addr]
        hit = sum(1 for c in gcalls if c in pairs)
        p["calls_checked"] = len(gcalls)
        p["calls_matched"] = hit
        p["why"] = ""

        if p["ambiguous"]:
            weak[addr] = p
        elif len(p["agree"]) >= 2:
            # Two independently built SDK versions naming the same function is
            # evidence that does not depend on the call graph.
            p["why"] = "cross-version(%s)" % "+".join(p["agree"])
            confirmed[addr] = p
        elif gcalls and hit / float(len(gcalls)) >= MIN_CALLS_OK:
            p["why"] = "callees %d/%d" % (hit, len(gcalls))
            confirmed[addr] = p
        elif not gcalls and p["loose"] >= LEAF_LOOSE and p["strict"] >= LEAF_STRICT:
            p["why"] = "leaf, body only"
            confirmed[addr] = p
        else:
            weak[addr] = p

    with open(a.out, "w") as fh:
        fh.write("#addr\tcurrent\tproposed\tloose\tstrict\tcalls_matched"
                 "\tcalls_checked\tdll\twhy\tagree\n")
        for addr in sorted(confirmed):
            p = confirmed[addr]
            fh.write("%08x\t%s\t%s\t%.2f\t%.2f\t%d\t%d\t%s:%s\t%s\t%s\n" % (
                addr, game_by_addr[addr]["name"], p["names"][0],
                p["loose"], p["strict"], p["calls_matched"], p["calls_checked"],
                p["ref"]["src"], p["ref"]["dll"], p["why"], "+".join(p["agree"])))

    if a.report:
        with open(a.report, "w") as fh:
            fh.write("#addr\tcurrent\tcandidates\tloose\tstrict\treason\n")
            for addr in sorted(weak):
                p = weak[addr]
                reason = ("folded/ambiguous" if p["ambiguous"]
                          else "leaf below bar" if not p["calls_checked"]
                          else "callees unmatched")
                fh.write("%08x\t%s\t%s\t%.2f\t%.2f\t%s\n" % (
                    addr, game_by_addr[addr]["name"], "|".join(p["names"]),
                    p["loose"], p["strict"], reason))

    bywhy = collections.Counter(p["why"].split("(")[0] for p in confirmed.values())
    print("confirmed=%d  needs_review=%d -> %s" % (
        len(confirmed), len(weak), a.out), file=sys.stderr)
    for k, v in bywhy.most_common():
        print("    %-18s %d" % (k, v), file=sys.stderr)


if __name__ == "__main__":
    main()
