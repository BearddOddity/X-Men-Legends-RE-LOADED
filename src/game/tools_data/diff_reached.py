"""Diff two [COVERAGE-VA] dumps - the set difference that counts cannot give.

Both coverage COUNTS fell after seeding sub_00011B2B and sub_001E9558
(reached 55 -> 42, callsites 194 -> 177) while kernel calls quadrupled. A
count cannot distinguish "went further" from "stopped thrashing", because a
retry spin visits many distinct places while achieving nothing.

The set difference can. Addresses present ONLY in the post-seed run are
functions that never executed before the change, whatever the totals did.
Addresses present ONLY pre-seed are what the boot stopped doing.

The dumped VAs are guest addresses in the game's own code range, so each one
IS its function name: 0x001E8F30 -> sub_001E8F30. No symbolisation needed.

Usage: diff_reached.py <before.txt> <after.txt>
"""
import io, os, sys


def load(path):
    if not os.path.exists(path):
        sys.exit("missing: %s" % path)
    out = set()
    with io.open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.add(int(line, 16))
            except ValueError:
                sys.exit("not a hex VA: %r in %s" % (line, path))
    if not out:
        sys.exit("empty: %s" % path)
    return out


def show(title, vas):
    print("\n%s (%d)" % (title, len(vas)))
    if not vas:
        print("  <none>")
        return
    for va in sorted(vas):
        print("  sub_%08X" % va)


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__.strip().splitlines()[-1])
    before, after = load(argv[0]), load(argv[1])

    gained = after - before
    lost = before - after
    kept = after & before

    print("before : %d reached" % len(before))
    print("after  : %d reached" % len(after))
    print("common : %d" % len(kept))

    show("ONLY AFTER  - code that never ran before the change", gained)
    show("ONLY BEFORE - code the boot no longer executes", lost)

    print("\nverdict:")
    if gained and not lost:
        print("  strictly more code ran. Unambiguous progress.")
    elif gained:
        print("  %d new function(s) reached, %d dropped." % (len(gained), len(lost)))
        print("  Progress IF the dropped ones are the abandoned retry path -")
        print("  check whether they are the OOM reporter / allocator spin.")
    elif lost:
        print("  NOTHING new was reached and %d were lost." % len(lost))
        print("  That is a regression on this measure. Do not explain it away.")
    else:
        print("  identical sets - the change moved no coverage at all.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
