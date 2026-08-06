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
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
PATH = os.path.join(GAME, "ledger.json")

VERDICTS = ("refuted", "confirmed", "inconclusive", "superseded")


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


def similar(db, claim, threshold=0.34):
    """Entries sharing enough vocabulary with `claim` to be worth reading.

    Deliberately crude. The job is to make a human or a tool pause and read,
    not to decide anything - a false hit costs a glance, a miss costs a repeat
    of a day already spent.
    """
    want = words(claim)
    if not want:
        return []
    out = []
    for e in db["entries"]:
        have = words(e["claim"] + " " + " ".join(e.get("tags", [])))
        if not have:
            continue
        overlap = len(want & have) / len(want)
        if overlap >= threshold:
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
