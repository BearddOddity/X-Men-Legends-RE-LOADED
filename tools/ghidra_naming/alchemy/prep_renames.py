#!/usr/bin/env python3
"""prep_renames.py - turn the matcher's output into names Ghidra can take.

Two jobs beyond formatting:

  * demangle to a readable qualified name. "?multiply@igMatrix44f@Math@Gap@@..."
    becomes "Gap::Math::igMatrix44f::multiply", matching the ::-joined style the
    RTTI walk already put in the database.
  * refuse many-to-one. Several game addresses proposing the same Alchemy name
    means at most one of them is right - inlined copies, overloads the mangling
    distinguishes but the body does not, or a bad match. Those go to review
    rather than getting the same name stamped on all of them.
"""
import argparse
import collections

SPECIAL = {
    "??0": "ctor", "??1": "dtor", "??_G": "scalar_deleting_dtor",
    "??_E": "vector_deleting_dtor", "??_D": "vbase_dtor",
    "??4": "operator_assign", "??2": "operator_new", "??3": "operator_delete",
    "??8": "operator_eq", "??9": "operator_ne", "??A": "operator_index",
    "??6": "operator_lshift", "??5": "operator_rshift", "??_7": "vftable",
}


def qualified(sym):
    """?name@Class@Ns2@Ns1@@... -> Ns1::Ns2::Class::name"""
    if not sym.startswith("?"):
        return sym
    method = None
    rest = None
    for pfx, nm in SPECIAL.items():
        if sym.startswith(pfx):
            method, rest = nm, sym[len(pfx):]
            break
    if method is None:
        body = sym[1:]
        parts = body.split("@")
        method, rest = parts[0], "@".join(parts[1:])
    scope = []
    for p in rest.split("@"):
        if not p or p.startswith("$") or p in ("Z",):
            break
        # The signature follows the scope; it starts at the encoding letters.
        if len(p) <= 3 and p.isupper() and scope:
            break
        scope.append(p)
    name = "::".join(list(reversed(scope)) + [method])
    return "".join(c if (c.isalnum() or c in "_:") else "_" for c in name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--renames", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rejected", required=True)
    a = ap.parse_args()

    rows = []
    with open(a.renames) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.rstrip("\n").split("\t")
            if len(f) < 8:
                continue
            rows.append({
                "addr": f[0], "current": f[1], "sym": f[2],
                "loose": float(f[3]), "strict": float(f[4]),
                "cm": f[5], "cc": f[6],
                # carry the corroboration reason into the dll field so it
                # reaches the plate comment the apply step writes
                "dll": (f[7] + " " + f[8]).strip() if len(f) > 8 else f[7],
                "name": qualified(f[2]),
            })

    by_name = collections.Counter(r["name"] for r in rows)

    keep, drop = [], []
    for r in rows:
        if by_name[r["name"]] > 1:
            r["reason"] = "many-to-one (%d addresses propose this name)" % by_name[r["name"]]
            drop.append(r)
        else:
            keep.append(r)

    with open(a.out, "w") as fh:
        fh.write("#addr\tnew_name\told_name\tloose\tstrict\tdll\n")
        for r in sorted(keep, key=lambda x: x["addr"]):
            fh.write("%s\t%s\t%s\t%.2f\t%.2f\t%s\n" % (
                r["addr"], r["name"], r["current"], r["loose"], r["strict"], r["dll"]))

    with open(a.rejected, "w") as fh:
        fh.write("#addr\tproposed\told_name\tloose\tstrict\treason\n")
        for r in sorted(drop, key=lambda x: x["name"]):
            fh.write("%s\t%s\t%s\t%.2f\t%.2f\t%s\n" % (
                r["addr"], r["name"], r["current"], r["loose"], r["strict"], r["reason"]))

    print("apply=%d  rejected_many_to_one=%d" % (len(keep), len(drop)))
    for r in sorted(keep, key=lambda x: -x["loose"]):
        print("  %s  %-58s  %.2f/%.2f  was %s" % (
            r["addr"], r["name"], r["loose"], r["strict"], r["current"]))


if __name__ == "__main__":
    main()
