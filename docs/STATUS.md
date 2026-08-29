# Where this port stands

*29 August 2026. Figures from a deterministic two-of-two run of the current build.*

**28,318 functions are translated to C. 146 of them execute.**

That ratio — 0.52% — is the whole picture. Translation was never the
bottleneck and has not been for some time. The game dies during early
initialisation, so nearly the entire binary has been converted to C and almost
none of it has ever run. Every metric this project tracks — kernel calls, call
sites, reached VAs — measures how far into startup the boot survives, not how
much has been ported.

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
