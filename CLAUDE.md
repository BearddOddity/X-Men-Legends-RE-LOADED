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

## Regenerating

Expensive the first time, mechanical after. Back up `gen/` first, then:

```bash
py -3 -m tools.recomp game/default.xbe --all --split 1000     --gen-dir <scratch>/gen_new --functions seeded_functions.json --skip-binary-check
# copy in, then:
py -3 tools_data/manual_edits.py apply --partial --force
py -3 tools_data/repair_wraps.py --apply --drop-unclosed
py -3 tools_data/find_icall_esp_saves.py --fix --only sub_00209650,sub_002235D0,sub_00226250,sub_00236500
py -3 tools_data/stub_overridden.py --apply
py -3 tools_data/dedupe_seed.py --apply            # better discovery -> seed duplicates
py -3 tools_data/manual_edits.py check-braces      # must say "all functions balanced"
# `apply` placing only ~119/139 on the first pass is NORMAL: the steps above
# restore the anchors it could not find. Re-run it afterwards - it should
# then place all of them. Only investigate if the second pass is short too.
# The __SEH_epilog save-area replacement is RETIRED - rejected 3x (40/50/52
# against a 200 baseline). It compensated for esp drift that the fall-through
# fix and the g_seh_ebp sync removed at source. Do not re-add it.
py -3 tools_data/manual_edits.py extract           # re-sync the store
```

## The loop

```bash
./build_compile.bat && ./run.bat            # build, run
py -3 tools_data/triage_crash.py --grep     # where and why
py -3 tools_data/add_probe.py ...           # prove it (#5)
py -3 tools_data/add_probe.py --where ...   # ...and who called it
py -3 tools_data/triage_crash.py --where    # resolve those backtraces
py -3 tools_data/strip_probes.py --apply    # clean up
./build_compile.bat && ./run.bat            # verify twice (#7)
py -3 tools_data/progress.py record -m "…"  # (#13)
py -3 tools_data/progress.py stalled        # (#14)
```

Back up `gen/` before anything risky:
`tar --force-local -czf <scratch>/gen-$(date +%H%M%S).tar.gz src/recomp/gen`

## State

Boot progress is in `src/game/tools_data/progress.json`; the full investigation
log is `src/game/DEBUGGING_NOTES.md`. Read `progress.py` output before reporting
status — never from recollection.
