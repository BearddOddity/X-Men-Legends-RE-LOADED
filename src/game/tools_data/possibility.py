#!/usr/bin/env python3
"""possibility.py - try each competing explanation and let the numbers pick.

Why this exists
---------------
Investigations on this project keep ending at a fork: two readings of the same
evidence, both plausible, and picking wrong means editing working code on a
guess. Ledger #60 is the current example - MEM32(pool + 4) is written as a
refcount by one function and read as a deferred-free array slot by another, and
either the pointer is wrong or the array base is. The honest answer is to try
both and measure, but doing that by hand is four build/run cycles of careful
bookkeeping, and the bookkeeping is exactly what goes wrong at 2am.

What it guarantees
------------------
1. BACKUPS ARE MANDATORY AND VERIFIED. Every file a variant touches, plus
   seed_list.json and tools_data/manual_edits.json, is copied to a timestamped
   directory BEFORE anything is modified, and each copy is re-read and hash-
   compared against the original. If any backup cannot be made or verified the
   run aborts having changed nothing. The 2026-08-05 entry records a 25x
   regression that was cheap to undo only because the list had been backed up
   first; this makes that unskippable rather than remembered.
2. THE TREE IS RESTORED. Always, including on exception and on Ctrl-C, via a
   finally block that restores from the verified backups and then hash-checks
   the result. `--keep <id>` re-applies one variant at the end, deliberately.
3. A BASELINE IS MEASURED FIRST. A variant is only meaningful against the
   numbers the tree produces right now, not against a remembered figure.
4. `reached` DECIDES. signals.py already defines direction and puts `reached`
   first because it is the only signal with resolution - kernel_calls is a
   logging cap (ledger #33/#34) and reading it as progress threw away a good
   fix once. This tool never invents its own ranking.
5. WORST-OF ACROSS RUNS. A variant must be good on every run to win; it can
   lose to noise but never win by it.

What it does NOT do
-------------------
It does not decide whether an edit is faithful to the original x86. A variant
that scores better may still be scaffolding rather than a port fix - project
rule #11. The report says which won; a human says whether it is right.

Spec format (JSON)
------------------
    {
      "name": "pool+4 aliasing",
      "question": "is esi+0xB0 wrongly the pool, or is the array base off?",
      "runs": 2,
      "variants": [
        {"id": "A",
         "hypothesis": "one sentence, stated BEFORE measuring",
         "edits": [{"file": "src/recomp/gen/recomp_0015.c",
                    "old": "exact text", "new": "replacement"}]}
      ]
    }

`old` must appear EXACTLY ONCE in the file or the variant is skipped - an
ambiguous anchor silently editing the wrong site is the failure mode this
refuses to have.

Usage (from src/game/):
    py -3 tools_data/possibility.py spec.json
    py -3 tools_data/possibility.py spec.json --runs 3
    py -3 tools_data/possibility.py spec.json --keep A
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import signals  # noqa: E402
try:
    import recomp_lock
except Exception:
    recomp_lock = None

BACKUP_ROOT = os.path.join(GAME, "possibility_backups")
REPORT = os.path.join(GAME, "possibility_report.md")

# Always backed up, whether or not a variant edits them: the seeder and the
# manual-edit store are the two things whose loss is expensive and silent.
ALWAYS_BACKUP = ["seed_list.json", "tools_data/manual_edits.json"]


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Backups:
    """Verified copies of everything we may touch. Refuses to be optional."""

    def __init__(self, paths):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = os.path.join(BACKUP_ROOT, stamp)
        os.makedirs(self.dir, exist_ok=True)
        self.entries = {}
        for rel in sorted(set(paths)):
            src = os.path.join(GAME, rel)
            if not os.path.exists(src):
                continue
            dst = os.path.join(self.dir, rel.replace("/", "__").replace("\\", "__"))
            shutil.copy2(src, dst)
            want, got = sha(src), sha(dst)
            if want != got:
                raise RuntimeError("backup of %s did NOT verify - aborting" % rel)
            self.entries[rel] = (dst, want)
        if not self.entries:
            raise RuntimeError("nothing was backed up - refusing to continue")
        print("backups verified in %s (%d file(s))" % (self.dir, len(self.entries)))

    def restore(self, quiet=False):
        bad = []
        for rel, (dst, want) in self.entries.items():
            src = os.path.join(GAME, rel)
            shutil.copy2(dst, src)
            if sha(src) != want:
                bad.append(rel)
        if bad:
            raise RuntimeError("RESTORE FAILED for: %s - backups are in %s"
                               % (", ".join(bad), self.dir))
        if not quiet:
            print("tree restored from backup and hash-verified")


def sh(cmd):
    return subprocess.run(cmd, cwd=GAME, capture_output=True, text=True)


def build():
    r = sh(["cmd", "/c", os.path.join(GAME, "build_compile.bat")])
    blob = (r.stdout + r.stderr).lower()
    return not (r.returncode and "error" in blob)


def crash_site():
    """The crash/hang line from the last run, or None.

    NOT a gated signal - it is text, and signals.py deliberately owns what
    gates. But a MOVED crash is often the only thing that changes when a
    variant is right and the counters are flat: every real fix on 2026-08-07
    moved the crash, and twice it changed category (access violation <->
    divide-by-zero) while kernel_calls stood still. Reporting counters alone
    would have called those runs identical.

    The absolute RIP is stripped: it moves with ASLR on every run, so
    comparing the raw line reports a "MOVED" crash for two runs of the SAME
    build - which this tool did on its first outing, flagging a variant as
    changed when only the load address had. Only the RVA and the fault
    address are kept, which are the parts that identify where it died.
    """
    log = os.path.join(GAME, "stderr.txt")
    if not os.path.exists(log):
        return None
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("[CRASH]") or "[WATCHDOG]" in line:
                    line = line.strip()
                    rva = re.search(r"RVA=(0x[0-9A-Fa-f]+)", line)
                    fault = re.search(r"fault addr=(0x[0-9A-Fa-f]+)", line)
                    kind = "hang" if "WATCHDOG" in line else "crash"
                    if rva:
                        return "%s RVA=%s%s" % (
                            kind, rva.group(1),
                            " fault=%s" % fault.group(1) if fault else "")
                    return re.sub(r"RIP=0x[0-9A-Fa-f]+", "RIP=<aslr>", line)[:120]
    except OSError:
        return None
    return None


def measure(runs):
    """Worst-of across `runs`; None if any run produced no fresh log.

    Returns (signals, crash_site_text).
    """
    acc = None
    site = None
    for _ in range(max(1, runs)):
        sh(["cmd", "/c", os.path.join(GAME, "run.bat")])
        sig = signals.read()
        if sig is None or sig.get("stale"):
            return None, None
        site = crash_site()
        acc = sig if acc is None else signals.worst_of(acc, sig)
    return acc, site


def apply_edits(edits):
    """Apply a variant. Returns None on success, else a reason string."""
    staged = []
    for e in edits:
        path = os.path.join(GAME, e["file"])
        if not os.path.exists(path):
            return "missing file %s" % e["file"]
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
            text = f.read()
        n = text.count(e["old"])
        if n != 1:
            return "anchor appears %d time(s) in %s (need exactly 1)" % (n, e["file"])
        staged.append((path, text.replace(e["old"], e["new"], 1)))
    for path, text in staged:
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(text)
    return None


def rank_key(sig):
    """Order by signals.GATED, `reached` first - see the module docstring."""
    if sig is None:
        return ()
    out = []
    for name, direction in signals.GATED.items():
        v = sig.get(name, 0)
        out.append(v if direction == "up" else -v)
    return tuple(out)


def describe(sig):
    if sig is None:
        return "UNMEASURABLE (no fresh log)"
    parts = ["%s=%s" % (n, sig.get(n, "?")) for n in signals.GATED]
    if sig.get("hung"):
        parts.append("HUNG")
    return "  ".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--runs", type=int, default=None,
                    help="runs per variant (default: spec's, else 2)")
    ap.add_argument("--keep", help="re-apply this variant id at the end")
    ap.add_argument("--force", action="store_true",
                    help="bypass the build lock's ACTIVITY HEURISTIC only. "
                         "This tool builds and runs constantly, so its own "
                         "exhaust trips that heuristic; a genuinely held lock "
                         "still refuses, so this cannot stomp an AFK run.")
    args = ap.parse_args(argv)

    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)
    runs = args.runs or spec.get("runs") or 2
    variants = spec["variants"]
    ids = [v["id"] for v in variants]
    if args.keep and args.keep not in ids:
        sys.exit("--keep %s is not one of %s" % (args.keep, ids))

    print("possibility: %s" % spec.get("name", args.spec))
    if spec.get("question"):
        print("question   : %s" % spec["question"])
    print("variants   : %s   runs each: %d\n" % (", ".join(ids), runs))

    touched = list(ALWAYS_BACKUP)
    for v in variants:
        for e in v["edits"]:
            touched.append(e["file"])

    lock = None
    if recomp_lock is not None:
        try:
            lock = recomp_lock.build_lock("possibility", force=args.force)
            lock.__enter__()
        except SystemExit as exc:
            # build_lock raises SystemExit, which is a BaseException - an
            # `except Exception` here would let it through and the tool would
            # exit without saying why.
            sys.exit("%s\n(pass --force to bypass the ACTIVITY HEURISTIC only; "
                     "a genuinely held lock still refuses)" % exc)

    backups = Backups(touched)      # raises if it cannot verify
    results = {}
    try:
        print("\n--- baseline (tree as-is) ---")
        if not build():
            raise RuntimeError("baseline build FAILED - fix the tree first")
        base, base_site = measure(runs)
        print("  %s" % describe(base))
        print("  crash: %s" % (base_site or "(none)"))
        if base is None:
            raise RuntimeError("baseline unmeasurable - nothing to compare against")

        for v in variants:
            print("\n--- variant %s ---" % v["id"])
            if v.get("hypothesis"):
                print("  hypothesis: %s" % v["hypothesis"])
            backups.restore(quiet=True)
            why = apply_edits(v["edits"])
            if why:
                print("  SKIPPED: %s" % why)
                results[v["id"]] = ("skipped", None, why, None)
                continue
            if not build():
                print("  BUILD FAILED")
                results[v["id"]] = ("build-failed", None, None, None)
                continue
            sig, site = measure(runs)
            print("  %s" % describe(sig))
            moved = site != base_site
            print("  crash: %s%s" % (site or "(none)", "   <== MOVED" if moved else ""))
            worse = signals.worse_than(sig, base) if sig else ["unmeasurable"]
            results[v["id"]] = ("ok", sig, worse, site)
    finally:
        backups.restore()
        if lock is not None:
            try:
                lock.__exit__(None, None, None)
            except Exception:
                pass

    # ---- report -----------------------------------------------------------
    lines = []
    lines.append("# possibility: %s\n" % spec.get("name", args.spec))
    if spec.get("question"):
        lines.append("**Question:** %s\n" % spec["question"])
    lines.append("Runs per variant: %d. Ranked by `reached` first "
                 "(signals.py order).\n" % runs)
    lines.append("| variant | outcome | signals | crash | vs baseline |")
    lines.append("|---|---|---|---|---|")
    lines.append("| _baseline_ | - | %s | `%s` | - |"
                 % (describe(base), base_site or "none"))
    ok = []
    moved_any = False
    for v in variants:
        state, sig, extra, site = results.get(v["id"], ("?", None, None, None))
        if state == "ok":
            note = "no regression" if not extra else "WORSE: " + ", ".join(extra)
            if rank_key(sig) > rank_key(base):
                note = "BETTER"
            ok.append((rank_key(sig), v["id"], sig))
        else:
            note = extra or state
        moved = site is not None and site != base_site
        moved_any = moved_any or moved
        lines.append("| %s | %s | %s | `%s`%s | %s |"
                     % (v["id"], state, describe(sig), site or "none",
                        " **MOVED**" if moved else "", note))
    lines.append("")
    if ok:
        ok.sort(reverse=True)
        best_key, best_id, best_sig = ok[0]
        if best_key > rank_key(base):
            lines.append("**Winner: %s** - the only variant that beats the "
                         "baseline on the gated signals.\n" % best_id
                         if len([k for k, _, _ in ok if k > rank_key(base)]) == 1
                         else "**Best: %s.**\n" % best_id)
        elif moved_any:
            lines.append("**No variant beat the baseline on the counters, but a "
                         "crash MOVED.** That is a real behavioural difference the "
                         "gated signals cannot see - read the moved variant's new "
                         "crash before dismissing it.\n")
        else:
            lines.append("**No variant changed anything** - not the counters, not "
                         "even the crash site. Either both readings are wrong, or "
                         "the edited code does not execute on this boot. Check it "
                         "runs at all before theorising further; that mistake has "
                         "been made on this project more than once.\n")
    lines.append("Tree was restored from verified backups in `%s`.\n"
                 % os.path.relpath(backups.dir, GAME))
    lines.append("A better score is not proof of faithfulness (rule #11) - "
                 "confirm the winning edit against the original x86 before keeping it.\n")
    text = "\n".join(lines)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(text)
    print("\n" + text)
    print("report written to %s" % os.path.relpath(REPORT, GAME))

    if args.keep:
        v = next(x for x in variants if x["id"] == args.keep)
        why = apply_edits(v["edits"])
        if why:
            sys.exit("--keep %s could not be applied: %s" % (args.keep, why))
        print("re-applied variant %s; rebuild before measuring again" % args.keep)


if __name__ == "__main__":
    main()
