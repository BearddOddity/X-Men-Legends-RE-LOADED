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

## Where work happens: the lab is the workspace, Windows only compiles

**Do analysis in the RE Lab** (WSL + WSLg, `kali-linux`), in
`/home/oddity/projects/xbox-recomp`. Reading `gen/`, greps and cross-references,
the `tools_data/` scripts, Ghidra, and every `OddityRecomp` MCP call belong
there. The MCP server runs in the lab and derives its paths from its own
location, so the lab tree is what `progress`, `ledger`, `function`, `faithful`,
`probe` and `triage` report on. `ledger.json` and `progress.json` are written
lab-side and committed from there.

**Use Windows only to build and run**, via the `build_*.bat` / `run_*.bat`
scripts. That is where the target builds *today*: `windows.h` is included by 11
runtime files, graphics are D3D8, audio is XAudio2 and DirectSound, input is
XInput, and the crash path is a Vectored Exception Handler.

**Cross-platform Linux + Windows is an intended goal**, so read the list above
as the current port surface rather than as a permanent boundary. It is the
inventory of what a native Linux build has to replace. Until that exists the
build stays on Windows, and running the Windows binary under Wine is not a
shortcut to it: the method here rests on byte-identical deterministic runs —
that is how an empty diagnostic is told apart from a lost one — and Wine puts a
variable under exactly that signal. A real second target does not; it gives two
independent deterministic runs to compare, which is worth more than either.

**After every Windows build, copy the artifacts back to the lab.**
`src/recomp/gen/`, `stderr.txt` and `build/xmen_legends_recomp.map` are
gitignored build output, so a `git pull` never brings them and the lab's MCP
tools fail until they are copied over `\\wsl$\kali-linux\...`.
`src/game/game/default.xbe` can be a symlink to the repo's own
`game_files/default.xbe` instead of a second copy — same file, verified by
MD5.

**The linker map is the one that is easy to forget, and its absence is
silent.** Without it `triage_crash.py` cannot symbolise, so it prints an
EMPTY caller list rather than an error. That silence has already cost one
wrong conclusion: it was read as "no reliable frames exist", and a call
chain was rebuilt from the untrusted raw stack scan instead. With the map
present the same tool prints the full chain from the entry point down.

Do NOT "fix" this by repointing the MCP server at the Windows tree. That
inverts the arrangement above, and it has been proposed and rejected.

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
| `phase.py` | run a whole pipeline phase in the right ORDER — `status`, `reseed`, `rebuild`, `verify`. Add `--dry-run` to see the steps. The order is the part that keeps going wrong; this encodes it |
| `probe_struct.py` | dump MANY fields of one object in a single probe — `--base esi --offsets 0xA8:limit,0xC8:size`. Answering "what is in this object" used to cost one build-and-run per field |
| `bisect_seeds.py` | a batch of seeds regressed the boot — **which one?** Binary-searches the batch, keeping the good ones instead of abandoning all of them. Backs up `seed_list.json` and restores on every exit path. `--from-gaps` takes the FRAGMENT list straight from `find_icall_gaps.py` |
| `find_icall_gaps.py` | which failed indirect calls are MISSING FUNCTIONS? Classifies every unresolved target and offers only the confident ones (`--add`). **Seeding a real one has been the highest-yield fix on this project, three times over** |
| `triage_crash.py` | crash → function, source line, register analysis. `--grep` finds the faulting expression, `--icall` resolves failure backtraces to callers. On a HANG it symbolises the watchdog RIP and stack |
| `add_probe.py` | emit a debug probe with correct escaping |
| `strip_probes.py` | remove probes; refuses any removal that would unbalance braces |
| `add_guard.py` | emit the standard pointer-plausibility guard |
| `progress.py` | `record` a verified result, no args to show history, `stalled` for the #14 check |
| `whatis.py` | identify any Xbox VA — section, owning function, disassembly |
| `find_missing_functions.py` | functions reachable only via data pointers |
| `seed_missing_functions.py` | recompile those additively into `gen/recomp_seed.c` |
| `guard_bulk_writes.py` | guards every emitted bulk copy/fill against clobbering an address range; covers BOTH the memcpy and the loop form |
| `census_categories.py` | what the 28k lifted functions are, by category, size and whether this boot reaches them |
| `who_writes.py` | which functions write a guest address, and whether one can reach them; follows direct AND tail calls |
| `resolve_rva.py` | host RVAs in a crash stack -> function names, via `build/*.map`; `--stack FILE` does a whole trace, `--ours-only` drops system frames |
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
- **Never run `cmd /c` from the Bash tool.** MSYS path translation rewrites the
  `/c` flag into `C:/`, so `cmd /c build_compile.bat` becomes
  `cmd C:/ build_compile.bat` — an interactive shell that prints a banner, runs
  nothing, and exits 0. It looks like a successful build. Use the PowerShell
  tool (`& .\build_compile.bat`), or `cmd //c`, or Python's
  `subprocess.run(["cmd","/c",...])`, which is unaffected. Python tooling was
  never at risk; only hand-run shell commands. This burned a full
  build-and-measure cycle on 2026-08-06 and produced a phantom 1452 → 62
  "regression" that had never been measured.
- **Probes are NOT behaviourally free, and the allocator return is the worst
  place for one.** Calling `recomp_alloc_log`/`recomp_alloc_fixup` at
  `sub_003437F3`'s return made the boot die at 33 kernel calls in 2 of 3 runs;
  removing the two calls gave 5 of 5 runs at exactly 692/1452/152/11. Causation
  established by removal, mechanism unproven - most likely stdio locking or
  timing, since stdio locks have deadlocked this boot twice before. Consequence
  that matters more than the cause: **any measurement taken with heavy
  instrumentation may describe a disturbed system.** Re-measure clean before
  building on it, and prefer a counter dumped at exit over a call that prints
  inside a hot or lock-sensitive path.
- **`$env:VAR` set in the PowerShell tool does not reach the game.** `run.bat`
  launches the exe directly, and the variable was absent every time
  (`enabled=0` on all runs), so an env-gated experiment silently never ran and
  looked like a result. Verify a gate actually fired - print a line when it
  arms - before trusting anything downstream of it.
- **A stale run log is more dangerous than a missing one**, because it still
  returns confident numbers. `signals.read()` now compares `stderr.txt` against
  `seed_list.json` and `recomp_seed.c` and marks the result `STALE`;
  `bisect_core.run_once()` treats stale as unmeasurable rather than trusting
  it. Never read counters out of `stderr.txt` by hand without checking mtimes.
- **Never batch-add seeds.** "This address is a real instruction" and "seeding
  this address is safe" are different questions. A mid-function target seeds
  as a FRAGMENT that duplicates its owner's tail and inherits a half-built
  frame — sometimes the right fix, sometimes fatal, and the disassembly cannot
  tell you which. Adding 13 at once took 1452 → 56 kernel calls.
  `find_icall_gaps.py --add` now refuses fragments; use `bisect_seeds.py` to
  try a batch safely, or `--add-fragment` one at a time and measure between.
  **Copy `seed_list.json` before any seed change** — that copy is the only
  reason the 25× regression cost one cycle to undo instead of a session.
- **The TIB moved off VA 0 on 2026-08-05** — it now lives at `0x00770000` with
  `g_fs_base` pointing at it, and low memory is left ZERO. It used to be
  mapped at 0, so null dereferences returned plausible garbage instead of
  reading null, and bugs surfaced far from their cause — that overlap made
  `MEM32(4)` answer as a handler count and sent one investigation 7.6M
  iterations sideways. Cost 4 kernel calls (1456→1452); five absolute
  low-memory reads remain in `gen/` (`MEM32(0xC)` ×2, `0x54`, `0x1C`, `0x14`)
  and are the suspected reason. **Low memory reading zero is now the
  invariant** — if a null deref stops faulting, something re-mapped it.
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
py -3 tools_data/find_icall_esp_saves.py --fix --only sub_00209650,sub_002235D0,sub_00226250,sub_00236500,sub_0020E547,sub_001A23F3,sub_0020E680
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
py -3 tools_data/find_icall_gaps.py         # missing functions? highest yield
RECOMP_HANG_RIP=1 ./run.bat                 # ON A HANG: names the spinning
py -3 tools_data/triage_crash.py            #   function. Bounded by a hard
                                            #   deadline, so it always exits.
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

**Current live thread: a HANG, not a crash.** As of 2026-08-05 the boot
reaches **100 kernel calls / 11 heap allocs** with no access violation, then
spins until the 8 s watchdog fires (12.1M indirect dispatches).

**The igMetaObject recursion is RESOLVED, and the cause was not what the
recursion looked like.** `sub_001EC5E0` and `sub_001EC750` were missing from
the function database. `sub_001EC5E0` is the engine memory manager's
*allocate* method, reached only through vtable `0x003F4770` at `+0xCC`, so
**every allocation through the manager failed**: `RECOMP_ICALL_SAFE` returned
0, `sub_001F6FB0`'s failure path fell through `sub_001F6FCF` (`eax = 0`) into
`sub_001F6FD1`, which stored that 0 into `MEM32(0x5BC538)` — the class
registry's first table. A NULL registry is what made the bootstrap unable to
converge. Two lines in `seed_list.json` fixed it.

Both had sat in the baseline's four failed indirect calls, every run, for the
whole life of the 54-call plateau. **A failed indirect call to a clean `.text`
address means a function is MISSING** — check that before suspecting the
translator. The regression gate now prints exactly that on a `failed_icalls`
rise.

Retracted: a widened bootstrap gate in `sub_002226E0`
(`|| MEM32(0x5BC274) == 0`) was tried first and did break the recursion, but
once the allocator was fixed it made **no difference to any signal** and was
removed. It was a crutch for a symptom. Don't re-add it.

Also still true: the game creates **exactly one** thread
(`grep -c "PsCreateSystemThreadEx #" stderr.txt` → 1), so real threads remain
*not* the blocker, and the old synchronous-thread explanation stays retracted.

### The current spin

- Tail of the ICALL ring is kernel thunk slots 46/47 —
  `RtlLeave`/`RtlEnterCriticalSection` — so it takes and releases a lock each
  iteration.
- `esp` is now around `0x03DAB9F0`, in the **heap** (base `0x00F80000`), not
  the 8 MB stack. The game has switched to a stack it allocated itself.
- **Not** a missing function: all 195 failed indirect calls occur exactly
  **once** each against 12.1M successful dispatches — none is hot. 66 are
  absent from the function DB but most are page-aligned round numbers
  (`0x00140000`, `0x00382000`, `0x00011000`) that are data misread as
  pointers. **Do not bulk-seed that list**; classify with `whatis.py` and seed
  only what disassembles cleanly.
- One oddity worth chasing: a `RtlEnterCriticalSection` with `cs_va = 0`, i.e.
  a NULL critical section.

Next: name the spinning function, then probe its entry and exit to see whether
it returns. `recomp_where` is callable from a bridge — `recomp_where("tag", N,
a, b, c, d)` then `triage_crash.py --where tag` resolves the native stack, which
is how the lock callers above were identified.

---

## Agent skills

### Issue tracker

Local markdown under `.scratch/` — chosen for offline/cross-platform use
regardless of `gh` auth state. See `docs/agents/issue-tracker.md`.

### Wayfinder maps

Long-range planning (bigger than one session) is charted as a wayfinder map:
one map file with child ticket files, worked one decision at a time. See
`.claude/skills/wayfinder/SKILL.md`. First map: `.scratch/boot-to-menu/map.md`.

### Diagnosing walls

`.claude/skills/diagnosing-bugs/SKILL.md` — a red/minimise/hypothesise/
instrument/fix/regression-test discipline for hard bugs. Complements, doesn't
replace, `## The 15 rules` above: its ranked-falsifiable-hypothesis step
(Phase 3) is worth running before reaching for `probe.py` on a new wall.


---

## Assigned powers

<!-- 2026-09-03 assigned by skill-manager at the boot-to-menu debugging stage.
     status-page added the same day, on request, once it was written.
     Same eleven as `D:\My apps\Reverse Engineer Brain`, which is this project's
     knowledge side; the two rosters are kept identical on purpose so moving
     between the repos needs no switching step. Standing for this project. Do
     not re-derive; change only on an explicit shift. -->

**Stage:** Long-horizon debugging of the recompiled boot. The translation is
essentially finished; getting it to *execute* is the whole remaining problem.
Work is measured fixes, purpose-built instruments, and a ledger of confirmed
and refuted claims — not fresh reversing, and not feature building.

These powers are in force for this project. Use them when the work touches
their subject, whether or not they are named.

| Power | In force for |
|---|---|
| `oddity-re:ghidra-mcp-usage` | Every wall gets decompiled before it gets fixed; `tools/ghidra_naming/` automates the annotation |
| `oddity-re:re-project-loop` | `progress.py`, `ledger.json` and the project memory ARE the cross-session ledger this method describes — rules #13 and #15 |
| `oddity-re:re-lab-ops` | The Kali/WSLg lab holds the mirror, Ghidra and the Wine runs |
| `offensive-claude:reverse-engineering` | Technique catalogue for the XBE, the x86 lifter and the Alchemy engine internals |
| `mcp-server-dev:build-mcp-server` | `tools/mcp_server/` is the OddityRecomp MCP server that drives build, run, ledger and progress |
| `superpowers:test-driven-development` | `tools_data/test_*.py` is a real target, and rules #4 and #9 say build the tool and the detector |
| `superpowers:systematic-debugging` | The walls are the work; complements `.claude/skills/diagnosing-bugs` rather than replacing it |
| `superpowers:verification-before-completion` | Rules #1, #7 and #8 are this power written as house rules — build and run before and after, two runs per number, a second signal per fix |
| `plugin-dev:skill-development` | `.claude/skills/` holds six locally authored skills, including `wayfinder` and `diagnosing-bugs` |
| `caveman:caveman` | At **lite**: compressed but complete sentences. Rule #12 governs — articles and full grammar stay, because that is what dyslexia needs; only the filler goes |
| `oddity-re:status-page` | `progress.py`, `ledger.json` and `walls.py` are exactly the evidence a progress page needs; `gen_status_page.py` is this skill already written by hand for one project |

**Not assigned, deliberately:**

- `oddity-re:crackme-workflow` — nothing here is a licence check or a protected
  binary; `re-project-loop` covers the long-horizon method that does apply.
- `oddity-re:anti-debug-reference` — the XBE is neither packed nor obfuscated.
- `android-reverse-engineering` — reads as adjacent and is not. This is an Xbox
  x86 XBE; there is no APK, DEX or JVM bytecode anywhere.
- `caveman:investigate-first` — real overlap with `superpowers:systematic-debugging`
  and with `.claude/skills/diagnosing-bugs`. Two investigation methods is one
  too many; three is noise.
- `caveman:surgical-patch`, `safe-refactor`, `migration`, `lean-build` — rule #6
  ("fix only as wide as the evidence") and rule #10 ("don't tidy what you can't
  prove is inert") already carry the narrow-fix discipline, and carry it with
  this project's own incidents attached.
- `caveman:cavecrew` and `superpowers:dispatching-parallel-agents` — subagents
  start cold and re-derive context the ledger already holds. Use only when
  explicitly asked.
- The offensive kill chain (`initial-access`, `privesc-*`, `edr-evasion`,
  `red-team-ops` and the rest) — there is no engagement here. Only the analysis
  powers from that plugin apply.
- `oddity-re:log-analysis`, `blue-team-defense`, `csoc-automation`,
  `grc-compliance`, `ot-ics-security` — installed for other work; this project
  runs no defensive operations.
