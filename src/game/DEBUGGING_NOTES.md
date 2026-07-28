# X-Men Legends recompilation - debugging notes

Status as of this session: builds and runs, boots into real game/CRT code,
runs 24+ kernel calls including genuine multi-lock critical-section usage
(no more recursion), then hits an access violation that traces into C++
exception-handling / `operator new` failure-path infrastructure - a
different, harder subsystem than anything fixed so far. Six game-specific
bugs fixed via manual overrides/patches; four bugs fixed **at the
recompiler tool level**, benefiting the entire 26,505-function codebase,
not just this game.

Entry point: `0x001A1C97`. XDK 5849, Title ID `0x4156001E`.

## Fixed (game-specific): thread trampoline never detected (`sub_0019F196`)

The Xbox CRT's `_threadstartex`-equivalent trampoline is passed to
`PsCreateSystemThreadEx` as a raw data value (`StartRoutine`), never called
directly via `CALL`/`JMP` anywhere in the binary. The disassembler's
function-detection only tracks direct call targets, so this function was
never recognized and `recomp_lookup()` returned NULL for it - the very
first thread spawn silently did nothing.

Fix: manual override in `src/recomp_manual.c` (`sub_0019F196`) that skips
the CRT per-thread setup (not needed by our runtime) and calls
`StartContext1(StartContext2)` directly, matching
`void ThreadRoutine(PVOID StartContext1, PVOID StartContext2)`.

## Fixed (game-specific): game's real entry point also undetected (`sub_001A1C23`)

Same root cause, one level deeper: `StartContext1` passed to the first
`PsCreateSystemThreadEx` call is `0x001A1C23`, the XAPI process-startup
dispatcher (`_XapiInitProcess`-equivalent). It's *also* only reachable as a
data value, so it was *also* undetected - meaning after fixing the
trampoline above, it still looked up NULL and did nothing.

Fix: another manual override in `src/recomp_manual.c` (`sub_001A1C23`)
that hand-replays the original call sequence, since everything *it* calls
(`sub_001A3639`, `sub_001A23F3`, `sub_001A35AC`, `sub_001A3554`,
`sub_00011E40`, `sub_001A237D`) *is* a properly-detected function. Skips
one small inline TLS-fixup block gated on a per-thread debug pointer that's
null in our environment anyway.

## Fixed (game-specific): self-relaunch boot flag (`HalReturnToFirmware`)

`sub_0019EB69` reads Xbox VA `0x0044B0D8`; if zero, it self-relaunches via
`HalReturnToFirmware(2)` (ordinal 49, unbridged in `kernel_bridge.c`). The
XBE ships that address as zero with **no writer anywhere in the game's
code** - real hardware must set it via some kernel/firmware mechanism we
don't replicate.

Fix: `src/main.c`, right after `xbox_MemoryLayoutInit()`, writes `1` to
that address to skip the self-relaunch we can't otherwise satisfy.

## Fixed (recompiler tool): heap corruption from missing epilogue + stale hardcoded addresses

This was the big one. Root cause chain, found via a runtime guard-page
watchpoint (see technique below):

- The CRT heap allocator (`sub_001A0B0C`, classic NT `RtlAllocateHeap`
  pattern) crashed dereferencing garbage from its `FreeLists[128]` array.
- Traced to a 3-byte pointer mismatch: `RtlCreateHeap`-equivalent
  (`sub_001A06E8`) correctly initializes `FreeLists[]` relative to base
  `0x00F80060`, but the pointer stored for later use was `0x00F80063`.
- Root cause: `sub_001A06E8`'s **success epilogue was a mid-function
  fallthrough block only reached by a conditional jump (`jne`), never a
  direct `CALL`/`JMP` target** - so the disassembler never found it as a
  function, and the recompiler silently stubbed it empty
  (`void sub_001A0A97(void) {}`). On the success path, no header
  finalization ran, `eax` (the return value) was left holding garbage from
  the previous call, and the 28 bytes the caller pushed were never popped.

**This is a systemic bug, not a one-off** - `tools/recomp/analyze_unresolved.py`
(already existed, just never run) showed **57% of all 6,693 unresolved call
stubs are this exact "continuation past a mis-detected function boundary"
pattern**, plus another 20% are plain undetected functions in gaps between
known ones. Fixed at the source, in three places:

1. **`tools/recomp/analyze_unresolved.py`** had a hardcoded `SECTIONS` table
   with sizes/names (`XMV`, `XONLINE`, `XNET`, a 2,863,616-byte `.text`)
   left over from a different game (the generated-file headers still say
   "Burnout 3: Takedown" - this whole toolkit was originally built/tested
   against that game). X-Men Legends' real `.text` is 3,448,212 bytes with
   completely different section names, so real `.text` addresses were
   silently misclassified as fake library sections and excluded from
   candidate seeding. **Fixed**: sections now load dynamically from
   `xbe_parser`'s JSON output (`--xbe-json <path>` positional arg), never
   hardcoded.
2. **`tools/recomp/lifter.py`**: conditional-jump tail-calls
   (`_lift_jcc`, both branches, and `_emit_cond_goto` for the CMP+Jcc
   fusion pattern) built `if (cond) { name(); return; }` without the
   `g_seh_ebp = ebp;` frame-pointer propagation that the *unconditional*
   jmp tail-call path (`_lift_jmp`) already correctly included. This is
   exactly what caused the heap descriptor pointer corruption above.
   **Fixed**: all three sites now emit `g_seh_ebp = ebp;` before the call,
   matching `_lift_jmp`.
3. **`tools/recomp/translator.py`**: the `used_regs.add("ebp")` heuristic
   that decides whether a function declares a local `ebp` variable had two
   bugs: (a) it only checked unconditional `jmp` for tail-calls, not `jcc`
   (so after fixing #2, several functions tried to read an undeclared
   `ebp` - compile error `C2065`), and (b) it checked for calls to
   `__SEH_prolog`/`__SEH_epilog` via a **second hardcoded address pair**
   (`0x00244784`/`0x002447BF` - again leftover from a different game)
   instead of using the already-correct auto-detected addresses
   (`self.lifter.SEH_PROLOG`/`SEH_EPILOG`, which for X-Men Legends are
   `0x3432A8`/`0x3432E3`). **Fixed**: tail-call detection now covers
   `jcc`/`jecxz`/`jcxz` too, and the SEH-call check uses the real
   per-binary addresses.

### The workflow that applied this fix codebase-wide

1. Extract addresses from `recomp_stubs_unresolved.c` into a plain list,
   run `py -3 -m tools.recomp.analyze_unresolved <xbe_analysis.json>`.
2. Build a seed list from the `gap`/`continuation` entries in
   `tools/recomp/output/addable_functions.json` (plain JSON array of
   integer addresses).
3. Re-run `py -3 -m tools.disasm <xbe> --text-only --seed-functions
   <seed_list.json>` - the disassembler already supports this flag, it
   just needed candidates fed in.
4. Re-run `func_id` and `recomp --all --split 1000`.
5. Repeat 1-4 once more (each pass reclassifies what's *left* as
   unresolved, catching more gap/continuation candidates that were hidden
   behind the previous round's mis-detected boundaries).

Result: unresolved stubs dropped from **6,693 → 3,272** (51%) across two
seeding passes, before the lifter/translator fixes. `sub_001A0A97` (this
bug's actual culprit) went from an empty stub to a real, correct
translation once seeded - the hand-written manual override that used to
live in `recomp_manual.c` for it has been deleted; the real generated code
replaces it now.

### Reproducing the guard-page watchpoint technique (for future crashes)

If you need to find who writes/reads a specific Xbox VA at runtime:

1. Pick the hook point - either a fixed address (arm the guard right after
   `xbox_MemoryLayoutInit()` in `main.c`) or a dynamically-allocated block
   (arm it in the relevant `kernel_bridge.c` bridge function right after
   the allocation succeeds). `VirtualProtect(native_ptr, 4096,
   PAGE_READWRITE | PAGE_GUARD, &old)`.
2. In `veh_handler` (`main.c`), catch `STATUS_GUARD_PAGE_VIOLATION`
   (`0x80000001`), log `RIP - GetModuleHandleA(NULL)` as an RVA (resolve
   against `build/*.map`'s "Publics by Value" section, matched against
   `0x140000000 + RVA` since that's the map's preferred load base, to get
   the `sub_XXXXXXXX` symbol) and the faulting Xbox VA, then set
   `EFlags |= 0x100` (trap flag) to single-step past the instruction.
3. Catch `EXCEPTION_SINGLE_STEP` right after: re-apply
   `PAGE_READWRITE | PAGE_GUARD` to re-arm (needed if you want every touch,
   not just the first - `PAGE_GUARD` only fires once per access).
4. **Add `/MAP` to the linker options** (`CMakeLists.txt`,
   `target_link_options`) beforehand so `build/*.map` exists to resolve
   RVAs against.

This is real-time, ground-truth instrumentation - much faster than
guessing from static disassembly once you're deep into CRT internals.
Remove the watchpoint code after use; it's meant to be temporary (both the
`kernel_bridge.c` arm site and the `main.c` catch/re-arm logic are
currently reverted, not in the tree).

## Fixed (game-specific): CRT lazy lock-table bootstrap recursion

Native `STATUS_STACK_OVERFLOW` starting at kernel call #19: `ordinal 277`
(`RtlEnterCriticalSection`)/`ordinal 294` (`RtlLeaveCriticalSection`)
alternated in a tight pattern, `g_esp` shrinking by `0x3C` per pair, for
180+ pairs. Found the exact recursive pair via a `CaptureStackBackTrace`
diagnostic added to `bridge_RtlEnterCriticalSection` (triggered once call
count hit a threshold, resolved the captured RVAs against `build/*.map` -
see technique below): `sub_00345674` <-> `sub_003455D4`, mutually
recursive forever.

Root cause: `sub_003455D4` (creates a numbered CRT lock on first use, e.g.
for the heap) **always acquires "lock #10" first** to protect its own
shared lock table - including when lock #10 itself is what's being
created. Real CRT startup (`_mtinit`, before `main()`/`WinMain()`) eagerly
pre-initializes lock #10 so this self-reference never actually recurses on
real hardware. We skip full CRT startup (see the `sub_001A1C23` override
above), so the very first lazy lock creation recursed forever.

Fix: `src/main.c`, right after the `0x0044B0D8` boot-flag patch, allocates
a `CRITICAL_SECTION`-sized block via `xbox_HeapAlloc`, calls
`InitializeCriticalSection` on it, and writes its Xbox VA into the lock
table slot for #10 (`0x0047A550 + 10*8 = 0x0047A5A0`) directly - mimicking
what `_mtinit` would have done.

## Fixed (recompiler tool): breakpoint handler was corrupting execution on every INT3

Separately, `__debugbreak()` (translated from x86 `int3`) was crashing
with `EXCEPTION_BREAKPOINT` because nothing caught it. First fix attempt
added a VEH handler that did `Rip += 1` to skip past the trap - this was
**wrong** and actively harmful: standard `INT3` semantics mean the
exception's saved `RIP` *already* points to the instruction after the
1-byte `int3`, so `Rip += 1` skipped an *extra* byte into the middle of
the following instruction, corrupting the execution stream. This produced
a *different* bug that looked identical to the lock recursion (same RVA
logged 12,000+ times) but was actually garbage-instruction execution
looping back into the same corrupted decode point.

Fix: `src/main.c`'s `veh_handler` now catches `EXCEPTION_BREAKPOINT`, logs
where it happened, and returns `EXCEPTION_CONTINUE_EXECUTION` **without**
adjusting `Rip`. This correctly treats debug breaks as no-ops (matching
retail Xbox hardware with no debugger attached, modulo not actually
resetting the console).

## Fixed (recompiler tool): ICALL failures were completely silent

`recomp_icall_fail_log()` existed (declared in `recomp_types.h`,
implemented in `recomp_manual.c`) but **neither `RECOMP_ICALL` nor
`RECOMP_ICALL_SAFE` ever called it** - every failed indirect call this
entire session failed with zero log output, which is why several bugs
above needed guard-page watchpoints or manual stack-trace diagnostics
instead of just reading a log. Fixed in both `templates/runtime/recomp_types.h`
(for future games) and this game's copy (`src/recomp/recomp_types.h`):
both macros now call `recomp_icall_fail_log(_va)` on the fallback path.
To avoid flooding the log on a hot vtable dispatch that fails every frame,
`recomp_icall_fail_log()` now dedupes by address (logs each unique failing
VA once, caps at 512 unique addresses).

## Fixed (game-specific): third instance of the undetected-function pattern (`RtlInitializeCriticalSection` fallback)

While fixing the lock recursion above, found `sub_0034B1A4`
(`InitializeCriticalSectionAndSpinCount`-equivalent) delay-loads a real
implementation into a function-pointer slot (`Xbox VA 0x005D9CD8 = 0x0034B194`)
and calls through it indirectly. `sub_0034B194` (just calls kernel ordinal
291, `RtlInitializeCriticalSection`, and returns success) was invisible to
the disassembler - same "only reachable via a data/pointer reference, not
a direct CALL/JMP target" pattern as the thread trampoline and entry
dispatcher. The indirect call silently returned failure (0), cascading
into a lock-creation-failure error/report path.

Fix: added `0x0034B194` to `tools_data/seed_functions.json` and re-ran the
seed → disasm → func_id → recomp pipeline - now a real, correctly
generated function rather than a manual override.

### Reproducing the native-stack-trace-on-a-counter technique (for recursion bugs)

If a crash looks like unbounded recursion (steadily shrinking/growing
`g_esp`, or a `STATUS_STACK_OVERFLOW` exit code `0xC00000FD`):

1. Pick a bridge function on the suspected hot path (e.g.
   `bridge_RtlEnterCriticalSection` in `kernel_bridge.c`).
2. Add a `static int call_count` counter; once it crosses a threshold
   (enough to be well into the recursion but comfortably before the stack
   actually exhausts - 30-60 worked here), call
   `CaptureStackBackTrace(0, N, frames, NULL)` and print each frame as
   `(uintptr_t)frames[i] - (uintptr_t)GetModuleHandleA(NULL)` (an RVA).
3. Resolve each RVA against `build/*.map`'s "Publics by Value" (same
   lookup as the guard-page technique) - the native C call stack directly
   mirrors the recompiled game code's call chain, since every
   `sub_XXXXXXXX()` is a real C function call. A short repeating pair/
   triple of symbols in the frame list is the recursive cycle.

Remove the diagnostic after use (not currently in the tree).

## Open: access violation in C++ exception-handling / `operator new` failure path

After all fixes above, the game runs 24+ kernel calls with genuine
multi-lock critical-section usage (confirms the lock bootstrap fix is
correct - no more recursion), then crashes:

```
[CRASH] Access violation at fault addr Xbox VA 0xFFFFFFF5 (read)
  ecx=0x00000000
```

Traced to `sub_001A016A` (called from `sub_00345ACC`, which also calls the
heap descriptor getter `sub_0019ED75` and `_lock` `sub_00345674` - looks
like a generic allocation-failure/error path, possibly `operator new`'s
failure handling or a `std::bad_alloc`-style throw):

```asm
mov ecx, [esp+0xc]
mov al, [ecx - 0xb]   ; <-- crashes: ecx is NULL, reads Xbox VA -11
test al, 1
jne ...
```

`ecx` (some object pointer - the offset pattern, reading a flag byte at
`-0xB` from the pointer, is consistent with MSVC's C++ exception-object
or RTTI header layout) is null when this runs. This is a **different,
harder problem than everything fixed so far** - it's not another
undetected-function case, it's the actual C++ exception-handling ABI
(frame-based SEH unwinding, scope tables, possibly `operator new`'s
new-handler chain) that our runtime doesn't implement at all. Full support
would be a substantial undertaking; a narrower fix (make `sub_001A016A`
handle `ecx == 0` gracefully, or find why `ecx` is null one level up in
`sub_00345ACC`) is more tractable but not yet investigated.

Not yet done: trace `sub_00345ACC` far enough to find why it passes a null
object pointer forward - could be as simple as another lazily-initialized
global (matching the pattern of every fix in this document so far) rather
than genuinely needing full C++ EH support.

## Environment notes for future sessions

- MSVC via VS "18" BuildTools (`C:\Program Files (x86)\Microsoft Visual
  Studio\18\BuildTools`), bundled CMake and Ninja under
  `Common7\IDE\CommonExtensions\Microsoft\CMake\`.
- Build/run via `.bat` files in `src/game/` (`build_configure.bat`,
  `build_compile.bat`, `run.bat`) invoked through `cmd //c` (double slash -
  git-bash mangles a single `/c` into a Windows path otherwise) with
  `< /dev/null` to avoid hanging on stdin, always via the Bash tool's
  `run_in_background: true` for anything that takes more than a few
  seconds.
- The game data folder (`src/game/game/`) is a Windows directory junction
  to the extracted XISO folder, not a copy - avoids duplicating ~2.4GB.
- `origin` remote was intentionally removed from this repo per user
  request - nothing should ever be pushed to `sp00nznet/xboxrecomp`. If
  you need version control, ask the user for a remote to point at first.
