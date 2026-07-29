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

## Current issue: genuine multi-function recursion (not yet fixed)

At kernel call #57 the process now hits a **stack overflow**
(`STATUS_STACK_OVERFLOW`, `0xC00000FD`) instead of a clean crash -
`smoke_test.ps1` flags this by signature regardless of how far kernel
calls got, since it's the same exception code as the earlier (already
fixed) CRT lock-bootstrap recursion bug. **This is a different bug that
happens to produce the same exception code**, not a regression of the
old one - the call chain is entirely different code.

ICALL trace immediately before the crash shows repeated `[ICALL] Failed
to resolve VA 0xFFFFFFFF` (a `-1`/"not found" sentinel being called as
if it were a function pointer) with an **identical repeating call chain**
across consecutive failures:

```
sub_0013A6A0 -> sub_0013AC10 -> sub_0013AE50 -> sub_0013B0E0 ->
sub_0013B220 -> sub_00145E60 -> sub_001463F0 -> (back into sub_0013A6A0)
```

This is genuine **mutual recursion across 7 functions**, not simple
self-recursion like the earlier lock-bootstrap bug - a different, harder
class of problem (no single obvious "always acquire this before creating
it" fix; likely a tree/graph traversal that revisits a node it should
have already marked visited, or a terminator condition that depends on
another D3D-null value). Not yet root-caused. The spin-loop probe
technique (recomp_icall_fail_log's Nth-failure stack trace, already
built) caught this cleanly; next step is resolving why VA `0xFFFFFFFF`
specifically is being called (probably a `-1` "not found" return value
from something upstream being used as a function pointer without a
sentinel check, similar in spirit to the `sub_0011E7A0` fix above but in
a different subsystem) and finding where the cycle should have
terminated but doesn't.
