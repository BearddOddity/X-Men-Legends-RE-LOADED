# X-Men Legends → PC port

Static recompilation of the Xbox XBE into a native Windows executable.
**Definition of done: boots to the main menu.**

Never push to any remote. This repo is local-only; `origin` was removed
deliberately. `src/game/src/recomp/gen/` is gitignored — 1M+ lines mechanically
derived from a copyrighted XBE, never commit it.

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
| `find_icall_esp_saves.py` | `_icall_esp` save points across register saves; `--live` narrows to real failures |
| `find_stale_flag_tests.py` | deferred-flag miscompiles |
| `manual_edits.py` | extract/re-apply the hand edits across a regeneration |
| `audit_kernel_ordinals.py` | cross-check the bridge's two ordinal tables |

## Traps — enforced by tools, not memory

- **Shell heredocs mangle backslash escapes.** Never build a C string literal
  through one — it corrupted `\n` four separate times. Use `add_probe.py`, or
  write a `.py` file and run it.
- **`tar` on Windows reads `C:` as a remote host** — pass `--force-local`.
- **The fake TIB at VA 0 is mapped**, so null dereferences return plausible
  garbage instead of faulting. Bugs surface far from their cause.
- **`esp` is simulated and drifts.** Lifted code that reads relative to `esp`
  across a call is exposed; prefer a saved frame pointer where one exists.

## The loop

```bash
./build_compile.bat && ./run.bat            # build, run
py -3 tools_data/triage_crash.py --grep     # where and why
py -3 tools_data/add_probe.py ...           # prove it (#5)
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
