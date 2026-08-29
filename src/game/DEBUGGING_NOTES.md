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

## Fixed (game-specific): null-pointer query crash (`sub_001A016A`)

Access violation reading Xbox VA `0xFFFFFFF5` (`ecx - 0xB` with `ecx == 0`).
Traced the argument back to `sub_00345ACC`'s own incoming parameter
(passed down from its caller, `sub_00340D06`) rather than anything
computed locally - `sub_001A016A` reads a flag byte at `[ptr-0xB]` to
answer what looks like a "does state X exist yet" query (possibly C++
exception/per-thread state - the negative-offset flag-byte pattern is
consistent with that), and the real x86 code has no null check because
real CRT startup guarantees the state exists by the time anything can
reach this call. We skip full CRT startup, so that guarantee doesn't
hold, and the very first query crashes instead of getting a normal "not
set" answer.

Fix: **not** full C++ exception-handling support (that would be a much
bigger undertaking) - just a null guard. `sub_001A016A`'s generated body
is disabled via `#if 0` in `src/recomp/gen/recomp_0011.c` (**not tracked
by git - reapply this disable if the pipeline ever regenerates that
file**), replaced by a manual implementation in `recomp_manual.c` that
checks `ecx == 0` first and returns the same "not set" answer the real
code's own failure path already provides (via the untouched, still-real
`sub_001A017A`/`sub_001A0196`) - it just skips the crash to get there.
This is a **direct-call replacement**, not a `recomp_lookup_manual`
override, since `sub_00345ACC` calls it with a plain C function call.

Result: 24 → 28 kernel calls before the next crash.

## Worked around (game-specific): heap manager large-block allocation crash

Crash after the `sub_001A016A` fix, in `sub_0019F765` (called from
`sub_001A02B7`, called from `sub_001A0B0C` - our own heap allocator):

```
[CRASH] Access violation, fault addr Xbox VA 0xD8042464 (read)
  eax=0x00002000 (8192 - an allocation size)  ecx=0  esi=0xD804245C (garbage)
```

Traced via targeted `fprintf` diagnostics added directly to the generated
code (not the guard-page technique - simpler for this, since the values
needed were already local variables, not memory writes to watch):

- `sub_001A02B7` walks a **64-entry bucket array at `heap_struct+0x60`**
  (matches Windows NT heap manager's well-known `Segments[HEAP_MAXIMUM_SEGMENTS]`,
  `HEAP_MAXIMUM_SEGMENTS = 64`), indexed by size class, looking for a
  bucket covering the requested allocation size (`eax=0x2000` - the first
  *large* allocation attempted; previous ones all stayed in the
  small-block `FreeLists[]` path already fixed earlier this session).
- The search is **hierarchical/recursive**: the first lookup (in the
  main heap struct at `0x00F81000`) succeeds and returns a pointer
  (`0x00F81630` - confirmed to be just offset `0x630` into the *same*
  heap block, not a separate allocation - close to a `0x60C` offset
  constant seen deep in `sub_001A06E8`'s body that was never fully
  traced). `sub_001A02B7` is then called *again* using that pointer as
  its own "structure" argument, searching *its* bucket array - and slot 8
  of that nested table holds `0x0003003B`, neither zero nor a valid
  heap pointer.
- This is a materially deeper layer of NT heap-manager segment/UCR
  (uncommitted-range) bookkeeping than the `FreeLists[]`/epilogue fixes
  above, and appears to involve engine-specific or otherwise
  undocumented structure layout that wasn't fully traced (see the
  `0x60C` lead above for a next-session starting point).

**Workaround applied** (not a root-cause fix): `sub_0019F765`'s generated
body is disabled via `#if 0` in `src/recomp/gen/recomp_0011.c` (**not
tracked by git - reapply if that file ever regenerates**), replaced by a
direct-call-replacement in `recomp_manual.c` that adds a
plausible-Xbox-VA bounds check (`0x00010000` to `0x74000000`, our mapped
mirror ceiling) before each list-node dereference, treating an
out-of-range pointer as "end of list" instead of crashing. This may
cause the large-block allocator to fail/fall back where it should
succeed, rather than fixing the underlying segment-table gap - flagged
in the code for revisit if that turns out to matter.

Result: 28 → 35+ kernel calls before the next issue (below) - confirmed
via direct testing, not yet baselined (see next section).

## Open: silent infinite loop (no crash, no kernel calls) after ~35 kernel calls

After the workaround above, the process no longer crashes - instead it
hangs, burning CPU (confirmed via `Get-Process`: ~1 second of CPU time
per second of wall time, i.e. a genuine busy-spin, not an idle wait) with
**zero new log output** after kernel call #35. Killed manually via
`Stop-Process -Force` after ~70 seconds with no progress.

This is a different diagnostic problem than everything above: the
stack-trace-on-a-call-counter technique (used for the lock-bootstrap
recursion) only works because that bug hit a *kernel bridge function*
repeatedly, giving a natural hook point to count from. A loop entirely
within recompiled game/CRT code that never calls into the kernel bridge
has no such hook.

**Next step, not yet done**: a timer/watchdog-based stack sample instead
- e.g. a second thread (or a Windows timer callback) that, after a
fixed delay (say 5-10 seconds) with no forward progress, suspends the
main thread via `SuspendThread`, captures its context/stack via
`GetThreadContext`/`RtlCaptureStackBackTrace` from *outside* the running
thread, and resolves the addresses against `build/*.map` the same way as
every other technique in this document. This is the standard way to
diagnose a hang with no natural instrumentation point, and hasn't been
built yet this session.

## Environment notes for future sessions

- MSVC via VS "18" BuildTools (`C:\Program Files (x86)\Microsoft Visual
  Studio\18\BuildTools`), bundled CMake and Ninja under
  `Common7\IDE\CommonExtensions\Microsoft\CMake\`.
- Build/run via `.bat` files in `src/game/` (`build_configure.bat`,
  `build_compile.bat`, `run.bat`), invoked **directly** from git-bash as
  `./build_compile.bat` / `./run.bat` (not `cmd //c "build_compile.bat"` -
  that form started failing with "not recognized as an internal or
  external command" partway through this session for unclear reasons;
  direct invocation works reliably), with `< /dev/null` to avoid hanging
  on stdin, always via the Bash tool's `run_in_background: true` for
  anything that takes more than a few seconds.
- The game data folder (`src/game/game/`) is a Windows directory junction
  to the extracted XISO folder, not a copy - avoids duplicating ~2.4GB.
- `origin` remote was intentionally removed from this repo per user
  request - nothing should ever be pushed to `sp00nznet/xboxrecomp`. If
  you need version control, ask the user for a remote to point at first.

## Root cause of the "silent infinite loop" hang: two bugs, not one

The hang described above (zero log output after kernel call #35, busy-spin)
turned out to be two separate bugs, found by adding a watchdog thread (see
`main.c`, `watchdog_arm()` - spawns a thread that sleeps 8s then dumps the
ICALL trace ring buffer and terminates the process; safe because it never
touches the suspended main thread, unlike the `SuspendThread`+
`GetThreadContext` approach mentioned above, which caused a secondary
crash once and was abandoned).

**Bug 1 - `NtFreeVirtualMemory` incompatible with the bump allocator.**
`xbox_NtFreeVirtualMemory` (`src/kernel/kernel_memory.c`) called the real
Win32 `VirtualFree()` on a pointer that came from `xbox_HeapAlloc`'s bump
allocator (a slice of one big pre-mapped region), not from an individual
`VirtualAlloc`. `VirtualFree()` is guaranteed to fail in that situation,
so `NtFreeVirtualMemory` returned `STATUS_UNSUCCESSFUL` where real
hardware would return `STATUS_SUCCESS`, and the game's error-handling
path for that failure called through a never-initialized null function
pointer in a tight loop (487M+ ICALL dispatches in 8 seconds, all
targeting VA 0). Fixed in `bridge_NtFreeVirtualMemory`
(`src/kernel/kernel_bridge.c`) by making it a no-op, matching
`xbox_HeapFree`'s existing "No-op for bump allocator" design - do **not**
"fix" `xbox_NtFreeVirtualMemory` itself, the bridge is the active path.

**Bug 2 - `sub_001F8890` (string/name pool allocator) walks a linked list
through a null "this" pointer.** Diagnosed via a new `recomp_icall_fail_log`
probe: a counter that fires a `CaptureStackBackTrace` dump on the *Nth
total* ICALL failure regardless of address (bypasses the existing
per-address dedup, which would otherwise silence a spin loop re-failing on
an already-seen address). RVAs resolved against `build/*.map` pointed at
`sub_001F8890`; a temporary `fprintf` inserted directly into the generated
function (`src/recomp/gen/recomp_0014.c`, reverted after use - this file
is gitignored/regenerated) confirmed `ecx` ("this") was exactly 0 on every
call. Root cause: whatever constructs this pool is D3D-related, and
**D3D isn't implemented at all** - see the "D3D question" section below.
sub_001F8890 was overridden in `recomp_manual.c` (generated version
disabled via `#if 0` in `recomp_0014.c`, same "direct-call replacement"
pattern as `sub_0019F765`) to just hand back a fresh buffer from the bump
allocator - the caller (`recomp_0015.c`, `sub_0020DA95`) doesn't null-check
the return value and immediately `memcpy`s a string into it, so returning
0 would just move the crash one instruction later.

> **STALE as of 2026-08-08 (ledger #96/#97). Two claims above are now wrong.**
>
> 1. **`ecx` is no longer 0.** Re-measured inside the stub: `ecx` is
>    `0x01092B58`, a real constant heap pointer, on **all 33 calls**. The
>    "this is null because the pool is D3D-related" reasoning no longer
>    holds. A refutation expires when the code under it changes.
> 2. **"Just hand back a buffer" was not enough.** The function's signature
>    is `(length, out-ptr, out-ptr)` and the stub ignored **both**
>    out-parameters. `sub_0020DA95` zeroes those slots, passes them by
>    address, then forwards them to `sub_0020A360` - the `{owner, refcount}`
>    block-header initialiser - which therefore wrote its header to guest
>    VA `0x00`/`0x04` 33 times. Invisible until the page-zero census build
>    (`RECOMP_TRAP_PAGE_ZERO`) caught it, because guest page 0 is mapped.
>
> The stub now writes both out-parameters and allocates `len + 8` so a real
> 8-byte header sits before the buffer. Result: `reached` 86 → 101,
> **15 gained / 0 lost**, and the crash stops being a null dereference.
> See the corrected comment at `sub_001F8890` in `recomp_manual.c`.

With both fixed, boot reaches kernel call #35 plus several real heap
allocations (previously hung indefinitely after call #31) before hitting
a new, later crash - see below.

## The D3D question (decision point, resolved)

Running `py -3 -m tools.recomp.analyze_unresolved src/game/game/xmen_analysis.json`
showed **103 functions in XDK library sections** (D3D: 60 fns/83KB, DSOUND:
28 fns/48KB, XGRPH, XPP, D3DX, WMADEC) that were never disassembled - only
`.text` (game code) was. The game's boot sequence tries to create a D3D
device; since none of that code exists in the recompiled build, every call
into it silently no-ops, leaving D3D-owned objects (like the pool behind
Bug 2 above) permanently null.

Two options were considered: (a) extend the disassembly pipeline to cover
the library sections too (the toolkit already supports this via
`tools.disasm --extra-sections`), or (b) skip recompiling Microsoft's own
D3D8 code and instead hand-write native shims for the specific D3D8 API
entry points the game calls during boot. **User chose (b)** - reasoning:
D3D8 on Xbox pokes real GPU hardware registers directly, so even a
faithful recompile of the library code would just hit a second wall
needing NV2A hardware emulation (which doesn't exist in this toolkit).
A native shim is much less work and doubles as the seed for the
modern-graphics replacement planned after the game boots successfully.

**Implication for future fixes**: when a crash traces back to an
uninitialized/null object that turns out to be D3D-owned (check whether
the constructing code is unresolved and its target VA falls at or above
`0x0035ADA0`, the D3D section's `virtual_addr` per `xmen_analysis.json`),
the fix is a native override in `recomp_manual.c` that fakes a plausible
result (like `sub_001F8890` above), not a deeper trace into D3D internals.

## sub_001F84D0 (fixed)

Root cause confirmed by temporarily printing both globals at the read site:
`MEM32(0x5BC538)` and `MEM32(0x5BC53C)` are both always 0 - same D3D-null
pattern as `sub_001F8890`. Traced the constructor (`sub_001F6FD1`,
`recomp_0014.c`): both only get a real value if an ICALL through a device
object at `0x5BC508` succeeds; since D3D isn't recompiled it fails
silently and they stay 0 forever.

Fixed by copying the generated function into `recomp_manual.c` near-
verbatim (register names prefixed `g_`, since this file isn't compiled
with `RECOMP_GENERATED_CODE` register aliasing - watch for macros like
`RECOMP_ICALL_SAFE` that assume the alias internally; wrap the call site
in a local `#define eax g_eax` / `#undef eax` if you hit an "undeclared
identifier" for a bare register name) plus one added guard: if the
selected table pointer is 0, take the function's own existing "nothing to
render" early-out (tail-call to `sub_001F853F`) instead of walking a null
table. Chose to preserve the original stack-cleanup code exactly rather
than reimplement from scratch - this function's `esp` bookkeeping is
entangled with `sub_001F853F`/`sub_001F8545` (no explicit cleanup before
the tail call; relies on whatever eventually calls into this three-
function unit), and re-deriving that by hand is easy to get subtly wrong.

## Systemic fix attempted and reverted: dummy object for all failed ICALLs

After hitting a **third** D3D-null crash (`sub_002235D0`, 600+ bytes, many
ICALLs through what looks like a caller-supplied resource-descriptor
callback table - much bigger/riskier to hand-patch than the previous two),
tried a systemic fix instead of another individual guard: changed the
`RECOMP_ICALL`/`RECOMP_ICALL_SAFE` failure fallback (both copies of
`recomp_types.h`) to return a pointer to a shared, lazily-allocated,
zeroed 4KB scratch buffer (`recomp_icall_dummy_object()` in
`recomp_manual.c`) instead of 0. Idea: downstream code expecting "got an
object back" would take its normal path with inert dummy data instead of
null-crashing, potentially resolving many D3D-dependent crashes at once
rather than one at a time.

**Result: regression, reverted.** `smoke_test.ps1` dropped from 35 to 31
kernel calls, with a *new, earlier* crash (`eax=0xFF000000`, fault at
`0xFF000078` - looks like a bad shift/enum-default computation, consistent
with code that reads a capability/format field from what it assumes is a
real, correctly-shaped D3D object and gets zeroed dummy data instead).
Confirms the risk flagged when this approach was chosen: a zeroed dummy
object isn't validly-shaped for every D3D interface, and "looks like an
object" can be worse than "obviously null" when the consumer doesn't
null-check but does trust specific field values.

**Verdict**: stick with per-crash guards (`sub_001F8890`, `sub_001F84D0`
above) unless a future attempt at the dummy-object idea makes the buffer
smarter (e.g. pre-fill specific known offsets with sane defaults for the
specific interfaces actually consulted, rather than one generic zeroed
blob for every failure site). Reverting is a pure revert of
`recomp_types.h`/`templates/runtime/recomp_types.h` to their prior
committed content plus deleting `recomp_icall_dummy_object()` from
`recomp_manual.c` - nothing else depends on it.

## sub_002235D0 (fixed) - NOT the D3D-null pattern, a different bug

This one turned out to be unrelated to the D3D-null-global pattern above,
despite living in the same font/resource-registration area. At
`loc_0022360D` the function checks whether a caller-supplied slot
(`MEM32(esp+0x120)`, called `ebx` below) is already non-zero ("already
cached, reuse it") before falling through to a fresh-allocate path.

Dumped the raw bytes at that exact Xbox VA from `default.xbe` directly
(`.rdata` file offset = `raw_addr + (va - virtual_addr)` from
`xmen_analysis.json`'s section table) and found it's literally the ASCII
string **`"igStringObj\0..."`** - a type-name string constant, not a
pointer-sized cache slot at all. Its first 4 bytes (`0x74536769`) get
read as if they were a real cached object pointer, pass the `!= 0` check,
and the function walks it as an object several calls later - crash.

Root cause not fully understood (likely a caller argument-offset
mismatch upstream - which of ~11 pushed constants lands at `esp+0x120`
for this particular call site doesn't match my by-hand stack accounting,
and re-deriving it exactly for an 11-argument call into a 200-instruction
function was higher-risk than just guarding the symptom). Fixed with a
plausible-VA range check on the "already cached" branch condition itself
(same range convention as `sub_0019F765`'s list-walk guard: valid Xbox
heap/data VAs are `0x00010000` to under `0x02000000`) - if the "cached"
value doesn't look like a real pointer, treat the slot as empty and take
the normal fresh-allocate path instead.

**Applied directly in `recomp_0016.c` (gitignored, not `recomp_manual.c`)
- unlike the previous two fixes, this one is NOT preserved across
regeneration.** At 610 bytes/200 insns this function was large enough
that hand-transcribing it into `recomp_manual.c` (register-name prefixing,
re-deriving every stack offset by hand) carried real risk of introducing
a *new* bug while chasing this one, so the smaller, lower-risk patch was
applied in place instead. **If the disasm/recomp pipeline is ever rerun
for this game, this fix is lost and must be reapplied**: in
`sub_002235D0`, change the `loc_0022360D` branch from `if
(MEM32(ebx) != 0) goto loc_00223698;` to also require `MEM32(ebx) >=
0x00010000u && MEM32(ebx) < 0x02000000u`.

Verified via `smoke_test.ps1`: 35 -> 39 kernel calls, no regression.

## Current crash (not yet fixed)

## sub_002085CA, sub_00200B18, sub_001186A0 (fixed) - three more instances

All three trace back to the same D3D-null allocator family, each a
different failure shape:

- **`sub_002085CA`** (append-to-dynamic-array): writes into a buffer
  (`MEM32(esi+8)`) without checking it's non-null. Growth
  (`sub_00202B60`) routes through the D3D-null allocator so the buffer
  never gets allocated. Fixed in `recomp_manual.c` (only one call site,
  simple contract - see the source comment there for the exact reasoning
  matching `sub_001F84D0`'s "kept near-verbatim" approach).

- **`sub_00200B18`** (`recomp_0015.c`, gitignored): a value fetched via a
  D3D/font-dependent ICALL (through a different object's vtable, not
  0x5BC53C/0x5BC538 directly, but downstream of the same broken font
  system) comes back 0 and gets used as a **divisor** with no zero-check
  → `EXCEPTION_INT_DIVIDE_BY_ZERO` (0xC0000094). Note: the VEH handler in
  `main.c` had no case for this exception code at all, so it crashed with
  no diagnostic output - added a case there (mirrors the access-violation
  one: RIP/RVA, Xbox regs, native stack) before this could even be
  diagnosed. Fixed with a one-line guard (`if (ecx == 0) ecx = 1;`) right
  before the division - the quotient feeds rendering geometry that's
  irrelevant without real D3D anyway.

- **`sub_001186A0`** (`recomp_0007.c`, gitignored) - **a genuine infinite
  loop, not a crash.** A 16-byte-node tree/tree-pool walk (`0x3FFFFFFF` =
  null-child sentinel) through a pool base that was never allocated (same
  root cause, yet again). Reading zeroed memory forms a spurious cycle
  the walk never escapes - no crash because our zero-mapped low memory
  just returns 0 without faulting. Diagnosed via the watchdog's
  `SuspendThread`+`GetThreadContext` path (see below). Fixed with an
  iteration cap (100,000 - well beyond any real tree depth) that forces
  the "not found" exit (`eax = 0x3FFFFFFF`) instead of spinning forever.
  **Important: the counter must be a per-call local (`uint32_t
  _guard_iter = 0;` declared at the top of the function), not `static`** -
  this function is called repeatedly during normal operation for
  legitimate searches, so a persistent counter would eventually misfire
  during genuine gameplay after enough cumulative iterations.

`recomp_0015.c` and `recomp_0007.c` fixes are gitignored, same caveat as
`sub_002235D0`: **lost if the disasm/recomp pipeline is ever rerun**,
must be reapplied by hand (search this file's git history / the commit
that references this section for exact diffs if needed).

Verified via `smoke_test.ps1`: 39 -> 41 kernel calls, no regression, and
the hang is gone (replaced by a new, later crash - see below).

## Watchdog SuspendThread path: confirmed unreliable (second data point)

Re-enabled the previously-disabled `SuspendThread`+`GetThreadContext`
diagnostic path in the watchdog specifically to catch `sub_001186A0`'s
infinite loop (which makes no ICALLs, so the always-safe ICALL-trace
dump had nothing useful to show). It worked well enough to get a valid
RIP and stack scan (`sub_001186A0` and its callers, all clustered in a
tight address range - consistent with a loop) - **but then caused a
second, distinct access violation immediately after**, reading from a
wildly out-of-range fake "Xbox VA" (`0x32990000`) with a native RSP that
also looked wrong (`0xFE3299FBA8`, not a normal thread stack address).
This is the second time this exact path has caused a secondary crash
(first time noted earlier in this document), so the "possibly a
SuspendThread race" theory looks right rather than being caused by a bug
this session has since fixed. **Re-disabled (wrapped in `#if 0` again)
in `main.c`.** Diagnostics print before the secondary crash, so it's
still usable for one-shot investigation if needed again - just expect
the process to crash a second time right after, harmlessly (the
watchdog's job is already done at that point).

## Current crash (not yet fixed)

Boot now reaches kernel call #41 (up from 39, previously hung
indefinitely at this point via `sub_001186A0`'s infinite loop) before a
new crash. Not yet investigated - next step is resolving its RVA against
`build/*.map` the same way as every fix above.

## sub_0011E7A0, sub_001E8E20 (fixed) - two more instances, then a range-guard lesson

- **`sub_0011E7A0`** (`recomp_0007.c`, gitignored, 23 call sites - a
  generic tree/map container's find-or-insert): `sub_001186A0`'s fix
  above (always report "not found" when the tree pool is broken) has a
  side effect here - every call that would normally be a cache **hit**
  now takes the **insert** path instead, since nothing is ever "found".
  Insert allocates from a fixed-capacity bitmap-indexed pool embedded in
  the container object itself; called repeatedly without ever reusing
  existing entries, the pool's "count" field grows without bound across
  the 23 call sites until the computed node address runs off into
  unrelated memory and crashes. Guarded: cap the count at 4096 (far
  above any plausible legitimate small-object-cache size) and jump to
  the function's own existing clean-exit label (`loc_0011E8D2`, already
  used by its other early-exit paths) instead of computing a wild
  address. **Confirms this session's guard-based fixes can shift load
  onto adjacent shared code** - worth watching for as more of these land.

- **`sub_001E8E20`** (`recomp_0014.c`, gitignored - the free-list search
  from the divide-by-zero investigation): walks a free-list array whose
  slots can hold either 0, a real cached-object pointer, or leftover
  garbage that happens to look like a pointer (the same misread-as-
  pointer pattern as `sub_002235D0`'s "igStringObj" string). First fix
  used a "plausible Xbox VA" range guard (`0x00010000`-`0x02000000`,
  matching `sub_0019F765`'s convention) - **this was too permissive**: it
  passed a garbage value because that range covers all of `.text` plus
  every unrecompiled XDK library section (D3D, DSOUND, etc.), and a
  pointer into DSOUND's code happened to look "plausible" while still
  being nonsense as an object pointer. **Tightened to the actual heap
  range** (`XBOX_HEAP_BASE` = `0x00880000` from `xbox_memory_layout.h`,
  through `0x08000000`) instead, which correctly excludes `.text`,
  `.rdata`, `.data`, and every library section at once - only genuine
  heap allocations pass. **Lesson for future range guards in this
  codebase: prefer the heap-range bound over the broader
  `0x00010000`-`0x02000000` one** unless there's a specific reason a
  static/`.rdata` pointer is legitimately expected.

Verified via `smoke_test.ps1`: 41 -> 43 kernel calls with the first pass
of these two fixes, then **41 -> 57** once the guard was tightened to the
heap range - a large jump, confirming the untightened guard had been
silently letting bad pointers through into other nearby code paths that
happened not to crash (yet).

## Genuine multi-function recursion (fixed) - a 374-instance template pattern

At kernel call #57 the process hit a **stack overflow**
(`STATUS_STACK_OVERFLOW`, `0xC00000FD`) - `smoke_test.ps1` flags this by
signature regardless of how far kernel calls got, since it's the same
exception code as the earlier (already fixed) CRT lock-bootstrap
recursion bug. **This was a different bug that happened to produce the
same exception code**, not a regression of the old one.

ICALL trace immediately before the crash showed repeated `[ICALL] Failed
to resolve VA 0xFFFFFFFF` (a `-1`/"not found" sentinel being called as
if it were a function pointer) with an identical repeating call chain:
`sub_0013A6A0 -> sub_0013AC10 -> sub_0013AE50 -> sub_0013B0E0 ->
sub_0013B220 -> sub_00145E60 -> sub_001463F0 -> back into sub_0013A6A0`.

Traced to `sub_0013AC10`: it fetches a "dependency type" id via an ICALL
through an uninitialized 16-byte-stride table (`eax = MEM32(edx+esi+4);
ICALL(MEM32(eax+4))` - same table-corruption family as the tree/bitmap
fixes above), and **unconditionally** calls `sub_0013A6A0()` to construct
that type regardless of whether the lookup succeeded - and
`sub_0013A6A0`'s own construction chain calls back into more of these
same dispatch functions for its own sub-dependencies, closing the loop.

**This turned out to be a template-generated pattern, not a one-off**:
grepping for the exact structural shape (ICALL through `MEM32(eax+4)`,
followed by `esi = MEM32(esp+0xC); PUSH eax,esi; call some
constructor(); esp+=8; eax=esi;`) found **374 raw occurrences of the
`ICALL(MEM32(eax+4))` idiom across 18 generated files, of which 59
matched the exact recursive-dispatch shape** (the other ~300+ are the
same macro used for unrelated purposes - destructors, generic vtable
calls, etc. - verified by sampling several that did NOT match before
trusting the narrower pattern). Wrote a Python script
(`re.compile` on the exact multi-line template, substituting a
guard + preserving the callee name and labels) to apply the identical
fix to all 59 matches at once: skip the recursive construct call (and
its balancing `esp = esp + 8`) when the lookup failed, since the
epilogue's return value (`esi`) doesn't depend on it either way.
Verified the transform matched cleanly (no partial/malformed matches)
before rebuilding.

Files touched (all gitignored, **lost if the disasm/recomp pipeline is
ever rerun** - reapply by re-running the same regex+substitution, or
search this commit's diff for the exact transform):
`recomp_0007.c` (6), `recomp_0008.c` (41, includes the originally-traced
`sub_0013AC10`, hand-fixed first then left alone by the script since its
wording already differed from the template), `recomp_0009.c` (10),
`recomp_0011.c` (2).

Verified via `smoke_test.ps1`: stack overflow is gone entirely (back to
a normal access-violation exit code), kernel calls hold at 57 (the same
point reached before - the recursion was consuming stack, not blocking
forward progress once it started, so fixing it revealed the *next*
crash rather than advancing further). Baseline updated to 57.

## Three more instances (fixed) - all in the same dispatch-function family

All three of these are in `recomp_0008.c` (gitignored - same reapply
caveat as everything else in this file), inside members of the
"get-or-construct dependency by type id" family from the recursion fix
above (`sub_0013AE50`, `sub_0013B0E0` - two of the original 7 functions
in the recursion cycle). Each is the same "an object pointer field is
garbage instead of null/valid, and code writes through it unconditionally"
pattern, just in different fields:

- **`sub_0013AE50`**: `MEM32(edi+0x14)` is supposed to hold a
  bounding-box object pointer; instead held a leftover float bit pattern
  (`0xC3480000` = -200.0f) and got written through while initializing
  default AABB min/max bounds. A second, adjacent block does the same
  thing via `MEM32(esi)`. Guarded both with the standard heap-range check
  (`0x00880000`-`0x08000000`).
- **`sub_0013B0E0`**: a *different* shape - not a null/garbage pointer,
  but a loop index (`ecx = MEM32(esi+0x104)`) that should be bounded to
  `0..0x3F` (the loop's own 64-iteration cap) holding a garbage value
  (`0x3F800000` = 1.0f as a float bit pattern) instead, turning `esi +
  ecx*4` into a wild address. Guarded with a bounds check (`ecx < 0x40`)
  matching the loop's own existing invariant, rather than the heap-range
  convention (this field isn't a pointer, it's an index).

Verified via `smoke_test.ps1` after each: 57 kernel calls held (no
regression) with the crash location moving forward each time, confirming
each fix was real progress even though the kernel-call count itself
didn't increase (these three crashes all happen within the same burst of
code between kernel call #57 and whatever comes next, so kernel-call
count isn't a reliable progress signal at this granularity - watch the
crash RVA moving to new functions instead).

## More dispatch-family instances, plus two important guard-convention fixes

Continued whack-a-mole through `sub_0013AE50`/`sub_0013B0E0` (both in the
original recursion cycle's function family, `recomp_0008.c`, gitignored):
four more `edi+4`/`edi+8`/`edx` pointer fields guarded with the same
heap-range check, one more loop-index guard on `ebx`. Same pattern every
time: an object pointer field holds garbage instead of null/valid, code
dereferences it unconditionally.

Two lessons worth keeping in mind for any future guard in this codebase:

1. **The heap-range upper bound was wrong.** Every guard so far used
   `0x00880000`-`0x08000000` (128MB), copied from an earlier one without
   checking - but `XBOX_TOTAL_RAM` (`xbox_memory_layout.h`) is **64MB**
   (`0x04000000`), the real address space ceiling. `0x08000000` let
   through addresses in the upper 64MB that were never actually mapped.
   **Fixed globally**: every existing `0x08000000u` guard bound across
   `recomp_0008.c`/`recomp_0014.c`/`recomp_0015.c` replaced with
   `0x04000000u` via a find-and-replace (11 occurrences, verified by
   count before/after). Use `0x00880000u`/`0x04000000u` for all future
   heap-range guards in this codebase.

2. **A pointer passing the range check isn't enough - the DATA it points
   to can still be garbage.** In `sub_001E8E20`, `esi` (a free-list
   pointer) legitimately passed the heap-range guard (it was
   `0x01000000`, a numerically valid-looking address) - but the memory
   *at* that address hadn't been genuinely allocated by our bump
   allocator, so `eax = MEM32(esi)` returned unrelated garbage
   (`0xFF000000`), and `RECOMP_ICALL_SAFE(MEM32(eax + 0x78), ...)`
   crashed *before its own internal bounds check could even run* -
   `MEM32(eax + 0x78)` is evaluated as the macro's argument, and that
   read faults natively on genuinely out-of-bounds memory regardless of
   what the macro does with the value afterward. **The fix must validate
   every pointer you're about to dereference, not just the first one in
   a chain** - added a second guard on `eax` (the value read *through*
   `esi`) before using it, at both symmetric call sites in this function.

Verified via `smoke_test.ps1`: 57 -> 58 kernel calls (small but real
progress - most of this round's fixes landed within the same burst of
code between kernel calls, same caveat as the previous round). Baseline
updated to 58.

## Two more instances (fixed) - one new function, pattern still holding

- **`sub_00200B18`** (the divide-by-zero function from earlier):
  `edx = MEM32(ebx)` is the same "data read through an already-valid
  pointer can still be garbage" case as `sub_001E8E20` - guarded the
  ICALL through it (`MEM32(edx + 0x50)`), replicating
  `RECOMP_ICALL_SAFE`'s own fallback (`eax = 0`, `esp` restored to the
  pre-push checkpoint) in the guard's else-branch since the macro itself
  can't be reached safely. **Watch for this exact trap when editing
  push/reassign pairs**: the original code interleaves `PUSH32(esp, X)`
  then `X = new_value` - pushing the *old* value before overwriting it.
  An earlier draft of this fix reordered them (reassign then push),
  which pushes the wrong value and breaks whatever pops it later. Keep
  push/reassign pairs in their original relative order even when adding
  a guard around them.
- **`sub_00259CC0`** (`recomp_0018.c`, gitignored, first fix outside the
  `sub_0013Axxx`/`sub_0013Bxxx` family) - `MEM32(ecx + 0x1C)` should be
  null or a valid pointer, holds garbage instead. Guarded by treating an
  implausible value the same as the existing null check.

Verified via `smoke_test.ps1` after each: 58 kernel calls held, no
regression.

## Attempted: full-function sweep of sub_0013AE50 - reverted, regression

Tried the "guard everything in one pass" idea from the note above:
read through the whole function and found 7 ref-count increment/decrement
sites following the pattern `if (X == 0) goto skip; ... MEM32(X+4)
+= or -= 1;` that only checked for exact-zero, not garbage-but-nonzero
- extended each to also treat an implausible pointer as the skip
condition, plus one more `MEM32(ecx+0x20)` dereference guard. 8 guards
total, applied in one batch.

**Result: regression (58 -> 57), reverted.** The new failure was a
different shape - deep recursion again (a "Spin-loop probe" fired, `esp`
dropped to `0x006FB654` vs. the normal `~0x00F7Fxxx`, meaning real stack
depth this time, not the tight-loop kind). Root cause not fully
diagnosed before reverting, but the likely explanation: at least one of
those 7 fields is a legitimate ref-counted pointer in the surviving code
paths (unlike the D3D-null fields fixed elsewhere), and one of these
particular objects' construction genuinely depends on *this* function's
own earlier calls succeeding (`sub_0013AC10`/`sub_0013AC60`/etc.) rather
than being permanently D3D-blocked - so treating "doesn't look like a
heap pointer yet" as "skip forever" broke a legitimate not-yet-
constructed-but-will-be-shortly case, causing something to retry/recurse
instead of proceeding normally.

**Reverted via a scripted reverse of the exact 8 replacements** (kept a
record of old/new text pairs from the apply script, ran the same
substitution backwards) rather than manually re-deriving the original
code - much lower risk of a transcription slip. Confirmed back to 58 via
`smoke_test.ps1` before moving on.

**Lesson - retract the earlier "consider a full sweep" advice**: for
this specific pattern (ref-count touch on a possibly-shared object,
as opposed to a clearly-always-null-until-D3D-works field like a bbox or
font table), the crash-driven one-at-a-time approach is safer despite
being slower, because it only touches fields actually proven to crash on
the CURRENT execution path with the CURRENT set of upstream fixes - a
speculative broad sweep guards fields that might be legitimately
populated by a code path this session hasn't reached yet, and "fixing"
those preemptively can silently break correct behavior instead.

## The actual root cause of the recurring sub_0013AE50 crashes (fixed)

Two individual guard attempts at `sub_0013AE50`'s `loc_0013AED6`
(`ecx = MEM32(MEM32(ebp) + 0x20)`) both regressed when tried in
isolation (58 -> 57 each time, reverted both). That forced a rethink
instead of a third guess.

**The real root cause**: the 60-site "get-or-construct dependency by
type id" family (the recursion fix from earlier) skips the construct
call when the type-id lookup fails - correct, stops the recursion - but
never writes anything to the **output slot** in that case. The slot
(a field inside the caller's own object, e.g. `edi+0x10`) is left
holding whatever was there *before* this object's own allocation -
uninitialized garbage, not 0. Every consumer that later reads that slot
(`sub_0013AE50`'s `loc_0013AED6` among others) assumes "0 (not
constructed) or a real pointer" and breaks on "garbage that's neither."

This is exactly why the individual guards at the *consumer* end kept
being fragile: they were patching the symptom at one read site while
the actual bug (an uninitialized write) could still corrupt whatever
*other* code reads that same slot next. **Fixed at the source instead**:
extended the same 60-site scripted transform from the recursion fix to
add an `else { MEM32(esi) = 0; }` branch - when construction is skipped,
explicitly zero the output slot, matching what every consumer already
assumes "not constructed" looks like.

Verified via `smoke_test.ps1`: 58 -> 61 kernel calls, no regression -
and this fixed `sub_0013AE50`'s recurring crash without touching
`sub_0013AE50` at all, confirming the diagnosis. Baseline updated to 61.

**Lesson**: when the same handful of functions keep needing new guards
for what looks like "the same kind of garbage" repeatedly, look upstream
at whatever *constructs* the value before continuing to guard every
place that *reads* it - the recursion-fix family here was exactly that
kind of shared constructor, and fixing its skip-path was a single,
low-risk change ("null" is *more* correct than leaving stale data) that
resolved multiple different-looking consumer crashes at once, unlike
the earlier failed attempt to guard 7 different *consumer* sites
speculatively in one batch.

## sub_0020E547 - root-caused via raw disassembly, fixed (took 4 attempts)

```
[CRASH] fault addr read Xbox VA 0xFFFFFFFF (MEM32(eax) where eax=-1)
Xbox regs: eax=0xFFFFFFFF ecx=0 edx=0x624F203A esp=0x0047C43C
ebx=0x003E3374 esi=0 edi=0
```

**Attempt 1**: redirect to the function's existing alternate lookup path
when `eax` isn't a plausible heap pointer. Regressed (new stack
overflow). Reverted.

**Root-caused via raw disassembly** (see "Technique: raw x86
disassembly" below for the reusable method) rather than guessing from
the lifted C a third time. Findings:

- `sub_0020E520` and `sub_0020E547` are **one logical x86 function**,
  not two - `0x0020E53F: jne 0x20e547` jumps directly into what our
  lifter split off as a separate C function (mid-function boundary
  split, same general class of issue as the earlier-fixed lifter/
  translator `g_seh_ebp` propagation bug, but this specific instance
  wasn't caught by that fix since it's a plain conditional jump, not a
  tail-call-shaped one).
- The real function's signature takes **one stack argument** - both
  exits are `ret 4`, not `ret 0`. Our toolkit's function-signature
  detection decided this function takes 0 stack params, so **every call
  site** (there are ~300+ across the codebase) pushes only the dummy
  return address, never the real argument. `MEM32(esp+8)` at
  `loc_0020E547` is reading **genuinely uninitialized caller-stack
  memory by construction** - not a D3D-null-pattern value, a toolkit
  signature-detection bug. (Not investigated further: whether this
  false-0-params pattern affects other functions site-wide, or is
  specific to this one - worth a systematic check if this keeps coming
  up.)

**Attempt 2**: same redirect-to-fallback fix as attempt 1, now confident
it's the right shape given the disassembly confirms "treat as if the
missing argument were 0". **Regressed again**, same stack-overflow
symptom - but this time diagnosed fully instead of just reverting:

The fallback path (`loc_0020E54F` onward) eventually reaches
`sub_002096B0()`, which chains into an 8-function cycle:
`sub_001E8E20 -> sub_002096B0 -> sub_0020E547 -> sub_00234DF0 ->
sub_0011DFE0 -> sub_0011E7A0 -> sub_0011EAAC -> sub_0011EBB0`. Reverted
again pending investigation.

**Investigated `sub_0011EAAC` via raw disassembly - it's not recursion
at all.** It's a straight-line sequence of ~20-30 direct calls into
`sub_0011E7A0` (our own tree/pool-insert function, already fixed earlier
this session for its own infinite-loop bug) with different
size/index arguments each time (`0x68000`, `0xf8000`, `0x3fc000`,
`0xe8000`, `0xd0000`, `0xb77000`, ...) - this is the **game's own memory
pool registration**, not font glyph enumeration as originally guessed.
No loop, no self-call. It's genuinely deep (many nested construct-
dispatch calls through the 60-site family) but **finite** work.

**The actual bug: our recompiled code uses far more native stack per
logical call than the original x86** (register-simulation macros,
`PUSH32`/`POP32` helpers, etc. all add real stack frames that the
original single `push`/`call` didn't need). Work that fits comfortably
in a normal thread stack on real hardware was overflowing our default
1MB. **Fixed by raising the linker stack size to 16MB**
(`/STACK:16777216` in `src/game/CMakeLists.txt`, committed - the one fix
in this whole investigation that's NOT gitignored/lost-on-regen).

**Attempt 3 (redo of attempt 2, now correct)**: re-applied the
argument-validity guard from attempt 1/2 alongside the bigger stack.
**Worked** - no more stack overflow, back to a normal access-violation
crash, no regression.

**Attempt 4**: that normal crash was a second instance of the
"garbage data read through an already-valid pointer" pattern
(`edx = MEM32(eax)` where `eax` is plausible but the data at it isn't) -
same lesson as `sub_001E8E20`/`sub_00200B18`. Guarded with the same
heap-range check on `edx` before using it in `MEM32(edx + 0xCC)`.
**Important subtlety**: the guard must NOT push anything before
skipping the ICALL block, even though the success path does
`PUSH32(esp, ebx)` before reassigning `ebx` - because
`RECOMP_ICALL_SAFE`'s own failure path restores `esp` to the value
captured *before* that push too, so the correctly-balanced "as if the
ICALL had failed" behavior is to touch `esp` not at all, not to
replicate the push.

Verified via `smoke_test.ps1` after each of the last two steps: 61
kernel calls held, no regression, and the stack overflow is completely
gone. Baseline still 61 (next crash is different code, not more kernel
calls yet).

## Current crash (not yet fixed) - corrupted esp inside sub_002235D0

```
[CRASH] Access violation, fault addr write to Xbox VA 0xFFFFFFA0
Xbox regs: eax=0 ecx=0x005D93FC edx=0x001A4340 esp=0xFFFFFFA0 (!)
ebx=0 esi=0x00F803A8 edi=0x00309736
```

`esp` itself is corrupted (`0xFFFFFFA0`, i.e. -96) - a genuine stack-
depth mismatch somewhere, not the usual garbage-pointer pattern. Crash
is inside `sub_002235D0` (the "igStringObj" function fixed earlier this
session for an unrelated bug), reached via `sub_0020E547 ->
sub_00234DF0 -> ...`.

**Diagnosed with a targeted debug print** at `sub_002235D0`'s entry
(before its own `esp -= 0x108` local-frame allocation): across 5 calls,
`g_esp` at entry was `0x00F7FCC4` (first, normal), then
`0x00502744 -> 0x005025DC -> 0x00502474 -> 0x0050230C` (steadily
*decreasing* by roughly `0x16C` per call, but not yet corrupted at entry
for the calls captured). Confirms the corruption *accumulates* across
many calls rather than happening in one bad step - consistent with
either a genuine deep-nesting depth issue or a slow per-call leak, not a
single wrong `PUSH`/`POP`.

**Found something real while investigating, but it didn't fix this
crash**: several guards added this session used `0x00880000u` as the
heap lower bound, copied from a comment on `XBOX_HEAP_BASE` in
`xbox_memory_layout.h`. That comment was **stale** - correct for the
original 1MB stack size, never updated when the stack grew to 8MB (see
the comment directly above it explaining that increase). The actual
computed value is `0x00F80000`, not `0x00880000` - meaning every one of
those guards has been accepting addresses in the `0x00880000`-`0x00F80000`
range (which is actually *inside the live stack region*) as if they were
valid heap pointers. Fixed the comment (harmless, correct now). **Tried
correcting all 18 guard literals to the real boundary - regressed
further (61 -> 59), reverted.** So the wider, technically-wrong bound is
currently load-bearing: some legitimate value at least once falls in
that stack-region range and the guards need to keep accepting it, even
though the theory (stack-region addresses accepted as heap pointers
could explain corruption if something writes through one) remains
plausible for *this specific* crash. Left as an open question rather
than pushed further tonight - both the literal value and the crash
itself are documented for a fresh look.

**Found the actual leak via raw disassembly, not instrumentation.**
Reading `sub_0011EAAC` in full (it's the memory-pool-registration
function from the earlier investigation, called from `sub_0011E980`
which is called from `sub_0011EBB0`) found the lifter's generated C
function **ends mid-sequence**:

```c
loc_0011EB63: ;
    PUSH32(esp, 4);
    PUSH32(esp, 0xA4000);

}   // <-- function just ends here, no call, no tail-call
```

Every one of the ~12 pool registrations before this one follows the
identical shape: `push 4; push <size>; push <index>; mov ecx,esi; push 0
(retaddr); call sub_0011E7A0` (5 pushes + call). This 13th one is
missing the index push and the call - and `sub_0011EB6A` (the very next
function in the file) starts with **exactly the missing continuation**:
`PUSH32(esp, 0xD); ecx = esi; PUSH32(esp, 0); sub_0011E7A0();`. High
confidence this is a genuine lifter bug (dropped the tail-call/
fallthrough at this specific mid-function split), not a D3D-null
pattern - and it leaks exactly 2 simulated-stack dwords (8 bytes) every
time `sub_0011EAAC` is called, since the function returns without ever
undoing these 2 pushes.

**Fix validated but NOT currently active** (see below for why):
```c
sub_0011EB6A(); return;   // added right before the closing brace, no
                          // g_seh_ebp reassignment needed - both
                          // functions are headless continuations that
                          // never declared their own ebp
```

**Applying it alone regressed (61 -> 57)** - not because the fix is
wrong, but because completing this dropped call means `sub_0011E7A0`
actually runs a 13th time with real arguments, executing more game code
than ever ran before and reaching a **different, previously-unreached**
bug sooner: `sub_00200B18` (the divide-by-zero function from earlier)
reads a global (`MEM32(0x5BB894)`) that's garbage (`0x80000000`) and
dereferences it directly. Added a guard there (validated, safe -
confirmed harmless in isolation via `smoke_test.ps1` at the current 61
baseline with the `sub_0011EAAC` fix reverted). Combining both fixes
progressed further (57 -> 59) but hit *another* instance of the same
esp-corruption symptom one level deeper, still below the 61 baseline.

**Current state**: `sub_0011EAAC`'s fix is written and correct but
commented out / reverted (search `recomp_0007.c` for `loc_0011EB63` -
currently ends with the bare `PUSH32(esp, 0xA4000);` followed directly
by `}`; the fix is documented here verbatim to reapply). The
`sub_00200B18` guards (both the `eax` validation before
`MEM16(eax+0x12)` and the chain of validations at `loc_00200B2D`) are
**kept active** since they're harmless on the current path and will be
needed once the deeper chain is resolved.

**Real next step**: this needs the push/pop-count instrumentation
originally planned, but applied to `sub_0011E7A0`'s 13th invocation
specifically.

**Searched for how widespread this bug class is - it's not isolated.**
Grepped every generated file for the exact shape (a bare `PUSH32(esp,
...)` as a function's last statement, with the closing `}` immediately
after - no call, no tail-call, no `return`):

```python
pattern = re.compile(r"    PUSH32\(esp, [^;]+\);\n\n\}\n")
```

**50 matches across 21 files.** This strongly suggests a systematic
lifter/disassembler bug (likely in how mid-function boundaries get
detected when a `PUSH`-heavy argument-setup sequence straddles a
function split point our disassembler treats as a new function start),
not a one-off transcription error in this specific spot. This is a
genuine toolkit-level fix candidate - fixing it in `lifter.py`/
`translator.py` (following the same precedent as the earlier
`g_seh_ebp` propagation fix from the very start of this session) would
be far more valuable than patching each of the 50 sites individually,
and would likely resolve a cluster of not-yet-encountered crashes at
once, the same way the 60-site output-slot fix did.

## All 50 dropped-tail-call sites fixed (verified, not speculative)

Before mass-fixing, verified each of the 50 sites individually via a
script: for every occurrence, confirmed the function's own `Original:
... - END` address exactly equals the *next* function's `Original:
START - ...` address. **All 50 matched with zero exceptions** - strong
confirmation this is a uniform, systematic split-point bug, not a mix
of real bugs and legitimate dead code.

Applied the fix to all 50 (append `{g_seh_ebp = ebp; }funcName();
return;` right after the dangling pushes, using the `ebp`-aware form
only for the small number of sites where the truncated function had
declared its own `ebp` local - in practice **all 50 were headless
continuations with no declared `ebp`**, so all 50 got the plain
`funcName(); return;` form). Compiled clean across all 21 affected
files (only pre-existing warnings, no new errors), linked successfully.

**Saved the fix script for reuse**: `src/game/tools_data/
fix_dropped_tailcalls.py` (git-tracked, unlike the generated files it
patches) - rerun this after any future regeneration of
`src/recomp/gen/*.c` via the disasm/func_id/recomp pipeline, since these
50 patches live in gitignored generated files and would otherwise be
silently lost.

**Result: 61 -> 59 kernel calls (not yet a net win).** Confirmed via
RVA resolution that the new crash is the **same already-documented
esp-corruption issue** (`sub_0013AE50`, `esp` in the same `0x0058Fxxx`
range as earlier tonight's investigation), not a new problem introduced
by any of the 50 fixes. This matches exactly what was predicted when
`sub_0011EAAC`'s fix was tested in isolation earlier: completing these
dropped calls means more game code actually runs than ever before,
which reaches the *next* already-present bug sooner rather than
introducing a new one. **The 50 fixes are correct and worth keeping**
(no downside observed, real upside once the deeper esp-corruption chain
is resolved) - they're just not sufficient on their own to beat the
baseline. The `sub_00200B18` guards from the earlier investigation are
still in place and still relevant.

**Next step**: the deeper esp-corruption chain (`sub_0013AE50` /
`sub_00200B18` / the font-glyph-adjacent construct chain) is now the
sole remaining blocker to progress past 59/61. Worth checking first
whether *this* crash is ALSO a dropped-tail-call instance my regex
missed (a different trailing shape, e.g. two dangling pushes or a
truncated ICALL block) before assuming it needs a bespoke guard -
apply the same raw-disassembly-first discipline that worked for
`sub_0020E547`.

## sub_0013AE50 edx guard (fixed) - reached genuine new territory (60, 61)

Following the plan above: `sub_0013AE50` at `loc_0013AFEA` had the same
"garbage data read through an already-valid pointer" shape as
`sub_001E8E20`/`sub_00200B18`/`sub_0020E547` - `edx = MEM32(eax)` (eax
itself fine, the data at it garbage: observed `0xBF800000` = **-1.0f**,
traced to `sub_0012FAF0`, a legitimate matrix/transform-init function
that writes `-1.0f`/`1.0f` alternating into a range of fields - some
*other* code is misreading one of those float fields as a vtable
pointer). Guarded the same way as the others. **Worked cleanly**: no
regression, and confirmed via kernel-call log that execution now
genuinely reaches calls **#60 and #61** (both new - the process was
never observed reaching that far before, in any prior run this
session), before hitting a new crash. This is real forward progress
even though `smoke_test.ps1`'s "61 kernel calls" number looks identical
to the pre-50-fix baseline - the *path* getting there is now different
and goes further before the next wall.

## Current crash: sub_0020E520 "this" pointer is the same -1.0f value

```
[CRASH] fault addr read Xbox VA 0xBF80003C (ecx/esi + 0x3C)
Xbox regs: eax=0 ecx=0xBF800000 edx=4 esi=0xBF800000 esp=0x005027B8
ebx=1 edi=0
```

Same `0xBF800000` (-1.0f) value, now arriving as the **"this" pointer**
(`ecx`, then copied to `esi`) of `sub_0020E520` - a completely different
manifestation of the same root value being misused, several calls
downstream from where it first appeared as a matrix field.

**Tried a guard at `sub_0020E520`'s own entry** (validate `esi` before
`MEM32(esi + 0x3C)`, redirecting to the function's genuine clean exit
since the "obvious" redirect target - `loc_0020E53B` - ALSO
dereferences `esi` unsafely). **Regressed badly (61 -> 44) and was
reverted.** `sub_0020E520` has **465 call sites** across the codebase
(`grep -rn "sub_0020E520()" src/recomp/gen/*.c | wc -l`) - a generic
entry guard rejects some *legitimate* "this" pointer shape used by
other, unrelated, much-earlier-executing callers (almost certainly
objects living outside the `0x00880000`-`0x04000000` heap range by
design, e.g. `.data`-resident singletons - this function is far too
widely shared for a blanket range check).

**Not yet found**: the *specific* caller (one of 465) that passes this
particular `-1.0f` value as `sub_0020E520`'s "this" argument. `grep`
alone doesn't scope it down; would need either (a) a temporary counter/
breakpoint in `sub_0020E520` itself that captures a stack trace on the
*first* call where `ecx` fails a plausibility check (matching the
`recomp_icall_fail_log` Nth-failure technique used earlier in the
session, adapted to a direct-call site instead of an ICALL), or (b)
tracing forward from `sub_0012FAF0` (the matrix-init function) to find
which object type gets both matrix-initialized AND later passed to
`sub_0020E520`, narrowing the caller by object type rather than by
brute-force search. Left as the next lead - **do not add another
generic guard to `sub_0020E520` itself**, that call site count is a
hard warning sign per the narrow-vs-wide-scope lesson from earlier in
this document.

## Technique: raw x86 disassembly for ambiguous lifted-C cases

When the lifted C's control flow or calling convention is genuinely
unclear (mid-function splits, uncertain stack-argument counts) - as
opposed to the "garbage pointer vs. null" cases every other fix this
session has been - don't keep guessing from the C. Disassemble the raw
bytes directly:

```python
import capstone
XBE = r"D:\My Games\Xbox Recomp\src\game\game\default.xbe"
TEXT_VA, TEXT_RAW, TEXT_SIZE = 0x00011000, 0x00001000, 3448212  # from xmen_analysis.json's .text section
def va_to_file_off(va): return TEXT_RAW + (va - TEXT_VA)
with open(XBE, "rb") as f:
    f.seek(va_to_file_off(start_va)); data = f.read(end_va - start_va)
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
for insn in md.disasm(data, start_va):
    print(f"0x{insn.address:08X}:  {insn.mnemonic} {insn.op_str}")
```

This is faster and far more reliable than re-deriving control flow from
the generated C by eye - it immediately showed the `jne` jumping into
the "separate" function and the `ret 4` signature mismatch, both of
which would have taken many more failed guesses to find otherwise. Worth
reaching for this earlier whenever a fix attempt regresses more than
once on the same site.

## Found the `sub_0020E520` caller, via a scoped diagnostic probe - and it led to a much bigger, systemic bug

Followed the plan from the previous section: added a **diagnostic-only**
probe at the very top of `sub_0020E520` (no behavior change) that
captures a native stack trace the first time `ecx` fails the plausible-
heap-pointer check, deduping by distinct `ecx` value (up to 16) instead
of firing only once, since the *first* implausible value turned out to
be a harmless, unrelated `ecx=0` case - the actual crash value
(`0xBF800000`) only showed up as the *second* distinct implausible
value once dedup-by-value was in place.

Resolving the captured RVAs against `build/*.map` (same technique as
before) gave the direct caller: `sub_00234DF0`, which does

```c
loc_00234E04: ;
    ecx = MEM32(esp + 8);
    esp = esp + 4;
    PUSH32(esp, ecx);
    ecx = MEM32(0x5BC498);              /* <-- reads a global "cache slot" */
    PUSH32(esp, 0); sub_0020E520();     /* passes it as "this" */
```

`0x5BC498` is a global living in `.data`'s zero-initialized BSS tail
(confirmed via a small XBE-header/section-table parser - the address is
past the section's raw file data, so on real hardware it starts at 0
and must be *written* by a lazy-init/"magic static" pattern before
first use). The write is supposed to come from:

```
sub_00234DF0 -> sub_00209650(0, 0x231820) -> [ICALL to sub_00231820]
             -> sub_00231820 -> sub_002235D0(..., &0x5BC498, ...)
             -> sub_002235D0 either reuses a cached object at *0x5BC498,
                or constructs a fresh one and writes its pointer there
```

A second scoped diagnostic (tracing `sub_002235D0` specifically when its
"cache slot" argument equals `0x5BC498`) never fired at all - meaning
the whole `sub_00209650 -> sub_00231820 -> sub_002235D0` chain never
actually ran before `sub_00234DF0` read the (still-zero-turned-garbage)
global. So the bug wasn't in `sub_002235D0`; something upstream was
silently skipping the entire initializer call.

### Root cause: systemic lifter bug in indirect-call operand translation

`sub_00209650`'s real x86 (confirmed via raw capstone disassembly) is:

```
push edi
call dword ptr [esp + 8]      ; the actual initializer function pointer
```

The lifter translated this as:

```c
{ uint32_t _icall_esp = g_esp;
PUSH32(esp, edi);
PUSH32(esp, 0); RECOMP_ICALL_SAFE(MEM32(esp + 8), _icall_esp); /* indirect call */
}
```

On real x86, `call [esp+8]` computes its memory operand **before** the
CPU pushes the return address as part of executing the `call`. The
lifter's `PUSH32(esp, 0)` models that implicit return-address push, but
it runs (as a C statement) **before** `MEM32(esp + 8)` is evaluated -
one push too early. So the operand ends up reading 4 bytes deeper on
the simulated stack than it should: instead of the caller's pushed
function pointer (`0x231820`), it reads whatever was sitting at that
now-shifted slot - in this case a stale `0` (the *caller's own* dummy
return-address placeholder from the "PUSH32(esp,0); sub_00209650();"
call convention). ICALL to VA 0 predictably fails and falls back to
`eax = 0` with no call made at all - which is exactly why the entire
initializer chain silently never executed.

This is a **general codegen bug**, not specific to this one call site:
any `call [esp+N]` gets mistranslated the same way, because the bug is
purely about *statement order* in the generated C, independent of what
`N` is or what else is going on. Confirmed by grepping for the shape
`RECOMP_ICALL_SAFE(MEM32(esp + ...)` across `src/recomp/gen/*.c`: **37
occurrences across 10 files**, all with the identical
`PUSH32(esp, 0); RECOMP_ICALL_SAFE(MEM32(esp + N), _icall_esp);` shape.

**Fixed at the source** in `tools/recomp/lifter.py`'s `_lift_call()` -
indirect-call targets are now captured into a temp *before* the dummy
push:

```c
uint32_t _icall_target = MEM32(esp + N);
PUSH32(esp, 0); RECOMP_ICALL_SAFE(_icall_target, _icall_esp);
```

This is safe for register/immediate targets too (evaluating them a
statement earlier doesn't change their value), so the lifter now always
emits this shape for indirect calls rather than special-casing esp-
relative operands.

**Reapplied to the already-generated files** via a new script,
`tools_data/fix_icall_esp_operand.py` (same reusable-script pattern as
`fix_dropped_tailcalls.py` - rerun after any pipeline regeneration in
case it picks up an older `lifter.py`). It patched all 37 sites
mechanically; no manual review needed since the transformation is a
pure, unconditional statement reordering.

### Result: this is real progress, even though the raw kernel-call count regressed (61 -> 39)

Same situation as the earlier `sub_0011EAAC` dropped-tail-call fix: the
fix itself is verified correct (proven via disassembly comparison, not
guesswork), but *completing* previously-silently-skipped initializer
calls means a large amount of code that never used to run now runs -
and that code has its own, previously-unreached bugs. `sub_00209650 ->
sub_002235D0` (the "get-or-construct cached singleton" function
documented earlier in this file, with its own existing manual guard for
a different fake-cached-pointer issue) now genuinely executes, calls
further into `sub_00216FD0`, and crashes there:

```
edi = MEM32(esp + 0x18);   /* the caller's pushed argument */
...
eax = MEM32(edi + 0x34);   /* faults: edi = 0xCCCCCCCC */
```

The `0xCCCCCCCC` argument traces back one level further, to
`sub_002235D0` pushing the **return value of a vtable ICALL**
(`RECOMP_ICALL_SAFE(MEM32(esp + 0x128), ...)`, one of the 37 now-fixed
sites) directly as `sub_00216FD0`'s argument. That ICALL is resolving
and calling a real function now (previously it may not have mattered),
and whatever it calls is itself returning `0xCCCCCCCC` - i.e. reading
something uninitialized several objects deep into this same lazy-init
chain. **Not yet investigated further** - this is fresh, previously
unreachable territory, not a regression caused by the ICALL-operand fix
itself. Next step: trace what real function `MEM32(esp+0x128)` resolves
to at this call (add a scoped one-shot diagnostic the same way as
`sub_0020E520`'s, printing the resolved VA and return value), then walk
into *that* function's real x86 to find what it's supposed to read
before it's actually been constructed.

`smoke_baseline.json`'s `min_kernel_calls` was **not** lowered to 39 -
per the `sub_0011EAAC` precedent, a proven-correct fix that surfaces a
new downstream bug is not a regression to codify into the baseline, and
kernel-call count should climb back past 61 once this next bug is
fixed.

## Second dropped-tail-call site found and fixed: `sub_002085B3`

Followed the `sub_00216FD0` crash (`edi = MEM32(esp+0x18)` reading
`0xCCCCCCCC`, the caller's argument) with a diagnostic-only probe at
its entry (dedup-by-exact-value, same style as `sub_0020E520`'s
probe). Traced the caller: `sub_002235D0`'s "get-or-construct" logic
calls `sub_00216FD0`, which (on the "already has a linked resource"
branch) calls `sub_002085A0`. That function pushes `ebx`/`esi`/`edi`
then branches to either `sub_002085B3` or (its own fallthrough)
`sub_002085CA` - **both** are the *next* function's start address
exactly matching *this* function's own end address (same signature as
the 50-site bug already documented above), i.e. another instance of
the systemic dropped-fallthrough lifter bug. `sub_002085CA` had
already been caught (it's manually overridden in `recomp_manual.c` and
correctly pops `edi`/`esi`/`ebx` + `esp+=8` at its end, matching the
disabled auto-generated version exactly). **`sub_002085B3` had not** -
it ends on `eax = eax >> 2;` with nothing after, not a bare `PUSH32`,
which is why `fix_dropped_tailcalls.py`'s regex (which only matches a
trailing `PUSH32(esp, ...)`) never caught it.

Fixed the same way as the other 50: appended `sub_002085CA(); return;`
after the truncated body (verified via address continuity: this
function's "Original ... - 0x002085CA" end exactly equals
`sub_002085CA`'s "Original: 0x002085CA - ..." start). Confirmed via
the diagnostic probe that this genuinely fixes the `sub_00216FD0`
crash - re-running afterward, execution goes one level deeper (into
`sub_002085CA` itself) instead of faulting in `sub_00216FD0`.

**Lesson for any future re-run of `fix_dropped_tailcalls.py`**: its
`DROP_PAT` regex only matches functions that end on a dangling
`PUSH32(esp, ...)` line. A dropped fallthrough can end on *any*
statement (here, a `shr`/`>>`) - the reliable signal is address
continuity (`this function's own "Original: ... - END"` exactly equals
`next function's "Original: START - ..."`), not the trailing statement
shape. Worth extending the script to flag *every* address-continuous
function pair for manual review, not just ones ending in a bare push.

## Third bug in the same chain: not yet found, deep multi-hop corruption

With `sub_002085B3` fixed, the exact same boot sequence now reaches
one level deeper before crashing - **still 39 kernel calls**, still
segfaults, but now inside `sub_002085CA` itself
(`MEM32(g_ecx + g_eax * 4) = g_edx;`, both `g_ecx` and `g_eax` are
`0xCCCCCCCC`). Crash regs are consistently reproducible:
```
eax=0xCCCCCCCC ecx=0xCCCCCCCC edx=0x00F81388 esp=0x00F7FBD4
ebx=0xCCCCCCCC esi=0x0013D370 edi=0xCCCCCCCC
```
`edx=0x00F81388` is a *real* heap pointer (matches the freshly
allocated object confirmed below), so not everything is corrupted -
only `eax`/`ebx`/`ecx`/`edi` (all `0xCCCCCCCC`) and `esi`
(`0x0013D370`, a *code* address - a real function, `sub_0013D370`,
confirmed to exist).

**Call chain** (confirmed via stack-trace probes + `build/*.map`
resolution): `sub_00239E50` (legit one-time global initializer, guards
itself via a 64-bit call counter at `0x5BC510`/`0x5BC514`) ->
`sub_00236500` -> ... -> `sub_002282E0` -> `sub_00209650` ->
`sub_00227E00` -> `sub_002235D0` (class/type "get-or-construct"
registration, slot = `0x3FA818`, name = NULL) -> `sub_00216FD0` ->
`sub_002085A0` -> `sub_002085B3` -> `sub_002085CA` (crash).

**Ruled out via direct diagnostic probes** (add-print/rebuild/run,
each removed after use - none left in the tree):
- `sub_002235D0`'s construction path: `eax=0`, `edi(name)=0` at the
  `sub_002226E0()` call site - a clean, deliberate NULL name, not
  uninitialized garbage.
- `sub_002226E0` (called via `sub_00222708`, since `MEM32(0x5BC508)`'s
  flag byte was `0`, i.e. genuinely first-time construction): its own
  allocator (`sub_001F7E00`) routes through the well-known "D3D-null
  allocator" (see `sub_001F84D0`/`sub_001F8890`/`sub_002085CA`
  elsewhere in this doc - always fails, no real D3D hardware) and
  correctly falls back to `sub_003437F3`, which **succeeds**: returned
  `esi=0x00F81388` - a valid, in-range heap pointer.
- `sub_00222708` finishes constructing that object (sets vtable
  `0x3F5D88`, calls `sub_00216EE0`/`sub_00222600`, two vtable ICALLs)
  and returns it intact.
- Back in `sub_002235D0`, `esi = MEM32(ebx)` = `0x00F81388` (confirmed
  valid) at `loc_00223698`. Manually re-read *every* line from there
  through the `sub_00216FD0()` call site (`loc_002236EC`) across all
  branches (`loc_00223825`/`loc_00223821`/`loc_0022379A`/
  `loc_002236F7` included) - `esi` is never reassigned anywhere in
  that stretch, so it should still be `0x00F81388` when pushed as
  `sub_00216FD0`'s "this".
- Manually re-verified `sub_00209650`'s `esi`/`ebx` push/pop discipline
  line by line (it's called twice, nested, in this chain) - every path
  through it (early-exit at `loc_002096A8`, and the
  `loc_0020968E`/`loc_00209694` path) correctly balances its
  `PUSH32(esp,ebx)`/`PUSH32(esp,esi)` at `loc_0020965B` against
  `POP32(esp,esi)`/`POP32(esp,ebx)` at `loc_00209694` before returning.
  No missing pop found here despite the recursive/nested call shape
  being the prime suspect.

**Where the trail was left off**: `0x0013D370` (a real function
address) and `0x0015FDF0` showed up repeatedly and *alternating* in
the `[ICALL] Recent ICALL targets` log around this same point in
execution - a strong signature of `sub_00209650`'s own array-walk loop
(`for esi in 0..ebx: call [MEM32(edi)+esi*4]`, `loc_00209666`) calling
through a two-entry callback array containing these two addresses. Not
yet confirmed whether `esi`/`edi` genuinely end up holding one of
these values (as opposed to a return value from calling them) by the
time `sub_00216FD0` receives them, or through which of its two callers
(`sub_002235D0` vs. `sub_0022FCCB`, both call `sub_00216FD0` - only the
`sub_002235D0` path has been traced so far).

**Next steps for whoever picks this up**:
1. Add a probe directly in `sub_002235D0` right at
   `PUSH32(esp, eax); ecx = esi; PUSH32(esp, 0); sub_00216FD0();`
   (`loc_002236EC`) printing `esi` and the ICALL-returned `eax`
   *immediately* before the call - this pins down definitively whether
   the corruption happens before or after this exact point, closing
   the gap between the confirmed-valid `esi=0x00F81388` at
   `loc_00223698` and the confirmed-garbage `esi=0x0013D370` at the
   crash.
2. If `esi` is still valid there, the bug is inside `sub_00216FD0` or
   `sub_002085A0` themselves (re-disassemble both against raw x86 -
   the `sub_002085A0`/`sub_002085B3`/`sub_002085CA` split already
   proved the C model wasn't trustworthy here at least once this
   session).
3. If `esi` is *already* wrong at `loc_002236EC`, the two vtable ICALLs
   at `loc_002236DC`/`loc_002236E3` (`MEM32(esp+0x124)` /
   `MEM32(esp+0x128)`) are the next suspects - check what real
   functions they resolve to and whether either is `sub_0013D370` or
   `sub_0015FDF0` directly.
4. Do **not** re-guess from the lifted C alone if a second attempt
   regresses - use the raw-disassembly technique (see the dedicated
   section above) on `sub_00216FD0` and `sub_002085A0` next, the same
   way it definitively resolved the `sub_001F7E00` argument-offset
   question earlier in this investigation.

## The empty-stub stack leak (systemic, found 2026-07-29)

`src/recomp/gen/recomp_stubs_unresolved.c` contains **3272** auto-generated
stubs for addresses the translated code calls but the pipeline never detected
as functions. Every one of them is genuinely empty:

```c
void sub_0035D900(void) { /* 0x0035D900: not detected */ }
```

That breaks two invariants of this recompile model at once:

1. **Simulated-stack leak.** The caller emits `PUSH32(esp, 0)` for a dummy
   return address, and the callee is responsible for popping it plus its own
   `__stdcall` arguments (`esp += 4 + N`). An empty stub pops **nothing**, so
   every call leaks at least 4 bytes of simulated stack, more when the real
   function had `ret N`. `sub_0035D900` alone is called 36 times.
2. **Stale return value.** No stub assigns `g_eax`, so a caller reading the
   "return value" gets whatever the previously-executed function left there.
   This is a major source of the garbage-pointer crashes chased all session -
   objects that turn out to be vtables, code addresses used as `this`, etc.

Note both effects are *silent*: no ICALL failure is logged (these are direct
calls, not ICALLs), and nothing faults at the call site.

## Xbox D3D8 is statically linked and called DIRECTLY (not via COM vtables)

Important structural finding, and it corrects an earlier assumption in this
file. On Xbox, D3D8 is linked into the XBE rather than being a COM DLL, so the
game calls it with plain `call` instructions. Consequently:

- **67 D3D-section entry points** are called from translated code, across
  **202 call sites**. That is the entire D3D API surface this game touches.
- There is no device vtable to dump; static analysis alone enumerates the API.
- The game also calls the raw GPU command-FIFO primitive directly:
  `sub_0035D900` is literally
  `[cursor] += 8; [cursor-8] = ecx (method id); [cursor-4] = edx (value)`,
  i.e. Xbox D3D8's inlined `SetRenderState`-style macros writing NV2A methods.
  Only **20 distinct method IDs** are used, all in `0x40300`-`0x40388`.

**Because the goal is a native PC port rather than Xbox emulation, do not build
an NV2A pushbuffer interpreter.** Intercept at these 67 entry points and
reimplement them against a modern graphics backend.

## D3D8 shim, Phase 1 (`src/d3d8_shim.c`)

Generated by `tools_data/gen_d3d8_shim.py`, then hand-maintained. It replaces
the 67 empty D3D stubs with real functions that:

- perform correct `__stdcall` cleanup (`g_esp += 4 + ret_imm`), where `ret_imm`
  is read from each function's actual `ret N` in the XBE (all 67 resolved), and
- set an explicit return value instead of leaking a stale register.

The generator also reports which stubs must be disabled in
`recomp_stubs_unresolved.c`; those lines are now replaced with a
`/* sub_XXXXXXXX: moved to src/d3d8_shim.c ... */` marker. **Careful:** the
original stub bodies contain `/* ... */`, so wrapping them in another block
comment does not nest in C - replace the line outright rather than commenting
it out.

Phase 1 deliberately renders nothing. Phase 2 fills in real behaviour behind a
backend-agnostic interface (D3D11 first, then D3D12/Vulkan behind the same
interface - the user wants all three eventually).

Result: no change to boot progress (still 39 kernel calls, same crash) because
the current blocker does not depend on a stub return value - but the stack-leak
and stale-register classes of corruption are now closed off.

## `sub_00209650`: `_icall_esp` save point captured before a register save

Third distinct lifter bug in this family. `translator.py`'s
`_fixup_icall_esp_save` walks backwards from an ICALL over consecutive `PUSH32`
lines and inserts the esp save before the *first* one, assuming they are all
argument pushes to be unwound on failure. It cannot tell an argument push from
a **callee-saved register save**.

Real x86 at `sub_00209650`:

```
00209650: push edi              <- register save, matched by `pop edi` at 002096A8
00209651: call dword ptr [esp+8]  <- target read from the EXISTING stack; no args pushed
```

With the save point before `push edi`, a failed ICALL rolled `g_esp` back past
the saved `edi`, so the later `POP32(esp, edi)` popped the caller's data.
That ICALL *does* fail here: its target is `0x00227F50`, which has no
recompiled function. Fixed by capturing `_icall_esp` after the register save.

Verified correct against raw disassembly, and confirmed to change behaviour
(crash-site `esp` shifted by exactly 4). It did not by itself fix the current
crash, and did not regress - kept on the same reasoning as the `sub_0011EAAC`
precedent.

**Not swept systemically.** A scan found 15 esp-relative ICALL sites with a
similar shape (plus ~3479 non-esp-relative matches that are mostly false
positives, where the pushed register is genuinely an argument and merely
happens to be popped elsewhere). Each needs per-site disassembly to classify;
given this document's repeated lesson about wide-scope fixes regressing, they
were left alone.

## Tool: `tools_data/whatis.py`

Answers "what is this Xbox VA?" - section, whether it is inside a known
recompiled function, disassembly for code, hexdump plus `.text`-pointer
annotation for data, and named runtime regions (stack/heap/kernel thunk).
Written because that question came up constantly and was being answered by
hand each time.

It immediately cracked the current crash: `0x0013D370` is `sub_0013D370`, a
**one-byte `ret` stub** followed by `int3` padding - which is precisely where
the recurring `0xCCCCCCCC` comes from. Anything that treats that address as an
object and reads a field off it reads `0xCC` padding bytes.

## Current blocker (unresolved): a selector returns a vtable where an object is expected

Fully traced, root not yet fixed:

1. `sub_00213000` is a small selector:
   `ecx = MEM32(MEM32(0x5BC508) + 0x394)`, then it returns
   `[esp+ecx]` from a 2-entry local table `{0x3F3638, 1}`.
2. `MEM32(0x5BC508)` is a valid heap object (`0x00F816C0`), but its `+0x394`
   field is **0**, so the selector returns `0x3F3638`.
3. `whatis.py` shows `0x3F3638` is in `.rdata` and is densely packed with
   `.text` pointers - a vtable/descriptor table.
4. `sub_002235D0` passes that to `sub_00216FD0`, which treats its argument as
   an object with a lazily-initialised cache slot at `+0x34`
   (`mov eax,[edi+0x34]; test eax,eax; jne use_it` ... `mov [edi+0x34],eax`).
5. `+0x34` into a vtable is a **method pointer** (`0x0013D370`, the `ret` stub),
   which is non-zero, so construction is skipped and the stub address is used
   as `this`.
6. Reading fields off it returns `0xCCCCCCCC`; the eventual
   `MEM32(ecx + eax*4) = edx` computes `0xCCCCCCCC + 0xCCCCCCCC*4 = 0xFFFFFFFC`
   and faults.

**Both `sub_00216FD0` and `sub_00213000` were disassembled and match the lifted
C exactly** - this is not a translation bug. The game is genuinely receiving
bad data, and the trail ends at `MEM32(0x5BC508) + 0x394` being 0 when it
presumably should not be. Whatever initialises that field is likely one of the
D3D-dependent paths that currently no-ops.

Next lead: find what writes `+0x394` on that object (a plain grep for `+ 0x394`
is too noisy - the offset is widely reused; scope it to code that also touches
`0x5BC508`).

### Workaround applied: plausibility guard on `sub_00216FD0`'s argument

Since the upstream cause (`MEM32(0x5BC508) + 0x394` being 0) is still unknown,
`sub_00216FD0` now rejects an argument that is not a plausible heap pointer and
takes its own existing no-op exit - the same path already taken when the
argument is 0. Only 2 call sites, so the blast radius is small.

Deliberately does **not** fall through to the construct-and-store branch: that
would write through `MEM32(edi + 0x34)` into read-only descriptor data and
corrupt a vtable.

Result: **39 -> 40 kernel calls**, stable across runs, and the crash moves to a
genuinely different site with much healthier register state - `ecx`, `ebx` and
`esi` are all valid heap pointers now, and only `edi` is bad (`0xFFFFFFF8`,
i.e. `0 - 8`, so something computed `ptr - 8` from a null). New crash is at
map RVA `0x962DB8`.

This is a **workaround, not a root fix.** The real question - what should
initialise `+0x394` on the object at `0x5BC508` - is still open, and is likely
one of the inert D3D paths. Revisit once the D3D shim does real work.

### Generator bug: `ret N` detection walked off the end of the function

`gen_d3d8_shim.py`'s first `stdcall_bytes()` only trusted a `ret` once every
forward branch target had been passed. That skips the *fast-path* `ret` in a
function shaped like `sub_0035D900`:

```
jae  0x35d91c     ; slow path: flush pushbuffer, then retry
...
ret               ; <- fast path, pops 0 - SKIPPED by the old rule
0x35d91c: push edx / push ecx / call flush / pop / jmp back to entry
int3 ...          ; padding: real end of function
```

Having skipped it, the sweep ran through the padding and picked up a `ret 8`
belonging to a **later, unrelated function** - producing exactly the wrong-stack-
cleanup bug this shim exists to eliminate. `sub_0035D900` is called 36 times, so
that was 288 bytes of spurious pops.

Fixed by collecting *every* `ret` in the function and terminating on an int3
padding run (or on a terminator with no forward branch past it), warning if the
immediates disagree. Corrected 3 of 67: `0x0035D100` and `0x0035D160` (4 -> 0)
and `0x0035D900` (8 -> 0).

Worth remembering generally: "scan forward until a `ret`" is not a safe way to
find a function's stack cleanup in optimised code. Padding runs are the reliable
end-of-function marker here.

### Key finding: the game reaches NO D3D call before the current crash

With per-call tracing compiled into the shim (`D3D8_SHIM_TRACE`), a full run to
the crash logs **zero** D3D entry-point calls. Verified the mechanism is live:
trace enabled, all 67 stubs disabled so the shim wins at link, no duplicate
definitions.

This invalidates the working theory that the current blocker is D3D-null
related. The crash happens in ordinary engine code *before graphics
initialisation begins*, so making the shim return real objects cannot help it -
those functions are never reached.

The shim work is still necessary (and fixed two real corruption classes), but it
is **not** on the critical path to the next boot milestone. Debugging effort
should go to the engine-side crash at map RVA `0x962DB8` instead.

## Engine crash after the `sub_00216FD0` guard - investigated, not yet fixed

With the guard in place the crash moves into ordinary engine code. Register
state is much healthier than before: `ecx`/`ebx`/`esi` are all valid heap
pointers and only `edi` is bad (`0xFFFFFFF8`, i.e. `0 - 8`).

Located it as **`sub_001A06E8`** (map RVA varies between builds as layout
shifts - resolve it fresh each time rather than trusting a recorded RVA).

What was established:

- `sub_001A06E8` begins by calling the SEH prolog helper `sub_003432A8`, then
  reads its frame pointer back out of `g_seh_ebp`. **That machinery works** - a
  probe shows `ebp = 0x00F7FF3C`, a valid stack address, not 0. The initial
  "SEH prolog never set ebp" theory was wrong.
- The failure is an **argument-frame mismatch**. `sub_001A06E8` reads
  `MEM32(ebp + 0x1C)`, dereferences it, and memcpy's `0x30` bytes from it - so
  it must be a pointer to a `0x30`-byte struct whose first field is `0x30`
  (a classic Win32 `dwSize` header). The probe shows it reads the value
  **`0x30`** instead: the struct's *contents*, not its address.
- The **caller is correct.** `sub_001A23F3` was disassembled and matches the
  generated C exactly: `lea eax,[ebp-0x34]` / `push eax` (6 args total), with
  `mov dword ptr [ebp-0x34], 0x30` filling the size field.
- Hand-tracing the frame says `ebp + 0x1C` *should* land on arg6. It instead
  lands on the caller's local buffer at `ebp_caller - 0x34`, so some quantity of
  stack drift is present - but the amount was not pinned down.
- **Ruled out: an 8-byte drift.** Instrumenting all 3205 remaining empty stubs
  showed only **2** are called before the crash (`0x001183EC`, `0x0019F7C8`),
  leaking 8 bytes. If the drift were 8 bytes the callee would read arg4; arg4 is
  `MEM32(0x10138)` = `0x1000`, not `0x30`. So residual stub leakage is real but
  is **not** the explanation here.

Next step: probe `esp` in `sub_001A23F3` immediately before the call and in
`sub_001A06E8` at entry, and compare against the hand-traced values, rather than
continuing to derive the offset on paper.

### Gotcha: restoring gen/ from a tar backup can produce a stale build

`tar` preserves mtimes, so files restored from a backup can be *older* than the
existing `.obj` files and ninja will skip recompiling them - you get a binary
still containing code you thought you removed. Symptom seen here: diagnostic
output kept appearing after the probes had been restored away.

After any restore:

```sh
find src/recomp/gen -name '*.c' -exec touch {} +
```

then rebuild, and confirm the expected number of objects actually recompiled.

## ROOT CAUSE FOUND: kernel ordinal 47 popped 24 bytes instead of 8

The `sub_001A06E8` argument-frame mismatch traced to a genuine bug in the
kernel bridge, not the lifter.

`kernel_bridge.c` keeps the same ordinal in two hand-maintained tables:

  * handler table  - `case 47: return bridge_HalReadSMCTrayState;` (2 args)
  * arg-bytes table - `case 47: return 24;  /* HalReadWritePCISpace(6) */`

Nothing enforced agreement, and the arg table is what drives cleanup
(`g_esp += g_slot_arg_bytes[slot]`). So every call to ordinal 47 popped 24
bytes when the caller had pushed 8 - silently raising `esp` by 16.

**The damage that caused.** In `sub_001A23F3`, the real frame is
`push ebp / mov ebp,esp / sub esp,0x34 / push ebx,esi,edi`, so `esp` must be
`ebp-0x40` when it sets up the call to `sub_001A06E8`. The ordinal-47 over-pop
left it at `ebp-0x30` instead. The sequence is:

```
lea eax, [ebp-0x34]         ; pointer to a 0x30-byte struct
push eax                    ; with esp wrong, this lands AT ebp-0x34
mov dword ptr [ebp-0x34], 0x30   ; ...and immediately overwrites it
```

so the callee received `0x30` - the struct's size field - where its address
should have been, then dereferenced it.

**Verified empirically before and after**, via probes in both functions:

| measurement            | before          | after           |
| ---------------------- | --------------- | --------------- |
| caller `esp` vs `ebp-0x40` | `+16`       | `+0`            |
| `arg6` at `[ebp+0x1C]` | `0x00000030`    | `0x00F7FF58`    |
| `MEM32(arg6)`          | (not a pointer) | `0x30` (correct)|

Fixed by setting ordinal 47's cleanup to 8 bytes. The argument count is what
was verified - from the game's own call site, which pushes exactly two values
(`0x461694`, a `.data` pointer, and `1`). That `(pointer, TRUE)` shape matches
`HalRegisterShutdownNotification(Registration, Register)` rather than either
name in the tables, so the *names* in this range are also suspect; the count is
the part that is established.

Crash progresses past `sub_001A06E8` into `sub_001A0B0C` (a later split of the
same original function). Kernel-call count is unchanged at 40 because the
remaining failure still precedes the next kernel call.

### Tool: `tools_data/audit_kernel_ordinals.py`

Cross-checks the two ordinal tables and reports (a) ordinals whose arg-table
comment names a different function than the handler table maps, and (b)
ordinals present in one table but not the other. Exits non-zero on a name
mismatch so it can gate a build. Worth running after any bridge edit - this
class of bug is silent and its symptom appears far from its cause.

Current state: no name mismatches. Two ordinals have **no** arg-bytes entry and
fall through to the `default: return 0`, so they under-pop their arguments:

- ordinal 63 (`bridge_IoCreateSymbolicLink`)
- ordinal 188 (`bridge_NtCreateDirectoryObject`)

Neither is called on the current boot path, and the handlers do not reveal the
true signatures (they ignore most arguments), so **the counts were deliberately
not guessed** - a wrong value here reproduces exactly the bug above. If either
is ever hit, derive the count from a real call site's pushes, the same way
ordinal 47 was settled.

### Gotcha: don't strip probes with a greedy multi-line regex

Removing a diagnostic block with `(?:.*?\n)*?` under `re.DOTALL` matched across
a function boundary and deleted an entire adjacent function, which only showed
up as a link error (`unresolved external symbol sub_001A23F3`). Restoring the
single file from the gen/ backup was the fast fix. Prefer removing probes with
an exact-text edit, or restore the file from backup and re-apply.

## Uninitialised circular-list heads in `sub_001A0B0C` (40 -> 43 kernel calls)

Three sites in `sub_001A0B0C` walk a circular list embedded at `ebx+0x180`:

```c
ecx = ebx + 0x180;            /* address of the list head */
eax = MEM32(ecx);             /* first link */
if (ecx == eax) goto skip;    /* head points at itself => empty */
edi = eax + -8;               /* else step back to the node header */
```

On real hardware an initialised-but-empty head **points at itself**, which the
original `CMP_EQ` catches. Here the head reads **0**, because whatever should
self-initialise it never ran - `ebx` is `0x00F81000`, the base of the 1MB block
from `NtAllocateVirtualMemory`, so the memory is merely zeroed. `eax = 0` then
gives `edi = 0xFFFFFFF8` and the dereference faults.

A zero head is logically the same thing as an empty list, so all three sites now
also treat `eax == 0` as empty. Narrow (one function, three sites) and
semantically faithful rather than a blind range check.

**40 -> 43 kernel calls**, stable across runs.

## Next blocker: failed ICALLs cascade into garbage objects

The crash moves to `sub_001F19F0`, on its very first instruction
(`edx = MEM32(ecx + 8)` with `ecx = 0xFFB79BE8`; `0xFFB79BE8 + 8` is exactly the
faulting address). It is called with a garbage `this`.

The mechanism is in `sub_002041D0`:

```c
RECOMP_ICALL_SAFE(MEM32(eax + 0x50), ...);  /* on failure: eax = 0 */
MEM32(eax + 0x2C) = MEM32(eax + 0x2C) - 1;  /* -> decrements VA 0x2C  */
eax = MEM32(eax + 0x30);                    /* -> reads VA 0x30       */
if (eax == 0) goto skip;                    /* garbage isn't 0, so no skip */
ecx = eax; sub_001F19F0();                  /* -> crash               */
```

The real x86 is the same shape - `call [eax+0x50]` legitimately clobbers `eax`
with the callee's return value, and the following `[eax+0x2C]` operates on it.
So this is **not** a lifter bug. The damage is entirely from
`RECOMP_ICALL_SAFE`'s failure path returning 0: the null check at `+0x30` was
written to catch a *legitimate* null, and reading VA `0x30` out of the fake TIB
produces a non-zero garbage pointer that sails past it.

### Failed ICALL targets fall into two distinct categories

Classified with `tools_data/whatis.py`:

- **Genuine missing functions.** `0x00227F50` disassembles cleanly and has the
  same shape as its neighbours (`call` then a run of argument pushes), i.e. it
  really is a function the pipeline failed to detect. An ICALL to it can never
  resolve. Note it is **not** in `tools/recomp/output/addable_functions.json`
  (120 entries) - none of the observed failing targets are - so that file does
  not cover this case.
- **Bad function pointers.** `0x00111600` resolves to `+0xE0 into sub_00111520`
  and disassembles to nonsense (`mov gs, esi` / `add byte ptr [eax], al`), so it
  is not even an instruction boundary. Nothing to add here; the pointer itself
  is garbage, which is a symptom of some earlier corruption.

Distinguishing the two matters: only the first is fixed by extending the
function database. Check any new failing target with `whatis.py` before assuming
it is a missing function.

## Regeneration risk: MEASURED (do not regenerate without reading this)

`gen/*.c` is generated but no longer purely generated - it carries **202
hand-written edits** (90 `Manual guard`, 60 `Manual addition`, 52 `Manual fix`)
plus 67 stub lines disabled in favour of `src/d3d8_shim.c`. Those files are
gitignored, so a regeneration destroys all of it.

The risk was measured non-destructively: `tools.recomp` was re-run with
`--gen-dir` pointing at a scratch directory, using the **existing**
`tools/disasm/output` and `tools/func_id/output`, so it reproduced exactly what
the live tree was generated from without touching anything.

### What survives a regeneration by itself

| fix class | live | after regen | note |
| --- | --- | --- | --- |
| lifter `_icall_target` fix | 38 | **17,146** | automatic, and *more thorough* - the committed `lifter.py` change applies it to every indirect call, not just the 37 esp-relative ones patched by script |
| dropped tail-calls | 51 | 0 | re-runnable via `fix_dropped_tailcalls.py` (but see its known blind spot above) |
| D3D stub disabling | 67 | 0 | re-runnable via `gen_d3d8_shim.py`, which reports the list |
| **hand-written guards** | **202** | **0** | **not reproducible by any script** |

So the systematic work is safe; the hand guards are the entire risk.

### Tool: `tools_data/manual_edits.py`

Extracts the hand edits into `manual_edits.json` and re-applies them after a
regeneration:

```sh
py -3 tools_data/manual_edits.py extract              # before regenerating
py -3 tools_data/manual_edits.py verify --gen-dir DIR # dry run, changes nothing
py -3 tools_data/manual_edits.py apply   --gen-dir DIR
```

Edits are anchored to the *following source line* (and, for wrapping guards, to
the enclosed lines) rather than to line numbers, so they survive the generator
moving code around. Anchor comparison is normalised, so an edit anchored to the
old `RECOMP_ICALL_SAFE(X, ...)` spelling still matches the new
`_icall_target = X; ... RECOMP_ICALL_SAFE(_icall_target, ...)` form.

**`apply` is atomic**: if any edit fails to place, nothing is written at all. A
partially restored tree still compiles but is silently missing guards, which is
far worse than an obvious failure.

**Current status: 146 of 209 recorded edits re-apply mechanically (70%).**

`if (...) { ... } else { ... }` guards ARE handled: the extractor splits them
into prefix / enclosed-generated-lines / suffix, and locates the site by
matching the whole enclosed run (a single anchor line such as
`PUSH32(esp, eax);` recurs many times in one function and picks the wrong
occurrence). Ambiguous matches are reported rather than guessed at.

The remaining **63 cannot be auto-applied**: those guards restructure code
*across a label boundary*, so the enclosed run does not exist verbatim in a
freshly generated tree. Handling them properly needs a C-aware differ, which is
more machinery than the payoff justifies. `verify` names every one of them, and
`manual_edits.json` records each one's exact text and location, so re-applying
them is mechanical hand work rather than archaeology.

### Regression guard

`extract` first audits every `/* Manual <word> (not in original x86)` marker in
gen/ against the `MARKERS` list and **fails with exit code 2** on an unknown
one. This is deliberate: the third marker wording (`Manual addition`) was
initially missing from the tool, and because unknown text is indistinguishable
from generator output, those 60 edits were being silently dropped - a
regeneration would have lost them with no error. If you introduce a new wording,
add it to `MARKERS`.

**Keep using the existing three wordings** rather than inventing new ones.

### Bugs found while building this (all fixed, worth not repeating)

- **Text-based idempotency is wrong.** Checking "does this comment already
  appear in the file" made every duplicate look already-applied: all 50
  dropped-tail-call fixes share one comment, so 49 were silently skipped. The
  check must be positional - does the block sit immediately before *this*
  anchor.
- **Blank lines are useless anchors** (not unique, and falsy in Python) - anchor
  on the next non-blank line.
- **Do not anchor one guard to another guard's comment.** Guards often sit back
  to back; in a freshly regenerated tree the neighbouring comment does not exist
  yet. Anchor on generator output only.

## Fourth systemic lifter bug: stale deferred flag tests (435 sites)

x86 computes flags with one instruction and branches on them later. The lifter
models this by deferring: it emits a placeholder where the flag-producing
instruction was, and re-evaluates the comparison **inline at the `jcc`**:

```c
(void)0;                    /* test cl, cl - flags set for next jcc */
ecx = esi;
if (TEST_NZ(LO8(ecx), LO8(ecx))) goto loc_002041F8;
```

That is only valid while nothing in between writes the tested register. At
`sub_002041D0` the real x86 is:

```
002041e0: test cl, cl        ; flags come from cl HERE
002041e2: mov  ecx, esi      ; ecx clobbered
002041e4: jne  0x2041f8      ; still branches on the pre-clobber flags
```

so the generated form re-read `LO8(ecx)` *after* `ecx = esi` and tested the low
byte of the **object pointer** instead of the flag byte. The branch could go the
wrong way - and did.

**What that caused.** Taking the wrong arm reached
`call [eax + 0x50]` where `eax = MEM32(esi)` was a **NULL vtable pointer**.
Because VA 0 is mapped (the fake TIB), `MEM32(0 + 0x50)` did not fault - it
silently returned garbage (`0x227E0068`), the ICALL failed on it, `eax` became
0, and `MEM32(0 + 0x30)` then produced `0xFFB79BE8`, which sailed past the
code's legitimate `if (eax == 0)` check and became a garbage `this` for
`sub_001F19F0`. Probe output confirming this exactly:

```
[DBG 2041D0] #1 vtable=0x00000000 target=[vt+0x50]=0x227E0068
[DBG 2041D0] #1 after icall eax=0x00000000 -> [eax+0x30]=0xFFB79BE8
```

Fixed at this one site by capturing the operand before the clobber, which is
what the hardware does:

```c
{ uint32_t _flag_cl = LO8(ecx);
  ecx = esi;
  if (TEST_NZ(_flag_cl, _flag_cl)) goto loc_002041F8; }
```

**43 -> 44 kernel calls**, stable across runs.

### Scale, and why the other 434 were NOT swept

`tools_data/find_stale_flag_tests.py` scans all 14,060 deferred-flag sites and
reports the **435** whose tested register is both assigned before the `jcc` and
still referenced by it. Roughly one site in 32.

Only this one is *proven* wrong - by disassembling the original and comparing.
The detector reports a **shape**, not a proven miscompile: a site is only truly
broken if the clobber actually changes the branch outcome, and some clobbers
will be benign. Given this document's repeated lesson that wide-scope sweeps
regress (61->44 and 58->57 both came from confident bulk edits), the remaining
434 are left alone pending per-site verification.

The proper fix is in the lifter: emit the comparison operands into temporaries
at the flag-producing instruction rather than re-reading registers at the `jcc`.
That would fix all 435 at once and survive regeneration - but it only reaches
the generated tree through a regeneration, so it is gated on the manual-edit
re-application story above.

### Related hazard: the fake TIB makes NULL dereferences silent

Worth stating on its own, because it changes how these bugs present. Xbox VA 0
is mapped (the fake TIB, needed for `fs:[0]` SEH emulation), so
`MEM32(0 + offset)` returns data instead of faulting. Every null-pointer
dereference therefore yields *plausible-looking garbage* rather than an
immediate, obvious crash - which is why so many failures in this log surface far
from their cause as "a garbage pointer appeared". When a garbage value has no
clear origin, check whether something read through a null base.

---

## Uninitialized bucket head in `sub_001A0B0C` (44 -> 47 kernel calls)

### The half-correct empty-list test

`loc_001A0B71` computes a bucket head and tests whether the list is empty:

```c
eax = esi + edi * 8 + 0x180;
if (CMP_EQ(MEM32(eax), eax)) goto loc_001A0C07;  /* empty */
eax = MEM32(eax + 4);   /* prev */
eax = eax - 8;          /* container_of */
SET_LO8(edx, MEM8(eax + 5));   /* faults */
```

A **properly initialised** empty head is self-linked: `next == head`, so the
test catches it. A head that was **never initialised** is zero-filled, so
`next == 0`, which is *not* equal to the head - the test falls through as if
the list had elements. `prev` is also 0, so `container_of` yields
`0 - 8 == 0xFFFFFFF8`, and the very next byte read faults at `0xFFFFFFFD`.

Confirmed by probe, not inference: `head = 0x00F811C0`, `prev = 0`.

The guard treats a head whose back pointer is not a plausible heap address as
an empty list. This is the **sixth** instance of this pattern in this one
function; the other five are guarded the same way.

### Two wrong turns worth recording

**1. Trusting the interpolated source line.** `triage_crash.py` estimates the
crash line by scaling the native function offset across the generated function's
line range. That put the fault near line 31214, and there *was* a matching
`MEM8(eax + 5)` there. A probe at that site produced **no output at all** - the
block is never reached. The estimate is a hint for narrowing a search, never
evidence. The tool's *expression* derivation (`fault == eax + 5`) is the
reliable part; the line number is not.

**2. Reasoning that a site was safe because a later line looked fine.** The
argument was "`eax` must be valid at line 30658, because `MEM8(eax + 5) = ...`
there does not fault". That is backwards: if the read at 30624 faults, control
never reaches 30658, so 30658 proves nothing. A line only constrains registers
on paths that actually execute it.

Both errors cost a build cycle each. Probe first.

### `triage_crash.py`

Added this round (Rule #4). It reads `stderr.txt` and reports the kernel-call
count, the owning function via the linker map, a region classification for every
register, and - the part that actually cracks these - which register the fault
address is a small offset from, optionally grepping the function for that exact
`MEM8/16/32(reg + off)` expression. On the next crash it named the single
faulting line unambiguously on the first try.

---

## `_icall_esp` captured across register saves in `sub_002235D0` (47 -> 53)

### A name string called as a function pointer

The crash was in `sub_001F7930`, a virtual dispatch on a NULL `this`. Probing
the two call sites showed caller 2 (`sub_002235D0`) passed a valid object five
times and NULL on the sixth. Just before that, two indirect calls failed on
targets `0x00219568` (mid-instruction) and `0x003FA710` - which `whatis.py`
identifies as **`.rdata` string data**, `"igNamedObject"` / `"igErrorHandler"`.
The garbage `edx = 0x63656A62` at the crash is `"bjec"` from that same string.

A *name string* was being called as a *function pointer*.

### Root cause

`sub_002235D0` registers an engine class descriptor and reads its arguments as
`MEM32(esp + 0x120 .. 0x13C)`. Its prologue was lifted as:

```c
{ uint32_t _icall_esp = g_esp;      /* <- captured too early */
PUSH32(esp, ebx);
PUSH32(esp, ebp);
PUSH32(esp, esi);
PUSH32(esp, edi);
PUSH32(esp, edx);                   /* the only real argument */
PUSH32(esp, 0); RECOMP_ICALL_SAFE(MEM32(eax + 0x58), _icall_esp);
}
```

`ebx/ebp/esi/edi` are callee-saved **register saves** - the epilogue pops all
four - not call arguments. `RECOMP_ICALL_SAFE` restores `g_esp` to the save
point on failure, so a failed call unwound the register saves too and left
`esp` 16 bytes high for the remainder of the function. Every subsequent
`MEM32(esp + 0x1XX)` argument read then landed four descriptor slots off,
handing the name-string pointer to a slot expecting a constructor.

Fix: capture `_icall_esp` *after* the four saves, immediately before the one
genuine argument push. Same defect previously fixed in `sub_00209650`; this is
the second confirmed instance, so it is a pattern rather than a one-off.

**47 -> 53 kernel calls**, stable across runs. Two side effects confirm the
diagnosis rather than merely moving the crash: `esp` at the next fault is now
`0x00F7F7C8`, inside the real stack range, where it had been `0x03FFFD10` (in
the *heap*); and failed indirect calls per run dropped from 21 to 13.

### Distinguishing a register save from an argument

Cheap and reliable: callee-saved registers on x86 are `ebx`, `ebp`, `esi`,
`edi`, and a function that saves them pops them in the epilogue. Caller-saved
(`eax`, `ecx`, `edx`) pushed immediately before a call are arguments. When in
doubt, check the epilogue for the matching pops.

### Tooling: resolving "who made this bad call"

`recomp_icall_fail_log()` already captured a native backtrace but printed it as
raw RVAs, which had to be resolved against `build/*.map` by hand every time.
`triage_crash.py --icall` now does that automatically and names the calling
`sub_XXXXXXXX` for every failure in the log. It pointed at `sub_002235D0`
immediately - no rebuild required, since the data was already being logged.

---

## The NULL-object crashes share one cause: missing static initializers

### The crash handler was blind

At 53 kernel calls the fault was reported inside
`__dyn_tls_init_callback + 0x6D6BD6D3` - an offset that *changed between runs*,
which is the tell that RIP was not in our image at all and the RVA was
meaningless. The native stack printed zero frames.

Two bugs in `veh_handler()`:

- The stack walk filtered frames against `0x140000000..0x150000000`, the PE
  header's **preferred** base. ASLR puts a 64-bit EXE near `0x7FF6'00000000`,
  so nothing ever matched and the stack was empty on every crash this project
  has ever had.
- The RVA was computed unconditionally against our module base, producing
  garbage whenever the fault was inside a system DLL.

Fixed by reading `SizeOfImage` from the loaded NT headers for a real module
range, naming the owning module via `GetModuleHandleExA` when RIP is foreign,
and scanning 256 stack slots instead of stopping at the first NULL. The very
next run said:

```
[CRASH] Access violation at RIP=0x7FF8414AC17B (NOT in this image - inside
        C:\WINDOWS\SYSTEM32\VCRUNTIME140.dll; ...)
  sub_00342AA0 + 0x6AA      <- the XBE's own memcpy
  sub_00204800 + 0x252
  ...
```

`triage_crash.py` now resolves that stack too, so a foreign-module crash names
its recompiled caller directly.

### What it found

`sub_00342AA0` is the XBE's `memcpy`, lifted from `rep movsd`. Probing all four
of its copy sites caught the fault: `dst=4, src=8, dwords=0x3FFFFFFF` - a 4 GB
copy from near-NULL, an array *erase* on an object whose storage pointer is 0.
A second probe on the array-insert path in `sub_00204800` showed the same:

```
[PROBE] 204829 obj=00000000 base=00000000 count=00000001 idx=00000000 ...
```

`obj` is the object itself. It is NULL, so `count` and `base` were read out of
the **fake TIB** at VA 0 and came back as plausible nonsense.

### The common root cause

Classifying every failed indirect call, **9 of 12** targets are real `.text`
addresses that were never recompiled - not garbage pointers:

```
0x00227F50  0x00340CDE  0x00340D86  0x00343862  0x00346743
0x00349FAB  0x0034AA86  0x0034BB3A  0x003556E0
```

Disassembling them shows clean function entries ending in `ret`, and several
are unmistakably **C++ static-initializer thunks**:

```
0x0034AA86: push 0x34AA40 ; call 0x19F0FF ; mov [0x5D9C1C], eax ; xor eax, eax ; ret
0x003556E0: mov ecx, 0x47C4F8 ; call 0x119E0 ; push 0x359D10 ; call 0x340DE6 ; ret
```

`RECOMP_ICALL_SAFE` sets `eax = 0` when it cannot resolve a target, so every one
of these calls silently returns NULL. That is where the NULL objects come from.

**The guards added one at a time in Phase A are all treating the same disease.**
Global constructors are not running because the functions that run them were
never recompiled. Phase A is now bottlenecked on Phase B (seed the missing
functions and regenerate).

### Tooling: `strip_probes.py`

Probes are added and removed several times per session; leaving one behind
slows every run and pollutes `stderr.txt`, and a greedy regex once ate an entire
function in this tree. `strip_probes.py` removes anything marked `/* PROBE */`,
is block-aware for multi-line probes, and refuses to write on an unterminated
block. Dry-run by default.

---

## Phase B: seeding the undiscovered functions (53 -> 48, deliberately)

### Discovery was the gap, not translation

Of the 26,505 functions in `tools/disasm/output/functions.json`, **26,504 have
generated C** (the exception is the XBE entry point, which `main.c` handles).
So when an indirect call fails on a *real* `.text` address, translation is not
at fault - discovery never found the function.

Function discovery works largely from direct call targets. A function whose
address is only ever taken as **data** is never the target of a `call rel32`
and so is invisible to it. `find_missing_functions.py` scans the data sections
for pointers into `.text`, discards anything already known or lying inside a
known function's body, and checks the bytes decode as code. Of the 12 failing
targets, 8 turned up immediately - and their referring addresses clustered:

```
0x0044A580:  00000000 00346743 00349FAB 00000000
             00000000 00345B25 003556E0 00355700
0x0044B0A0:  003B14C3 00000000 00000000 00340D86
             00343862 0034BB3A 0034AA86 00000000
```

Two NULL-terminated arrays of function pointers - the MSVC CRT initializer
tables (`__xi_a`/`__xc_a`). `_initterm` walks these at startup. Every entry the
recompiler had not discovered failed to resolve, and `RECOMP_ICALL_SAFE`
returns `eax = 0` on failure, so **the global constructors silently did
nothing**. That is the origin of the NULL objects behind most of this
project's crashes.

The ninth address, `0x00227F50`, has no data reference at all (it is reached
through a computed pointer) and was seeded by hand.

### Seeding is additive on purpose

`seed_missing_functions.py` computes each function's extent by disassembling to
its terminating `ret`, appends the entries to a copy of `functions.json`, runs
`tools.recomp -f` per function, and writes the results to a **new**
`gen/recomp_seed.c`. Registration goes through `recomp_lookup_manual()` in the
hand-written `src/recomp_manual.c` - the extension point `RECOMP_ICALL_SAFE`
already consults before the generated table.

Nothing existing is modified. The 214 hand guards are untouched, and the wiring
survives a future regeneration. A full regeneration would also have found these,
but would have zeroed every hand edit for a change that needed none of that.

### The result, stated honestly

**53 -> 48 kernel calls. That is a regression on the headline metric.**

The supporting signals all moved the other way:

- failed indirect calls per run: **12 -> 2**
- kernel ordinal 128 is reached for the first time
- the CRT initializer tables actually execute

The earlier 53 was reached by a program whose global objects were all NULL - it
was progress measured on garbage. The 48 is fewer calls on a far more correct
program. Kept deliberately, with the user's agreement, rather than reverted.

**Lesson for the metric itself:** kernel-call count is a proxy. When a change
alters *which* path the program takes, comparing counts across it is comparing
different programs. Rule #8's second signals are what make the call.

### Next blocker: SEH

The new crash is in `sub_003432FC`, which the register arithmetic identifies as
`_except_handler3`:

```
ecx = esi + esi*2          ; 3 * trylevel, for 12-byte scope table entries
eax = MEM32(edi + ecx*4 + 4)   ; the filter function
```

With `esi = 0xFFD9003C`, that computes `0xFE6159E4` - the observed fault
address exactly. `esi` is the trylevel, read from the exception registration
record `ebx = 0x0044A598` - which is in `.data`, where a registration record
should be **on the stack**. So an exception is being raised during startup and
the SEH emulation is dispatching it against a bogus registration frame.

### The 48-call crash: `_initterm`'s cursor is destroyed mid-walk

`sub_001A3554` is the CRT startup dispatcher. It walks two hardcoded tables -
`0x44B0A8..0x44B0BC` and `0x44A590..0x44B0A4` - calling every entry that is
neither 0 nor -1, with the cursor in `esi`.

Probing every dispatch shows the walk going wrong in one step:

```
[PROBE] initterm slot=0044B0B8 fn=0034AA86     <- fine
[PROBE] initterm  slot=0044A594 fn=00345B25     <- fine
[PROBE] initterm  slot=0044A598 fn=003556E0     <- fine, last real entry
[PROBE] initterm  slot=00F7FF10 fn=00000008     <- cursor is now on the STACK
[PROBE] initterm  slot=00F7FF2C fn=003432FC     <- calls _except_handler3
```

`esi` jumps from `.data` to the stack after `sub_003556E0` returns. `esi` is
callee-saved, so the cursor should have survived. Once it is pointing at the
stack, the loop calls whatever stack slots look like pointers - including the
`_except_handler3` address that `__SEH_prolog` stores in its registration
record, which is what made the earlier crash *look* like exception dispatch. It
was never an exception; it was `_initterm` walking the stack.

This is newly-exercised code: `sub_003556E0` is one of the seeded functions and
had never run before.

`sub_003556E0` itself lifts cleanly. It calls `sub_000119E0` (balanced
`push esi` / `pop esi`) and `sub_00340DE6`, which reaches `sub_00340DAE` - a
`__try` function using the SEH helper pair `__SEH_prolog` (`sub_003432A8`) and
`__SEH_epilog` (`sub_003432E3`). Those helpers are where `ebx/esi/edi` are
saved and restored, and they are the most intricate thing in the tree: the
prolog relocates the return address, rewrites the argument slot to hold the
caller's `ebp`, allocates the frame from an argument, and links a registration
record into the fake TIB at VA 0.

Reading them against the original disassembly, the lift looks structurally
faithful - the `[esp+0x10]` argument offsets, the `[ebp-16]` registration
record address and the pop order in the epilog all check out. So the clobber is
not obvious from inspection and the next step is a probe around the
prolog/epilog pair rather than more reading (Rule #5). If they do turn out to
be mis-lifted, `recomp_manual.c` is the place to fix them: hand-written
replacements for these two would be narrower and safer than trying to make the
lifter model a function that rewrites its own return address.

**Status: 48 kernel calls, stable, 2 failed indirect calls (from 12).**

---

## `__SEH_epilog` restored registers from a drifted esp (48 -> 54, 0 failed icalls)

### Tracing rather than reading

Reading `__SEH_prolog`/`__SEH_epilog` against the original disassembly had
suggested the lift was faithful - the `[esp+0x10]` argument offsets, the
`[ebp-16]` registration record address and the epilog's pop order all matched.
It was. The bug was not in *what* they do but in *when* the values are read.

Printing `esi` at entry and exit of every function in the chain settled it in
one run:

```
enter sub_000119E0 esi=0044A598   leave esi=0044A598   ok
enter sub_003432A8 esi=0044A598   leave esi=0044A598   ok   (__SEH_prolog)
enter sub_003432E3 esi=0044A598   leave esi=00F81000   CLOBBERED
```

### The bug

`__SEH_epilog` restores the callee-saved registers by popping:

```c
POP32(esp, ecx);   /* return address */
POP32(esp, edi);
POP32(esp, esi);
POP32(esp, ebx);
esp = ebp;
```

On real hardware that is correct: the function body leaves `esp` exactly where
`__SEH_prolog` left it, so the pops land on the save area. In this recomp `esp`
is simulated and drifts - measured **12 bytes** off at this call - so the pops
read four unrelated stack slots straight into `ebx`, `esi` and `edi`.

Nothing near the epilog looks wrong; the damage surfaces far away. Here it
landed in `_initterm`'s table cursor, which then walked the stack calling
whatever resembled a pointer - including the `_except_handler3` address stored
in a registration record, which is what made the whole thing look like
exception dispatch.

### The fix

`__SEH_prolog` already records the exact save-area address at `[ebp-24]`,
written right after pushing `ebx/esi/edi` and before pushing the return
address. So the layout is fixed and known:

```
[save-4] = return address
[save+0] = edi
[save+4] = esi
[save+8] = ebx
```

Restoring from there instead of from `esp` makes the epilog independent of any
esp drift in the body. Dropping the four `POP32`s does not change the final
`esp`, because `esp = ebp` on the next line overwrites it regardless - only the
restored *values* ever mattered.

**48 -> 54 kernel calls**, stable across two runs, and past the previous best
of 53. **Failed indirect calls went to zero** (12 at the start of this stretch,
2 after seeding). Every function pointer in the boot path now resolves.

### Why this was invisible until now

`sub_003556E0` - the initializer whose call chain reaches this epilog - is one
of the functions seeded in Phase B. The path had never executed before, so the
epilog had never been exercised with a drifted esp on a live cursor. Seeding
the missing functions is what made this bug reachable, and fixing it is what
turned the 53 -> 48 dip into 54.

### The general lesson

A lift can be *faithful to the instructions* and still wrong, because the
recomp's simulated `esp` is not the real `esp`. Any lifted code that reads
values relative to `esp` across a function body is exposed to drift. Where the
original stored an explicit frame or save-area pointer, prefer restoring
through that pointer - it is what the original author put there for exactly
this kind of recovery.

---

## The 54-call crash: runaway recursion in class registration

`esp = 0xFFFFFF10` at the fault - the simulated stack pointer has gone
*negative*, blowing through the bottom of the 8 MB Xbox stack. The crash
handler's stack walk shows a clean six-frame cycle repeating to the bottom:

```
sub_002235D0 -> sub_002226E0 -> sub_002221E0 -> sub_00209650 -> sub_002235D0 -> ...
```

`sub_002235D0` registers an engine class descriptor. It checks a cache slot,
and on a miss calls `sub_002226E0` to create the object, storing the result
afterwards:

```asm
0x0022368d: push eax
0x0022368e: call 0x2226e0
0x00223693: add esp, 4
0x00223696: mov dword ptr [ebx], eax     ; cache stored AFTER the call
```

Verified against the original disassembly - the lift is faithful, store-after-
call is what the game does. So the recursion must be broken by the *arguments*,
not by the cache discipline: registering class A recurses into its members, and
that hierarchy is finite on real hardware.

### The hand guard is innocent

Suspicion fell first on the hand-added plausibility guard on that cache check,
whose own comment guessed its root cause was "likely a caller argument-offset
mismatch upstream" - which is exactly the `_icall_esp` register-save bug fixed
earlier today, in this very function. Tempting, but Rule #5 says probe:

```
[GUARD] depth=1 ebx=005BC2FC cached=00000000 guard_takes=0 orig_takes=0
[GUARD] depth=2 ebx=005BC274 cached=00000000 guard_takes=0 orig_takes=0
...
[GUARD] depth=40 ebx=005BC274 cached=00000000 guard_takes=0 orig_takes=0
```

`guard_takes` and `orig_takes` agree at every depth, so the guard changes
nothing on this path - the original check would recurse identically. The cached
value is `0` every time because the recursion never returns to perform the
store. Left in place per Rule #10: it is inert here, and removing it would only
risk the case it was written for.

The next thread is upstream: `sub_00209650` iterates a child list, and one of
those children resolves back to the class being registered. That list is where
to look.

### `strip_probes.py` had a hole - now closed

The probe used above opened with `{ static int _d; ... {` on a line carrying no
`/* PROBE */` marker, only its closing line did. The stripper removed just the
marked line, leaving a dangling `{` and half a statement - and then printed
"no probes found - tree is clean". The build caught it, but the tool had
already lied.

Fixed with an invariant rather than a bigger parser: **whatever is removed must
be brace-balanced on its own.** If it is not, the removal would break the
surrounding code, so refuse to write and say why. Covered by a check in the
tool's own test path.

### Mapping the registration recursion (54 held)

The cycle, confirmed with a real frame walk rather than a raw stack scan:

```
sub_002235D0  (generic class registrar)
  -> sub_002226E0   ("ensure class B", hardcodes push 0x2221E0)
    -> sub_00209650 (calls the registrar it is handed, via `call [esp+8]`)
      -> sub_002221E0 (thunk: pushes 11 constants incl. slot 0x5BC274)
        -> sub_002235D0 ...
```

Probing the entry ICALL of `sub_00209650` shows the argument settling on
`0x002221E0` - class B's own registrar - while B is mid-registration:

```
[DEPFN] sub_00209650 arg=002225B0     <- first, a different class
[DEPFN] sub_00209650 arg=002221E0     <- then B, forever
[SLOT]  slot=005BC274 cached=00000000 <- never becomes non-zero
```

Termination should come from `sub_002235D0`'s cache check on slot `0x5BC274`,
but the slot is only written *after* the create call returns, and the create
call is what recurses.

### Four hypotheses killed by probing, not by reading

Worth recording because each looked convincing:

1. **The hand plausibility guard on the cache check.** Probed: `guard_takes`
   and `orig_takes` agree at every depth. Inert here. Left alone (Rule #10).
2. **`sub_00209650`'s dependency-array loop.** Probed: never entered, the count
   is <= 0. The recursion goes through the *entry* ICALL instead.
3. **`sub_00209650`'s two virtual calls.** Probed: neither fires.
4. **`cmp byte ptr [eax], 0` in `sub_002226E0` reading the fake TIB.** The
   theory was that `MEM32(0x5BC508)` is NULL, so byte 0 comes from the fake TIB
   (which holds `0xFFFFFFFF`) and the branch inverts. Probed: the pointer is a
   real heap object `0x00F81288` whose first dword is a vtable `0x003F4770`, so
   byte 0 is `0x70` legitimately. Disassembling confirmed the lift is faithful.
   The branch behaves exactly as on hardware.

The stack walk itself misled the first pass: the old raw-slot scan reported
`sub_00209650` and friends as callers, and they *were* on the stack - as stale
values. Fixed by preferring `CaptureStackBackTrace`, keeping the raw scan below
it under an explicit "may include stale return addresses" heading.

Remaining question: what sets slot `0x5BC274` before the recursion closes.
The only other writer is `sub_001F3210`, a specialised registrar for the same
class that calls `sub_002226E0` and stores the result directly. Whether that
path is meant to run first is the next thing to establish.

### Tooling: `add_probe.py`

Probes were being pasted through shell heredocs, and the shell mangles
backslash escapes, so `\n` inside a C string arrived as a real newline and the
file stopped compiling - **four times in one day**. Twice is a class (Rule #9).

`add_probe.py` builds the probe in Python, where the escaping is correct by
construction, enforces the `/* PROBE */` convention that `strip_probes.py`
depends on, checks that the `--fmt` conversions match the `--args` count, and
refuses to write unless the anchor line matches exactly once.

```
py -3 tools_data/add_probe.py src/recomp/gen/recomp_0015.c \
    --after "loc_00209650: ;" --tag DEPFN \
    --fmt "arg=%08X" --args "MEM32(esp + 4)" --limit 12
```

### Breaking the cycle instead of explaining it

After four eliminated hypotheses the root cause was still open, so the cycle was
cut rather than understood further.

`sub_00209650` already had a clean early exit: before the missing functions were
seeded, its entry ICALL failed to resolve, `RECOMP_ICALL_SAFE` set `eax = 0`,
and the `if (!edi)` test below took a balanced return. The guard reproduces
exactly that path when a registrar is already active further up the stack, via
`recomp_reg_enter()` / `recomp_reg_leave()` in `recomp_manual.c` - hand-written,
so it survives regeneration.

**Result: the recursion is gone.** The crash stack is now a clean 13-frame
chain from `sub_0019F22E` down, with no repetition, and `sub_002235D0` reaches
**+0xCDE** instead of +0xC0 - far deeper into the registration work.

But it is a trade, not a win:

| | recursion crash | with the guard |
|---|---|---|
| kernel calls | 54 | 54 |
| failed indirect calls | 0 | 5 |
| crash | stack overflow, esp negative | NULL object in `sub_00221F50` |

Skipping a registrar leaves that class unregistered, so its object is NULL and
the crash simply moves. Same wall, different brick.

### What the evidence actually points at

`sub_001F3210` is a **specialised** registrar for the same class, and the only
code anywhere that writes slot `0x5BC274` directly. If it ran first, the generic
registrar would find the slot populated and take its "already cached" branch -
and the cycle would never form. It has five direct callers.

Probed: **it never runs.** Zero hits.

So the shape of the real fix is clear - get `sub_001F3210` to run at the right
time - even though the trigger has not been found yet. That is a much smaller
question than "why does this recurse", and a better place to resume than more
tracing of the cycle itself.

---

## Stubbing the class registrar, and what it revealed about the metric

### Every narrow workaround made things worse

Three attempts to keep the registration subsystem and break only the cycle:

| attempt | result |
|---|---|
| re-entrancy guard in `sub_00209650` | stack overflow becomes a NULL-object crash |
| also suppress storing a NULL class object | crash becomes a 312M-iteration spin loop |
| un-register the seeded `0x00227F50` | no change at all - the cycle enters elsewhere |

Also checked and ruled out: both cache slots (`0x5BC274`, `0x5BC2FC`) are **BSS,
zero-init, no file data**, so they start at 0 on real hardware too. There is no
static initial value we are failing to load. Something registers these classes
in an order that has not been found.

### Skipping the subsystem is what actually moved the boot

`sub_002235D0` now returns immediately. The call chain at the crash tells the
story:

```
before                          after
  sub_002235D0 + 0xC0             sub_00123600 + 0x14
  sub_002221E0 + 0x173            sub_0022CC40 + 0x269     <- new
  sub_00209650 + 0xF1             sub_002392E0 + 0xBD1     <- new
  sub_002226E0 + 0x7E             sub_00239E50 + 0x30D     <- was +0x19F
                                  sub_00011E40 + 0x1FE
                                  sub_001A016A + 0xED
                                  sub_0019F22E + 0x2A4
```

The top-level init `sub_00239E50` advances from **+0x19F to +0x30D**, and two
entirely new frames appear below it. A large amount of startup that was
unreachable now runs.

### The kernel-call metric is blind in this region

**Every one of those attempts measured 54 kernel calls.** Crash, different
crash, spin loop, and real forward progress all scored identically, because
this whole stretch of initialisation makes no kernel calls at all.

That is the sharpest illustration yet of why Rule #1 says the count is a proxy.
In this phase the useful signals are:

- **how deep the top-level init gets** - `sub_00239E50 + N` is a direct measure
- **which frames appear at all** - a new frame means new code executed
- failed indirect calls, and whether the run crashes or hangs

`progress.py` already records `crash_in`, so the depth is captured per entry.
Read it alongside the count rather than reading the count alone.

### Status of the stub

It is a scope decision, not a fix, and it is marked as such in the source. The
registry will hand out NULLs to anything that queries it, and that is expected
to surface as its own crash later. Revisit once the registration order is
understood - the open question is narrow: what registers these classes first,
given `sub_001F3210` (the only direct writer of `0x5BC274`) never runs.

---

## The full regeneration (70 held, behaviour-neutral)

### Why bother

The lifter's esp-relative ICALL fix existed but only reached the tree through
38 hand-patched sites. A regeneration applies it everywhere:
**74 -> 34,292 `_icall_target` mentions**, i.e. 17,146 call sites. Both of the
day's largest wins (the `_icall_esp` register-save bug, the `__SEH_epilog` esp
drift) were esp-accounting bugs, so the latent population was worth clearing.

The regeneration also folded in the nine seeded functions natively - it was run
against `seeded_functions.json`, so `recomp_dispatch.c` now maps **26,514** VAs
rather than 26,505, and `gen/recomp_seed.c` is obsolete.

### The result

**70 kernel calls across two runs, same crash site, failed indirect calls
24 -> 23.** Behaviour-neutral, which is the right outcome for a migration: the
value is the latent fixes, not an immediate jump.

### What the migration actually cost, and what now automates it

`manual_edits.py apply` placed 153 of 222 on the first attempt. Every fix below
came from a hand repair that was then fed back into a tool, so the next
regeneration is mechanical:

| symptom | cause | now handled by |
|---|---|---|
| 69 edits "enclosed lines not found" | the fresh tree emits a label + blank line inside the run; the recorded run has neither | `manual_edits.py` skips label lines when matching, and refuses to wrap across a label that is still a `goto` target |
| nothing written at all | apply is all-or-nothing by design | `--partial` (file-atomic) plus `--force` |
| file written with `+2` unmatched braces | a wrap placed its opening but not its close | `check-braces`, and `repair_wraps.py` |
| unclosed guard, no recorded wrap to close it | the enclosed run moved too far | `repair_wraps.py --drop-unclosed` - drops the guard back to generated code, a known logged loss |
| `+1` from a duplicated block opening | the guard's recorded block carries `{ uint32_t _icall_esp = g_esp;`, so re-applying leaves the generated copy orphaned | `repair_wraps.py` detects and removes the orphan |
| a note inserted *inside* an existing comment | `--fix` assumed the N lines after the save point were the pushes | `find_icall_esp_saves.py --fix` now finds the Nth actual `PUSH32` |
| `LNK2005 already defined` ×5 | hand-written overrides in `recomp_manual.c` collide with regenerated bodies; the old tree had deleted those bodies, and `replace_line` can only restore a stub it can already find | `stub_overridden.py`, which reads the override sources themselves so adding an override needs no bookkeeping |
| `_guard_iter` undeclared | a guard's counter declaration is a separate edit that did not re-apply | hand-fixed; the pattern is "manual block references an identifier no longer declared" |

### Two known losses

`repair_wraps.py --drop-unclosed` removed one guard each from `sub_002041D0`
and `sub_0020E547`. Both were pointer-plausibility guards. If they mattered
they will resurface as their own crashes, which is preferable to a tree that
does not compile - and far preferable to a hand "repair" that silently deletes
a generated line, which happened once here (a `ecx = esi` thiscall setup) and
had to be caught by diffing against the pristine regeneration.

### The lesson worth keeping

Every one of those tool fixes was written **after** hitting the problem by
hand - which is Rule #15 working as intended, and also a reminder that the
first regeneration of a heavily hand-edited tree is going to be expensive no
matter what. The second should not be.

---

## 82 kernel calls, and why guards stopped helping

### The jump to 82 came from a migration artifact, not a new guard

`repair_wraps.py --dedup-guarded` found four sites where a guard that
*replaces* a statement had been re-applied as an *insert*:

```c
    MEM32(esi + ecx * 4) = edi;        <- the original, still running
    /* Manual guard ... */
    if (ecx < 0x40u) MEM32(esi + ecx * 4) = edi;
```

The guard is plainly visible in the source and completely dead - the unguarded
line above runs first and faults exactly as it did before the guard existed.
Removing the four originals: **70 -> 82**.

This is the nastiest failure mode of the whole migration, because reading the
source tells you the guard is present.

### Then two guards in a row made things worse

| attempt | result |
|---|---|
| guard `sub_0013AE50` at entry on its `this` | 82 -> **74** |
| guard only the faulting block inside it | 82 -> **70** |

Both reverted immediately (Rule #14). Skipping work in that function loses side
effects later code depends on - the object is bad, but the work is not optional.

### The reason guards stopped working

`esp` is **outside the simulated stack** at the crash - `0x005023D8`, in
`.data`. The kernel bridge logs `esp` on every call, so the moment it escapes
was already in the log and just was never checked:

```
#70  esp=0x00F7FC08          <- valid stack
#71  esp=0x00545F64          <- .data
```

Between those two calls the log shows a spin loop: **2,557 indirect calls**,
1,000 of them failing, on targets `0x00000004` and `0x3F800000` - the latter
being the float `1.0f` yet again. Resolved, the loop sits in `sub_0013AC10`,
called from `sub_0013AE50 + 0x19E`.

`esp` falls ~10 MB, which is more than the 8 MB stack, so it runs off the
bottom and into `.data`. Every frame after that is fiction, which is exactly
why guarding downstream functions made things worse rather than better: they
were being handed a corrupt frame, and skipping their work removed side effects
without fixing anything.

**`triage_crash.py` now reports this automatically** as `STACK ESCAPE`, naming
the kernel call where `esp` leaves the region, so the next occurrence does not
need re-deriving with awk.

### The next target

Not another pointer guard. Find why the loop around `sub_0013AC10` runs 2,557
times and leaks a frame each pass. `sub_0013AC10` already carries a guard for
the recursion case, and its push/pop pairs look balanced, so the leak is either
in a callee or in the caller's loop - and `0x3F800000` appearing as both a call
target and an array index says something upstream is handing out a float where
a pointer or index belongs.
