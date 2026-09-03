# Open regressions and defects

Findings from the audit of 2026-09-03. Each is stated so it can be picked up and
fixed on its own, without re-deriving how it was found. Nothing here is fixed —
this is the worklist.

Audit method: the tree was snapshotted first, both mirrored repos were confirmed
pushed, and the two repos with no remote were bundled to
`D:\My Games\recomp-snapshots\repo-bundles\`. Every check below is read-only
except the stub census, which instrumented a copy and was restored and
re-verified afterwards.

## What the audit confirmed is real

Stated first, because the rest of this file is problems and that would otherwise
read as the whole picture.

- **The current build reproduces its claimed numbers.** Rebuilt from source and
  run twice: 230 kernel calls, 136 heap allocations, 292 failed indirect calls,
  196 reached VAs, 551 call sites. Identical both runs.
- **196 reached and 551 call sites exceed every figure in the entire 193-entry
  history.** The highest previously recorded anywhere, including prose, are 170
  and 481.
- **All 1,093 recorded manual edits are physically present**, tested by exact
  block text rather than by the tool's own report. Nothing has been silently
  lost.
- **Seeds are intact**: 1,656 of 1,656 seeded addresses have bodies, and there
  are no duplicate function definitions anywhere in `gen/`.
- **The 2026-08-09 figures of 4,000 kernel calls and 826 heap allocations are
  not a lost high-water mark.** Entry #143 documents the change that dropped
  them as removing a page-zero junk dispatch — they were a runaway, not
  progress. Today's numbers are not a regression against them.

---

## R1 — the primary tracked signal is neither recorded nor checked

`signals.py` names `reached` as the first gated signal, "FIRST because it is the
only signal with any resolution". But `progress.json` has no `reached` or
`callsites` field, and `progress.py`'s `moved()` never tests either.

**Consequence.** Rule #14's stall detector is blind to the one signal that moves
when a fix works but the boot still stops in the same place. Only 39 of 193
entries mention `reached` at all, and only in prose. Today's records are
provable from the run log but not from the history.

**Fix.** Add `reached` and `callsites` to the recorded fields and to `moved()`.
Backfill what can be parsed out of existing messages; leave the rest null rather
than guessing.

## R2 — `progress.py` advertises a best that the project itself documented as junk

Every run prints `best: 4000 kernel calls`. That 4,000 is the watchdog clip on a
runaway, and entry #143 says so directly: the change that ended it removed "the
page-0 junk dispatch". So each session is told it stands 17x below a record that
was never real, which is exactly the misleading-number problem rule #8 exists to
prevent.

**Fix.** Exclude clipped runs from the best-ever calculation, or label them. The
clip value is a constant and easy to detect.

## R3 — `sub_0020B8C6` is an unresolved stub and is entered on every run

`0x0020B8C6` is a two-instruction island (`mov ebx, edx` then `jmp 0x0020B88D`).
Because it is a stub whose whole body is `g_esp += 4`, `sub_0020B850` returns
without its epilogue, leaving `ebx`/`esi`/`edi` unrestored.

The correct inline fix is written out verbatim in ledger #159 and is **held**,
not missing: applying it takes the boot from 582 kernel calls to 48, because the
broken stub had been returning early and skipping work that then runs and fails.

**This is an active wrong behaviour, not a dormant one.** The census below
confirms it is entered every run.

**Fix.** Blocked on whatever fails once the skipped work actually runs. Re-apply
ledger #159's patch and follow the new fault.

## R4 — `sub_001995AD` is an unresolved stub and is entered on every run

Ledger #158 refuted seeding it: `seed_missing_functions.py` derives extent by
scanning forward to a `ret` and gave it 693 bytes, when its enclosing function
`sub_001994F0` is only 41 bytes (`0x001994F0-0x00199519`). The seed spanned
several unrelated functions and measured 578 to 244 kernel calls.

So it remains a stub, and it is entered every run. There is currently **no fix
path** — seeding is wrong and the correct extent is unknown.

**Fix.** Determine what `0x001995AD` actually belongs to. It is mid-function
code; find the real owner and whether it is a continuation that needs inlining
rather than seeding.

## R5 — NEW: `0x0006702C` is an unresolved stub and is entered on every run

Not present in the earlier census, which ran before the paths opened up. Its
shape is the same class as the defect fixed today:

```
0x0006702c: dec  eax
0x0006702d: mov  dword ptr [ecx + eax*4 + 0x644], edx   ; an array store
0x00067034: inc  edx
0x00067035: cmp  edx, 0x64
0x00067038: jl   0x67000                                 ; loop back
0x0006703a: pop  edi                                     ; register restore
```

An array store, a loop, and a register restore — exactly the shape of
`sub_00208645`, whose absence was today's root defect. Skipping it drops a
hundred-iteration table fill and at least one `pop`.

**Fix.** Establish its owning function and extent, then seed or inline per the
rule in ledger #158: seed only when the extent stays inside one function.

## R6 — `0x00340F24` is entered every run and the seeder cannot repair it

`seed_missing_functions.py` refuses it with "no terminating ret found", so the
existing tool has no answer. It has been hit in both censuses.

**Fix.** Either extend the extent finder to handle whatever terminator this
function uses, or determine the extent by hand and seed from an explicit range.

## R7 — one undocumented drop in the recorded history

Entry #26 (2026-08-02), kernel calls 92 to 72, with no note. Every other large
drop in 193 entries carries an explanation. Low priority, but it is the only
gap in an otherwise complete record.

## R8 — `manual_edits.py apply` duplicates three functions on every run

Deterministic and reproduced three times: each `apply` writes a second copy of
`sub_001EA600`, `sub_001EA640` and `sub_001EA6B0` into `recomp_0014.c`, one of
them a hybrid carrying `loc_001EA5EF`, a label belonging to `sub_001EA5E0`.

`tools_data/dedupe_functions.py` now repairs it, so this no longer costs a build
— but the duplication itself is unfixed, and the repair must be run after every
`apply`. Ledger #163 and #166.

**Fix.** Find why `apply` re-emits the region. Likely a `wrap` record whose
`wrapped` content spans more than it should.

## R9 — `manual_edits.py verify` reports placeable as though it were present

`verify` prints `would place 1093/1093` whether or not the edits are actually in
the tree. That is how four edits, including ledger #136's carry-flag fix, were
silently lost earlier today and a whole baseline was measured wrong.

**Fix.** Add a presence check: for each record, count exact occurrences of its
block in the target file and report any group with fewer copies than records.
That test is what this audit used and it found the tree clean; it belongs in the
tool, not in an audit script.

---

## Preservation risk, outside the code

`oddity-skills` — the local marketplace holding the `oddity-re` plugin and all
twelve of its skills — **has no git remote**. It is committed, but on one disk.
A bundle now exists at
`D:\My Games\recomp-snapshots\repo-bundles\oddity-skills-*.bundle`, which is a
backup and not a mirror.

**Fix.** Add a remote. That is an outward-facing action and needs an explicit
decision, so it is recorded here rather than done.
