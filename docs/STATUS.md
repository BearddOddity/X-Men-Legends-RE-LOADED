# Where this port stands

*29 August 2026. Figures from a deterministic two-of-two run of the current build.*

**28,318 functions are translated to C. 415 call sites execute.**

That ratio is the whole picture. Translation was never the bottleneck and has
not been for some time. The game dies inside its own C runtime's static
initialisers, before the game proper starts, so nearly the entire binary has
been converted to C and almost none of it has ever run. Every metric this
project tracks — kernel calls, call sites, reached VAs — measures how far into
startup execution survives, not how much has been ported.

**On the numbers.** 415 is distinct *direct* call sites executed
(`g_callsite_count`, marked by `RECOMP_MARK_SITE` in `RECOMP_ABI_CALL`). The
separate "reached VAs" counter is 146, and it counts only functions entered
through an *indirect* call, because `recomp_mark_reached` is called nowhere but
the three icall macros. Neither is a clean count of functions executed. An
earlier revision of this document presented the 146 as though it were the
latter; it is not, and the crash site itself does not appear in that list
despite obviously executing.

## Three phases

| | phase | state |
|---|---|---|
| 1 | Understand the binary | done |
| 2 | Translate x86 to C | done |
| 3 | Make it boot | **in progress** |

**Phase 1.** 26,505 functions identified, 5,932 named against the 714 the
disassembler found alone — nearly all of the gain from walking MSVC RTTI to
vtables, which Ghidra never does on an XBE. Cross-referencing five related
builds split the binary into platform, engine and game code, which shrinks the
real surface from 26,505 functions to roughly 6,400. The rest is XDK, CRT and
D3D: replaced, not ported. See [FUNCTION_CLASSIFICATION.md](FUNCTION_CLASSIFICATION.md)
and [GAME_FUNCTIONS.md](GAME_FUNCTIONS.md).

**Phase 2.** 28,318 function bodies, plus ~29,000 lines of host-side code
standing in for the console — D3D, the NV2A GPU, the APU, audio, input, kernel.
None of it is stub code. Functions the disassembler misses can be seeded
additively without regenerating the tree.

**Phase 3.** The frontier. Because the boot dies early, those 29,000 lines of
graphics and audio have never faced a real workload; that is a second project.
Current work is one wall at a time.

## This session

The wall moved for the first time in a long while.

| | before | after |
|---|---|---|
| crash site | `sub_001FBA90+0x76` | `sub_002096B0+0xd5` |
| kernel calls | 226 | **434** |
| seeded functions | 1,188 | 1,667 |
| call sites | 437 | 415 |
| reached VAs | 159 | 146 |
| distinct kernel fns | 17 | 17 |
| heap allocations | 95 | 92 |

`sub_002096B0` is a different function entirely, and one ledger #143 examined
and explicitly ruled out.

**Not everything went up.** Coverage fell while kernel calls nearly doubled, and
*distinct* kernel functions held flat at 17 rather than rising. There is direct
precedent for a deliberate dip — ledger Phase B went 53 → 48 on purpose once the
higher number turned out to be progress measured on garbage. Taking a more
correct path earlier means paths that only ran on wrong state stop running.
Recorded as mixed: 208 more kernel calls through the same 17 functions could be
real work, or could be a loop.

## The dominant defect class

A recompiler discovers functions by following calls from an entry point. A
function whose only reference is a pointer in a table — a vtable, an initialiser
list, a factory array — is never reached, never translated, and whatever it was
meant to set up stays NULL for the whole run. The failure then surfaces
arbitrarily far from its cause.

| measurement | count |
|---|---|
| globals in the registry region that translated code reads | 1,770 |
| of those, never written by any translated code | 613 |
| which have a writer in the binary, never translated | ~500 |
| function pointers reachable only from data tables | 512 |
| orphan instructions recovered by an earlier pass | 44,926 |

Every wall examined has been an instance: a subsystem registry nothing writes
([BLOCKER_005BB700.md](BLOCKER_005BB700.md)), a type table left NULL, an object
that is really the integer `4` because a lookup against an empty registry
returned nothing ([PAGE_ZERO_CENSUS.md](PAGE_ZERO_CENSUS.md)).

**Strategic read:** anything that converts data-referenced code into translated
code has outsized leverage over fixing individual functions. Two passes of that
shape — 609 orphan functions, then 512 data pointers — have each produced more
movement than any single-function fix in this project's history.

## Next

1. **Resolve the new wall.** `sub_002096B0+0xd5`, faulting on a *write* to
   `0x10000FFFF` — one byte past the 4 GB boundary, suggesting a 32-bit address
   wrapping rather than the NULL dereferences behind every previous wall. A
   different failure mode.
2. **Account for the flat 17.** Establish whether 434 kernel calls through 17
   functions is real progress or a loop, before treating it as a gain.
3. **Keep converting data references into code.** The class is not exhausted:
   15 candidates were skipped for having no terminating `ret`, and tail-call
   paths remain uninstrumented by either ABI checker.

## The boot chain, mapped

Decompiling the wall traced the path from the entry point to the crash.

| step | what it is | result |
|---|---|---|
| `0x001A1C97` | XBE entry point | runs |
| `sub_0019F22E` | `CreateThread(NULL, 0, 0x1A1C23, ...)` | runs |
| `PsCreateSystemThreadEx` | bridged; runs the body inline | runs |
| `0x0019F196` | thread wrapper (TLS copy), passed `ctx1 = 0x1A1C23` | not lifted |
| `0x001A1C23` | CRT startup — **hand-written** in `recomp_manual.c` | runs, 4 steps |
| `sub_00011E40` | **static initialisers** | **entered, never returns** |
| `sub_002096B0` | crash | write to guest `0xFFFFFFFF` |

`[MAINLOOP] enter #1` appears once in the log and `RETURNED` never does — so
`sub_00011E40` is entered and dies inside.

`0x001A1C23` is never the target of a `call` instruction anywhere in the binary.
Its only reference is `push 0x1a1c23` as the `lpStartAddress` argument to
`CreateThread`. It executes at all only because someone hand-wrote it into
`recomp_manual.c`. That is the same indirect-dispatch gap as everything else,
sitting on the single most important path in the binary.

### Why so many globals are NULL

The crash needs two faults at once, and both trace to the same place:

1. `[0x5bc508]` is NULL, so `mov ecx, [eax+0x394]` reads guest `0x394` — the
   mapped null page — and silently yields `0` instead of faulting.
2. `this` arrives as `0xFFFFFFFF`. The caller at `sub_0020E547` computes
   `edi = <virtual call result> + [esi+0x20]` and guards with `je` — a
   **zero-only** test. A failed icall returns `0`, `[esi+0x20]` is `-1`, so
   `edi = -1`, which is non-zero and sails through the guard.

`0x5bc508` holds a 928-byte singleton built by `sub_00239E50` — a refcounted
constructor (`add ecx,1 / adc edx,0`, `or eax,esi`, `push 0x3a0`, `operator
new`, construct, store). It is invoked from `0x00011E95`, *inside* the static
initialisers, after the point where execution dies.

So the ~613 uninitialised globals are not 613 separate missing writers. Many
share one cause: the initialiser chain that would fill them stops partway
through. Finding where inside `sub_00011E40` it dies is now the single
highest-value question in the project.


## The fatal call, isolated

A per-call bisect of `sub_00011E40` put the death in initialiser 3,
`sub_00239E50`. Probing inside it isolated the exact call. `sub_0020E547` calls
`sub_002096B0` eleven times; the first ten are healthy and the eleventh is the
crash:

```
[LASTTHIS] this=01098650 adjust=00000000 esi=01098550 registry=01086000   ok
[LASTTHIS] this=FFFFFFFF adjust=FFFFFFFF esi=01098358 registry=01086000   crash
```

Two faults combine, and neither is the one previously assumed:

1. **`[esi+0x20]` is `0xFFFFFFFF`** on object `0x01098358`. Every other object
   passing through this path has `0`. That field is an adjust/offset the
   registration assigns; `-1` is the unassigned sentinel.
2. **The virtual call through `[edx+0xCC]` returns `0`**, so
   `this = 0 + (-1) = -1`.

The caller guards with `je` — a **zero-only** test. A guard written to catch
null does not catch a sentinel, so `-1` passes and is dereferenced.

### Three claims in this document were wrong

Recorded rather than quietly edited, because each looked solid at the time:

- *"`operator new` for the registry fails."* It does not. Probe `[REGALLOC]`
  shows `operator_new(0x3A0)` returning `0x01086000`.
- *"The store to `0x5BC508` never executes."* It does. The store sits at
  `loc_00239EA8`, **before** the registration calls, not after them.
- *"`0x5BC508` is NULL at the crash."* It is not — `registry=01086000` on all
  eleven calls. The `eax=0` in the crash register dump is the *value being
  stored* by `mov [esi+ecx], eax`, not the registry pointer. Misreading one
  register produced two follow-on conclusions.

The registry is built correctly. What is missing is the assignment of one
object's adjust field, which is a registration that has not run — the same
class, one level further in.
