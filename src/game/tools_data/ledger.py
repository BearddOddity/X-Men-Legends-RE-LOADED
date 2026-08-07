#!/usr/bin/env python3
"""ledger.py - what has been tried, and what it measured. Especially failures.

Why this exists
---------------
On 2026-08-06 seven theories about one bug were stated confidently and all seven
were wrong. Nothing in the project stopped an eighth identical attempt the
following week - the refutations lived in a chat log and in my head, neither of
which a tool can consult.

The costly half is the failures. A disproven idea looks exactly as attractive
the second time, and the reasoning that made it attractive is usually still
sound-looking. So this records the reasoning AND the measurement that killed it,
keyed on the thing being claimed rather than on prose, so a later run can check
before repeating.

Worked examples from that day:

  "the count field is corrupted by a use-after-free"
      REFUTED - the writers turned out to be a manager legitimately iterating a
      list and updating each element through MEM32(esi + 8). Nothing was freed.

  "skipping the loop is free, since every call in it fails anyway"
      REFUTED by measurement - 1452 -> 56 kernel calls against an unchanged seed
      list. "Every call fails" is not "the loop has no effect".

  "the allocation failed, so the pointer is null"
      REFUTED - the allocation is a clean new + constructor and succeeded.

Entries are append-only. A claim is never edited to look better after the fact;
a superseding entry links to the one it replaces.

Usage (from src/game/):
    py -3 tools_data/ledger.py list
    py -3 tools_data/ledger.py check "use after free"
    py -3 tools_data/ledger.py add --claim "..." --verdict refuted \\
        --evidence "1452 -> 56 against an unchanged seed list" --tags loop,guard
    py -3 tools_data/ledger.py report
"""
import argparse
import contextlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
PATH = os.path.join(GAME, "ledger.json")
LOCKFILE = os.path.join(GAME, ".ledger.lock")

VERDICTS = ("refuted", "confirmed", "inconclusive", "superseded")


@contextlib.contextmanager
def locked(timeout=30, poll=0.1):
    """Advisory lock around a load-modify-save cycle.

    load()/save() are a plain read-then-write with no coordination of their
    own. walls.py can write here many times over an hours-long unattended
    run, and an interactive caller (the ledger MCP tool, or this module's own
    CLI) can write at the same moment - without this, the second save()
    silently overwrites the first writer's entry with no error. That is
    exactly the failure this file exists to prevent, just aimed at itself.

    Every caller that does load()+add()+save() must wrap the whole sequence
    in `with locked():` - locking only save() is not enough, since the race
    is in the gap between load() and save(), not in save() alone.
    """
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(LOCKFILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - os.path.getmtime(LOCKFILE) > 30:
                    os.remove(LOCKFILE)      # stale - a crashed holder
                    continue
            if time.time() > deadline:
                raise TimeoutError(
                    "ledger.json is locked by another writer - try again")
            time.sleep(poll)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(LOCKFILE)


def load():
    try:
        return json.load(open(PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return {"entries": []}


def save(db):
    with open(PATH, "w", encoding="utf-8") as fh:
        json.dump(db, fh, indent=1)
        fh.write("\n")


def words(s):
    return {w for w in re.findall(r"[a-z0-9_]+", s.lower()) if len(w) > 2}


IDENT_RX = re.compile(r"\b(?:sub_[0-9A-Fa-f]{8}|loc_[0-9A-Fa-f]{8}|"
                      r"0x[0-9A-Fa-f]{6,8})\b")


def identifiers(s):
    """Function names and addresses - exact technical identifiers, not prose."""
    return set(IDENT_RX.findall(s))


def similar(db, claim, threshold=0.34, identifiers_=()):
    """Entries sharing enough vocabulary with `claim` to be worth reading.

    Deliberately crude on prose - the job is to make a human or a tool pause
    and read, not to decide anything, so a fuzzy word-overlap ratio against
    `threshold` is enough there. IDENTIFIERS (sub_XXXXXXXX, loc_XXXXXXXX,
    0xADDR) are handled separately and are NOT crude: a shared function name
    is exact, not approximate, and the fuzzy path misses real matches when
    the rephrasing is loose. Measured on the 2026-08-06 use-after-free case:
    a claim describing the same bug in different words scored 0.27 by
    word-overlap alone, below this function's own 0.34 threshold - a false
    "nothing similar on record" for a claim that was, in fact, on record. An
    identifier match now bypasses that threshold entirely.

    `identifiers_` lets a caller pass known-relevant identifiers (e.g. the
    writer function names investigate.py already has) instead of relying on
    them appearing verbatim in `claim`'s free text.
    """
    want = words(claim)
    want_ids = identifiers(claim) | set(identifiers_)
    if not want and not want_ids:
        return []
    out = []
    for e in db["entries"]:
        haystack = e["claim"] + " " + e.get("evidence", "") + " " + \
            " ".join(e.get("tags", []))
        have = words(haystack)
        have_ids = identifiers(haystack) | set(e.get("tags", []))
        id_hit = want_ids & have_ids
        overlap = len(want & have) / len(want) if want else 0.0
        if id_hit:
            out.append((round(max(overlap, 0.5), 2), e))
        elif overlap >= threshold:
            out.append((round(overlap, 2), e))
    out.sort(key=lambda t: -t[0])
    return out


def add(db, claim, verdict, evidence, tags, supersedes=None):
    e = {"id": len(db["entries"]) + 1, "claim": claim, "verdict": verdict,
         "evidence": evidence, "tags": tags,
         "when": time.strftime("%Y-%m-%d %H:%M")}
    if supersedes:
        e["supersedes"] = supersedes
    db["entries"].append(e)
    return e


def seed_from_today(db):
    """The seven wrong turns from 2026-08-06, so the record starts honest."""
    if db["entries"]:
        return 0
    rows = [
        ("The bad loop count is caused by a use-after-free: the memory was "
         "released and handed to another object.", "refuted",
         "The writers are sub_001F7930 iterating a list and updating each "
         "element via MEM32(esi + 8) - a field, not an object base. Nothing was "
         "freed. investigate.py's classifier asserted this and was wrong.",
         ["use-after-free", "count", "sub_001F7930", "classifier"]),
        ("Skipping the dispatch loop is free, because every call in it targets "
         "0 and fails, so the loop achieves nothing.", "refuted",
         "Measured 1452 -> 56 kernel calls against an unchanged seed list. "
         "'Every call fails' is not 'the loop has no effect' - it still "
         "advances an index and the failing calls run through the safe stub, "
         "which touches esp and eax.",
         ["count-clamp", "guard", "loop", "sub_001F7930"]),
        ("The 16-byte allocation is failing and returning null.", "refuted",
         "The allocation site is a clean operator-new plus constructor and it "
         "succeeds; 0x030FEFE8 is a legitimately allocated buffer.",
         ["allocator", "null", "allocation"]),
        ("esp escaping the 8 MB stack means the stack is corrupted.", "refuted",
         "0x03DAB1C8 is the worker thread's own stack, allocated from guest "
         "heap by PsCreateSystemThreadEx. triage_crash's heuristic does not "
         "model the second thread.",
         ["stack", "esp", "thread", "false-positive"]),
        ("Engine global 0x5BC508 is never initialised.", "refuted",
         "Written once by sub_00239E50+0x15C, before the loop that reads it.",
         ["globals", "0x5BC508", "init"]),
        ("The boot is deterministic, so a single run is a sound measurement.",
         "inconclusive",
         "The three gated counters repeat exactly, but total dispatch volume "
         "swings 8x between runs (622M vs 74.9M). Stable where the gate looks; "
         "not stable in general.",
         ["determinism", "measurement"]),
        ("kernel_calls is an adequate progress signal.", "refuted",
         "It read exactly 1452 across 566 seeded functions, 31 lifter repairs "
         "and a fixed freeze. It saturates where the boot stops. Replaced as "
         "the primary gate by `reached` (distinct dispatch targets), first "
         "measurement 692.",
         ["metric", "gate", "kernel_calls", "reached"]),
        ("A missing function is what blocks the boot.", "refuted",
         "566 functions that vtables provably call were seeded and measured "
         "safe; kernel_calls did not move. Worth re-testing against `reached`, "
         "which did not exist at the time.",
         ["seeding", "missing-functions", "wall"]),
    ]
    for claim, verdict, evidence, tags in rows:
        add(db, claim, verdict, evidence, tags)
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ledger", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list")
    sub.add_parser("report")
    c = sub.add_parser("check")
    c.add_argument("claim")
    a_ = sub.add_parser("add")
    a_.add_argument("--claim", required=True)
    a_.add_argument("--verdict", required=True, choices=VERDICTS)
    a_.add_argument("--evidence", required=True)
    a_.add_argument("--tags", default="")
    a_.add_argument("--supersedes", type=int)

    a = ap.parse_args(argv)
    with locked():
        return _run(a)


def _run(a):
    db = load()
    n = seed_from_today(db)
    if n:
        save(db)
        print(f"seeded {n} entries from the 2026-08-06 session\n")

    if a.cmd == "check":
        hits = similar(db, a.claim)
        if not hits:
            print("nothing similar on record - this looks new")
            return 0
        print(f"{len(hits)} similar claim(s) already on record:\n")
        for score, e in hits:
            print(f"  [{e['verdict'].upper()}] (match {score}) #{e['id']} {e['claim']}")
            print(f"      evidence: {e['evidence']}\n")
        if any(e["verdict"] == "refuted" for _, e in hits):
            print("At least one is REFUTED. Read the evidence before spending "
                  "time on this.")
            return 1
        return 0

    if a.cmd == "add":
        e = add(db, a.claim, a.verdict,
                a.evidence, [t for t in a.tags.split(",") if t], a.supersedes)
        save(db)
        print(f"recorded #{e['id']} as {e['verdict']}")
        return 0

    if a.cmd == "report":
        by = {}
        for e in db["entries"]:
            by.setdefault(e["verdict"], []).append(e)
        out = ["# Ledger", "",
               f"{len(db['entries'])} claim(s) on record.", ""]
        for v in VERDICTS:
            if v not in by:
                continue
            out += [f"## {v.title()} ({len(by[v])})", ""]
            for e in by[v]:
                out += [f"**#{e['id']} {e['claim']}**", "",
                        f"{e['evidence']}", ""]
        p = os.path.join(GAME, "ledger_report.md")
        open(p, "w", encoding="utf-8").write("\n".join(out) + "\n")
        print(f"wrote {os.path.basename(p)}")
        return 0

    if not db["entries"]:
        print("empty")
        return 0
    for e in db["entries"]:
        print(f"  [{e['verdict']:<12}] #{e['id']} {e['claim'][:88]}")
    print(f"\n{len(db['entries'])} entries. "
          f"`ledger.py check \"<idea>\"` before spending time on one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
