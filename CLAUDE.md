# X-Men Legends → PC port

Static recompilation of the Xbox XBE into a native Windows executable.
**Definition of done: boots to the main menu.**

**Ship no game data.** Same model as OpenGOAL and UnleashedRecomp: the user
brings their own disc, and everything derived is built on their machine.
`src/game/src/recomp/gen/` is gitignored — 1M+ lines mechanically derived from a
copyrighted XBE, never commit it. Nor the XBE, an ISO, or anything under
`game/`.

`tools/hooks/pre-commit` enforces this — `.gitignore` only filters, and
`git add -f` walks straight past it. Install with `tools/hooks/install.sh`.

A remote is allowed. (Superseded 2026-08-04: this file used to say "never push,
local-only". That rule existed because the game was expected to live in the tree
too; it doesn't — it's gitignored — so the reason no longer holds.)

---

## The 15 rules

Lower number wins, with two exceptions: **#10 outranks #2**, and **#11/#12 are
always-on**, not tie-breakers. Full text and the incident behind each:
`~/.claude/projects/D--My-Games-Xbox-Recomp/memory/feedback_project_rules.md`.

1. **No regression in correctness.** Build + smoke run before and after. The
   kernel-call count is a *proxy* — when a change alters which path runs, the
   counts measure two different programs. On a dip, weigh #8's second signals
   and check in; don't revert reflexively or keep it silently.
2. **Code clean** — only where provably inert. #1 and #10 outrank this.
3. **Section the work** into phases with checkpoints and clean hand-offs.
4. **Build a tool** when a task is done by hand twice. Inventory below.
5. **Probe, don't guess.** Prove the cause with a print before editing.
6. **Fix only as wide as the evidence.** Narrow a pattern with run data first.
7. **One change per build, two runs per number.** Behaviour changes only —
   probes, tools and docs can be batched.
8. **A fix needs a second signal.** A rising count alone can just mean the
   crash moved.
9. **A bug seen twice is a class.** Write a detector; fix the lifter when the
   fix can live there (it survives regeneration; a `gen/` edit does not).
10. **Don't tidy what you can't prove is inert.** The heap lower bound
    `0x00880000` is wrong *and load-bearing* — "fixing" it cost 61→59.
11. **PC port, never an emulator.** Translate to modern equivalents (D3D8 → a
    real modern backend). No NV2A behaviour, no pushbuffer interpreter, no
    cycle accuracy. Recompilation is static, never interpreted or JIT.
12. **Write for dyslexia.** Short sentences, short paragraphs, bullets, plain
    words, **bold** the key phrase, answer first. Every response.
13. **Track progress in the file, not from memory** — `progress.py`.
14. **Don't run in circles.** Trigger is mechanical: two consecutive changes
    improving no tracked signal → `progress.py stalled` → then stop and pick
    out loud: go around / escalate / switch targets. Revert a change that made
    things worse *immediately*.
15. **Never repeat a solved mistake.** Build the check on the 2nd occurrence,
    not the 4th. Once a tool exists, **use it** — don't hand-roll what it does.

---

## Tools — `src/game/tools_data/`, run from `src/game/`

Reach for these before hand-editing. Hand-rolling what a tool does is how
solved bugs come back (#15).

| tool | use |
|---|---|
| `triage_crash.py` | crash → function, source line, register analysis. `--grep` finds the faulting expression, `--icall` resolves failure backtraces to callers |
| `add_probe.py` | emit a debug probe with correct escaping |
| `strip_probes.py` | remove probes; refuses any removal that would unbalance braces |
| `add_guard.py` | emit the standard pointer-plausibility guard |
| `progress.py` | `record` a verified result, no args to show history, `stalled` for the #14 check |
| `whatis.py` | identify any Xbox VA — section, owning function, disassembly |
| `find_missing_functions.py` | functions reachable only via data pointers |
| `seed_missing_functions.py` | recompile those additively into `gen/recomp_seed.c` |
| `find_icall_esp_saves.py` | `_icall_esp` save points across register saves; `--live` narrows to real failures, `--fix --only F` applies |
| `find_stale_flag_tests.py` | deferred-flag miscompiles |
| `manual_edits.py` | extract/re-apply hand edits across a regeneration; `--partial --force` to write what places, `check-braces` to verify |
| `repair_wraps.py` | close or drop wrapping guards a regeneration left unbalanced |
| `stub_overridden.py` | remove generated bodies that hand-written overrides replace (fixes LNK2005) |
| `audit_kernel_ordinals.py` | cross-check the bridge's two ordinal tables |
| `snapshot.py` | archive/restore a known-good `gen/` + exe outside the repo; `--list`, `--restore` |
| `find_reg_clobbers.py` | functions that write ebx/esi/edi without saving them; `--only F --callees` walks a subtree |
| `walk_chain.py` | follow a tail-call chain and account for a callee-saved register across all paths |
| `dump_table.py` | dump a VA range as dwords, naming each entry - function-pointer tables |
| `normalise_seed_names.py` | rewrite named call targets in the seed to their `sub_ADDRESS` form |
| `fix_stub_purge.py` | make unresolved stubs pop the fake return address |
| `dedupe_seed.py` | drop seeded functions the main sweep now discovers (fixes LNK2005 after a regeneration) |
| `test_mmx_helpers.c` | standalone check of the packed-integer helpers - lane order, saturation, packing |

## Traps — enforced by tools, not memory

- **Shell heredocs mangle backslash escapes.** Never build a C string literal
  through one — it corrupted `\n` four separate times. Use `add_probe.py`, or
  write a `.py` file and run it.
- **`tar` on Windows reads `C:` as a remote host** — pass `--force-local`.
- **The fake TIB at VA 0 is mapped**, so null dereferences return plausible
  garbage instead of faulting. Bugs surface far from their cause.
- **`esp` is simulated and drifts.** Lifted code that reads relative to `esp`
  across a call is exposed; prefer a saved frame pointer where one exists.
- **An unresolved stub is never harmless.** The call site pushes a fake
  return address the callee must pop, so an empty body leaks stack on every
  call - and a stub that ends a tail-call chain also swallows the
  `PUSH32`/`POP32` pairs restoring `ebx`/`esi`/`edi`. Seed it:
  `seed_missing_functions.py --stubs --tail-only --apply`.
- **Backtraces were wrong before 2026-08-04.** `triage_crash.py` read only
  MSVC's *Publics* section, missing 25,207 of 55,789 symbols, and attributed
  every address to the nearest *loaded* symbol with a plausible offset.
  Re-check any conclusion that rested on a call stack.
- **Callee-saved registers are unenforced.** `ebx`/`esi`/`edi` are globals;
  only the emitted `PUSH32`/`POP32` pairs uphold the contract. Build with
  `-DRECOMP_CHECK_ABI=ON` to verify every direct call - it found a five-deep
  corruption chain in one run. Indirect calls are not wrapped yet.
- **Conclusions already disproven this project - don't re-chase them:**
  - "Our heap overwrote the game's buffer" - no, it was the allocator
    correctly zeroing a fresh block; a byte-exact write-watch confirmed it.
  - "`manual_edits.py` drops ~85 edits" - no, that was a re-run artefact from
    applying the store to an already-applied tree. The real loss is 7 guards,
    found by diffing two independently-built trees (see `gen/` section above).
  - "The XAPI rename is *why* regeneration loses ~22 calls" - it's a real bug
    (fixed), but not the cause; the loss persisted after fixing it.
  - "The 195 seed addresses the classifier skips are junk" - no, they're real
    mid-function call targets something genuinely calls; skipping them made
    the build *worse* (8 unresolved → 80).
  - Pattern behind all four: trusting a number before checking how it was
    produced. Verify the instrument, not just the result.

## Regenerating

Expensive the first time, mechanical after. Back up `gen/` first, then:

```bash
py -3 -m tools.recomp game/default.xbe --all --split 1000     --gen-dir <scratch>/gen_new --functions seeded_functions.json --skip-binary-check
# Copy in BOTH .c AND .h - `cp <scratch>/gen_new/*.c` alone leaves the old
# recomp_funcs.h beside new sources, and the only symptom is one undeclared
# identifier from recomp_dispatch.c (observed: sub_001A1C97, C2065). It reads
# like a generator bug and is not.
cp <scratch>/gen_new/*.c <scratch>/gen_new/*.h src/recomp/gen/
# recomp_seed.c is NOT regenerated - it comes from seeding. Copy, never sync.
# Copy-Item PRESERVES mtimes, so copied files look OLDER than the .obj files
# and ninja silently skips recompiling them. Symptom: hundreds of phantom
# LNK2019 from a half-old object set. Git-bash `cp` does not do this.
touch src/recomp/gen/*.c src/recomp/gen/*.h src/recomp_manual.c
# recomp_seed.c is NOT regenerated by the lifter. Rebuild it, and do it BEFORE
# stub_overridden.py - that step strips generated bodies it believes
# recomp_seed.c provides, so a stale seed file makes it strip too many.
py -3 tools_data/seed_missing_functions.py --from-list seed_list.json --apply
py -3 tools_data/manual_edits.py apply     # short first pass is normal, see below
py -3 tools_data/repair_wraps.py --apply --drop-unclosed
py -3 tools_data/find_icall_esp_saves.py --fix --only sub_00209650,sub_002235D0,sub_00226250,sub_00236500
py -3 tools_data/stub_overridden.py --apply
py -3 tools_data/dedupe_seed.py --apply            # better discovery -> seed duplicates
py -3 tools_data/manual_edits.py check-braces      # must say "all functions balanced"
# A short first pass is NORMAL: the steps above restore anchors it could not
# find. Re-run it afterwards - it should then place all 154. `apply` is
# idempotent, so re-running is free. Only investigate if the second pass is
# short too - and read its output carefully, because it now separates guards
# that are MISSING from the tree from ones merely mis-anchored. Only the
# first kind is a loss.
# The __SEH_epilog save-area replacement is RETIRED - rejected 3x (40/50/52
# against a 200 baseline). It compensated for esp drift that the fall-through
# fix and the g_seh_ebp sync removed at source. Do not re-add it.
py -3 tools_data/manual_edits.py extract           # re-sync the store
# NEVER run `extract` after seeding. recomp_seed.c lives in gen/, so extract
# captures all ~1,000 seeded bodies as "manual edits" (139 -> 1042 entries).
# The next regeneration then cannot place them and the tree stops linking.
```

## The loop

```bash
./build_compile.bat && ./run.bat            # build, run
py -3 tools_data/triage_crash.py --grep     # where and why
py -3 tools_data/add_probe.py ...           # prove it (#5)
py -3 tools_data/add_probe.py --where ...   # ...and who called it
py -3 tools_data/triage_crash.py --where    # resolve those backtraces
py -3 tools_data/strip_probes.py --apply    # clean up
./smoke_test.ps1                            # REGRESSION GATE - run before
                                            # recording anything. Gates all
                                            # four signals plus determinism;
                                            # exit 1 on any regression.
                                            # -BaselineUpdate after a genuine
                                            # improvement, never to make a
                                            # failure pass.
./build_compile.bat && ./run.bat            # verify twice (#7)
py -3 tools_data/progress.py record -m "…"  # (#13)
py -3 tools_data/progress.py stalled        # (#14)
```

Back up `gen/` before anything risky - **including every regeneration**:
```bash
py -3 tools_data/snapshot.py -m "before <whatever>"
```
Skipping this exactly once turned an untar into an hour-long rebuild that
did not even land on the same numbers.

## `gen/` IS reproducible as of 2026-08-05

A regeneration now reproduces 54/4/2/8 exactly, deterministic over 3 runs, and
carries all four previously-parked changes. Still snapshot before one — the
artefact is cheap to keep and an hour to rebuild — but it is no longer a
one-way door.

**The 22-call loss was NINE MISSING FUNCTIONS.** Not guards, not naming, not
seeding order:

```
sub_00227F50  sub_00340CDE  sub_00340D86  sub_00343862  sub_00346743
sub_00349FAB  sub_0034AA86  sub_0034BB3A  sub_003556E0
```

CRT static-initialiser thunks, reachable **only** as data pointers, so
call-target discovery never finds them — the exact class
`find_missing_functions.py` documents. Absent from `seeded_functions.json` and
from `seed_list.json`; they had been found in some earlier era and merely
*survived* in `gen/` ever since. Every regeneration dropped them, their
indirect calls returned 0 through `RECOMP_ICALL_SAFE`, and their initialisers
silently never ran. **The fix is nine lines of data** — those addresses added
to `seed_list.json` (1,060 → 1,069). Nothing else.

### How it was found — use this method, not a source diff

Diffing 29,000 function bodies drowns the signal (1,329 differ legitimately).
Diff the **kernel-call sequences** of the two builds instead:

1. Both runs were byte-identical through call #13, then split at #14 — the good
   tree made 14 consecutive `RtlInitializeCriticalSection` (ordinal 291) calls,
   the rebuilt tree made none and went straight to entering critical sections
   that were never initialised. 14 of the 22, located in one comparison.
2. A temporary trace in the bridge printed the CS addresses: 14 at `0x005D9A30`,
   stride `0x1C` — one array.
3. Grepping `gen/` for that base found `sub_00345550`, a loop over a 36-entry
   `{pointer, flag}` table at `0x47A550`. **Byte-identical in both trees**, so
   it was never reached.
4. Its only call site is in `sub_00346743`, which does not exist in the rebuilt
   tree — and a set-difference of all defined functions gave the other eight.

Symptom to remember: **`RtlEnterCriticalSection` with no matching
`RtlInitializeCriticalSection` earlier in the log means a static initialiser
did not run**, which almost always means a function is missing rather than
mistranslated.

### Superseded: the guard loss (real, fixed, but not this)

**FIXED 2026-08-05 — it was 18 guards, not 7, and the cause was
in `manual_edits.py`, not in the guards.** Restoring them changed no measured
signal; the 22-call loss persisted until the nine functions were seeded. All 18
are the same wrapping guard,
the D3D-null "dependency type" guard, across `sub_001198B0`, `sub_001199B0`,
`sub_0011BE90`, `sub_0011D470`, `sub_0011D4C0`, `sub_00124190`, `sub_00146A60`,
`sub_00148530`, `sub_00148580`, `sub_0014B960`, `sub_0014B9B0`, `sub_0014BA00`,
`sub_0014BA50`, `sub_0014BAA0`, `sub_0014BAF0`, `sub_0014BB40`, `sub_00194290`,
`sub_001942E0`. Their recorded `wrapped` run holds a direct call spelled
`sub_00119900();`; the generator now emits `RECOMP_ABI_CALL(sub_00119900);`.
`_match_run` compares line by line, so one re-spelled line made the whole run
miss and the wrap dropped silently. Fixed in `_normalise()`, which already
existed for exactly this class of drift.

Two more bugs in the same tool fell out while confirming it:
- The 67 moved-to-shim `replace_line` records reported "no line matched" while
  being fully applied — the comment had been re-worded, so exact-text
  idempotency failed on the *prose*, not the state. Now keys on the symbol.
- Positional idempotency is unsound when guards are textually identical, and
  many are deliberately (`sub_001E8E20` carries one guard at four sites). One
  apply turned 3 pairs into 5. Replaced by `_insert_quota()`: count what is
  present, insert exactly the deficit.

`apply` now places **154/154 and is idempotent** — three consecutive runs leave
the tree byte-identical — so it no longer needs `--partial --force`, and the
"destructive on a re-run" hazard is gone.

Retracted, do not re-adopt: **the `_heap_init` guard in `sub_001A23F3` was
never lost.** It is in `recomp_0011.c` and always was. The "7" came from a
39-vs-32 marker count comparing two things that were not comparable.

Two things it is *not* (ruled out, don't re-chase):
- Not the XAPI naming (`XAPILIB_*` vs `sub_ADDRESS`) - real bug, fixed, but
  the 22-call loss persisted after fixing it.
- Not seeding order/completeness - `seed_missing_functions.py --record` /
  `--from-list` is proven byte-identical on replay.

Guards restored cleanly and changed **no** measured signal, which is how we
knew to keep looking. `sub_001E8E20` is structurally identical in both trees.

Also resolved by the nine-function fix: the rebuilt tree used to fault reading
`0xBEEF0079` on `MEM32(eax + 0x78)` with `eax = 0xBEEF0001` — the fake thread
handle from `bridge_PsCreateSystemThreadEx` (`kernel_bridge.c:241`) being
dereferenced as an object vtable. That was downstream of the uninitialised
critical sections, not a defect of its own, and it no longer occurs. Worth
remembering as a *shape*: `0xBEEF0001` in a pointer register means a thread
handle is being used as an object.

Consequences:

- A recorded number belongs to a specific `gen/`, not to a commit. Snapshot
  the build whenever you record one: `py -3 tools_data/snapshot.py -m "..."`.
- Never compare a number across a regeneration boundary without re-measuring
  the baseline in the same tree.
- **All four parked changes have LANDED** in a build measuring 54/4/2/8:
  thread-local register file, `fs:` segment lifting (2,743 `g_fs_base` sites),
  fragment promotion (empty unresolved stubs 3,272 → 2,132), consistent
  `sub_ADDRESS` naming (zero `XAPILIB_*`). None cost a kernel call.
- `fs:` lifting is genuinely inert at `g_fs_base == 0` — verified by comparing
  one site: `MEM32(0)` in the old tree, `MEM32(g_fs_base)` in the new. But it
  is the indirection **per-thread TIBs require**, and the 54 tree had none, so
  that work was impossible before this regeneration and is possible now.
- After any regeneration, check the kernel log for `RtlEnterCriticalSection`
  without a preceding `RtlInitializeCriticalSection`. That pattern means a
  static initialiser did not run — i.e. a function is missing from
  `seed_list.json`, not mistranslated.
- **There is now a regression gate**: `./smoke_test.ps1`, baseline 54/4/2/8
  recorded 2026-08-05. It gates all four signals directionally plus
  determinism, and would have caught the nine-function loss on
  `failed_icalls` alone. Run it before recording any result.

## State

Boot progress is in `src/game/tools_data/progress.json`; the full investigation
log is `src/game/DEBUGGING_NOTES.md`. Read `progress.py` output before reporting
status — never from recollection.

**Current live thread (as of the `native-threads-and-memory` branch):** the
boot hangs in an infinite recursion inside Alchemy's `igMetaObject`
registration - traced to `PsCreateSystemThreadEx` running thread start
routines *synchronously* instead of as real threads, so a thread created
during startup re-enters startup instead of running concurrently (71 nested
entries measured). Fixing this needs per-thread register state + per-thread
stack/SEH + actual OS threads for `PsCreateSystemThreadEx` - which is why
`native-threads-and-memory` exists.

**No longer blocked.** As of 2026-08-05 `gen/` reproduces, and the tree carries
the thread-local register file plus the `fs:` indirection that per-thread TIBs
need. The remaining work is entirely in hand-written code under `src/`:

1. Allocate a guest TIB per thread and set `g_fs_base` per thread — this
   activates the 2,743 lifted `fs:` sites, which are inert only because the
   base is 0.
2. Carve a per-thread guest stack out of the 64 MB map (which must stay exactly
   64 MB — the engine probes for the boundary to size RAM).
3. Make `PsCreateSystemThreadEx` spawn a real OS thread, return a handle, and
   back it with a handle table and working wait primitives.

The crash at `sub_002235D0+0xEA` (`esp = 0xFFFFFF88`, the four-function
`igMetaObject` cycle) is expected to dissolve once startup ordering is correct,
because it is a symptom of the synchronous re-entry, not a translation defect.
