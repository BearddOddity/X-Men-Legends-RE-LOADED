#!/usr/bin/env python3
"""Archive a known-good build so it can be restored instead of rebuilt.

Why this exists
---------------
gen/ is derived, so it is gitignored - the assumption being that it can
always be rebuilt from a commit. That assumption is false here: the seeding
step is order-dependent, so a rebuild from the same commit produced 54/4/2/8
where the original had measured 56/3/2/5. Derived-but-not-reproducible is the
worst category to be in, because it looks disposable and is not.

Until seeding is deterministic, the fix is to keep the artefact. Taking a
snapshot costs seconds; the rebuild it replaces cost an hour and still did not
land on the same numbers.

Snapshots live OUTSIDE the repo so git never sees them, and are named by
commit and by the signals that were measured, so "the build that got 56" is a
thing you can actually go and get.

Usage (from src/game/):
    py -3 tools_data/snapshot.py                     # take one, auto-named
    py -3 tools_data/snapshot.py -m "before threads" # with a note
    py -3 tools_data/snapshot.py --list
    py -3 tools_data/snapshot.py --restore <name>

Override the location with RECOMP_SNAPSHOT_DIR.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(GAME))

DEFAULT_DIR = os.path.join(os.path.dirname(REPO), "recomp-snapshots")
SNAP_DIR = os.environ.get("RECOMP_SNAPSHOT_DIR", DEFAULT_DIR)

# Everything needed to put a build back exactly as it was. gen/ and the exe
# are the derived artefacts; the other two are tracked files that the seeding
# step rewrites, so a snapshot without them restores code that does not match
# its own registrations.
CONTENTS = [
    os.path.join("src", "recomp", "gen"),
    os.path.join("src", "recomp_manual.c"),
    os.path.join("build", "xmen_legends_recomp.exe"),
    "seeded_functions.json",
]


def git_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=REPO, capture_output=True, text=True,
                             timeout=30, stdin=subprocess.DEVNULL)
        return out.stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def signals():
    """The four tracked numbers from the last run, for the filename.

    The patterns come from smoke_spread.py rather than being copied here.
    Copying them is how they drift: the first version of this function used
    "ICALL FAILED" where the real marker is "Failed to resolve VA", and named
    a snapshot 54-0-2-8 for a build that had actually measured 54-4-2-8. A
    snapshot labelled with the wrong numbers is worse than an unlabelled one.
    """
    log = os.path.join(GAME, "stderr.txt")
    if not os.path.exists(log):
        return None
    sys.path.insert(0, HERE)
    try:
        from smoke_spread import SIGNALS
    except Exception:
        return None
    text = open(log, encoding="utf-8", errors="replace").read()
    return "-".join(str(len(re.findall(SIGNALS[k], text)))
                    for k in ("kernel_calls", "failed_icalls",
                              "heap_allocs", "safe_stub"))


def take(note):
    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    sig = signals()
    parts = [stamp, git_sha()]
    if sig:
        parts.append(sig)
    if note:
        parts.append(re.sub(r"[^A-Za-z0-9]+", "-", note).strip("-")[:40])
    name = "-".join(parts) + ".tar.gz"
    path = os.path.join(SNAP_DIR, name)

    missing = [c for c in CONTENTS if not os.path.exists(os.path.join(GAME, c))]
    if missing:
        print("warning: not present, skipping:")
        for m in missing:
            print("    %s" % m)

    with tarfile.open(path, "w:gz") as tf:
        for c in CONTENTS:
            full = os.path.join(GAME, c)
            if os.path.exists(full):
                tf.add(full, arcname=c)

    size = os.path.getsize(path) / (1024.0 * 1024.0)
    print("snapshot: %s" % path)
    print("  %.1f MB" % size)
    if sig:
        print("  signals (kernel-failed-heap-stub): %s" % sig)
    print("\nrestore with:")
    print("  py -3 tools_data/snapshot.py --restore %s" % name)


def show_list():
    if not os.path.isdir(SNAP_DIR):
        print("no snapshots yet (%s)" % SNAP_DIR)
        return
    names = sorted(f for f in os.listdir(SNAP_DIR) if f.endswith(".tar.gz"))
    if not names:
        print("no snapshots yet (%s)" % SNAP_DIR)
        return
    print("%s\n" % SNAP_DIR)
    for n in names:
        mb = os.path.getsize(os.path.join(SNAP_DIR, n)) / (1024.0 * 1024.0)
        print("  %-62s %6.1f MB" % (n, mb))


def restore(name):
    path = name if os.path.isabs(name) else os.path.join(SNAP_DIR, name)
    if not os.path.exists(path):
        sys.exit("no such snapshot: %s" % path)

    # Refuse to half-restore. A tree with the new gen/ and the old
    # recomp_manual.c does not link, and that failure looks like a code bug.
    with tarfile.open(path, "r:gz") as tf:
        members = tf.getnames()
        if not any(m.startswith("src/recomp/gen") for m in members):
            sys.exit("snapshot has no gen/ - refusing to restore a partial tree")
        gen = os.path.join(GAME, "src", "recomp", "gen")
        if os.path.isdir(gen):
            import shutil
            shutil.rmtree(gen)
        tf.extractall(GAME)

    print("restored %s" % os.path.basename(path))
    print("rebuild and confirm the numbers before trusting it:")
    print("  ./build_compile.bat && py -3 tools_data/smoke_spread.py 2")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", default="", help="short note for the name")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--restore", metavar="NAME")
    a = ap.parse_args()

    if not os.path.isdir(os.path.join(GAME, "src", "recomp")):
        sys.exit("run from src/game/")

    if a.list:
        show_list()
    elif a.restore:
        restore(a.restore)
    else:
        take(a.message)


if __name__ == "__main__":
    main()
