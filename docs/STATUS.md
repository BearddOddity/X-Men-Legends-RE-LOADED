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


## What the -1 actually is

Two candidate explanations, both testable, and the probes killed the one I
favoured.

**Not register corruption.** The descriptor constructor `sub_00216EE0` opens
with `xor ebx, ebx` at `0x00216EE1` and writes `[esi+0x20] = ebx` at
`0x00216F32`. If that constructor runs, the field is **always** 0 — a clobbered
`ebx` cannot put `-1` there, whatever else the callee-saved bug is doing
elsewhere.

**Not a failed virtual call either.** The slot at `[vtable+0xCC]` resolves to
`0x001EC5E0`, and that function *is* lifted — it is one of the 512
data-referenced pointers seeded earlier the same day. The icall succeeds, so its
`0` return is legitimate behaviour, and `adjust = -1` is the sole defect.

**It is an unfinished object.** Three descriptors sharing vtable `0x003F5D88`
pass through this path:

| descriptor | +0x20 | +0x8 | +0x1c | +0x28 | state |
|---|---|---|---|---|---|
| `01091AB0` | `00000000` | `00000000` | `011D5008` | `01096B88` | fully constructed |
| `01098550` | `00000000` | `FFFFFFFF` | `0121D008` | `00000000` | partial |
| `01098358` | `FFFFFFFF` | `FFFFFFFF` | `00000000` | `00000000` | barely started |

The full constructor sets `+8` to zero. Both failing descriptors carry
`FFFFFFFF` there, so it ran on **neither** — they came through the minimal path
at `0x002222B5`, which sets only `+0x1c` and `+0x60` and never touches `+0x20`.
The `0` on `01098550` and the `-1` on `01098358` are both heap residue; one
happened to land on zeroed memory.

So the fatal object is not corrupted. It is **unfinished** — the initialisation
that would have filled `+0x1c`, `+0x20` and `+0x28` never ran. Same class as
every other wall, one level further in, and it is why a guard written for null
does not save the caller: an unfinished object holds whatever the allocator
last left there, and that is rarely zero.


## Where the search stands, including what does not add up

Chasing what should have assigned the descriptor's fields narrowed the question
a long way and then hit a genuine contradiction, recorded here rather than
resolved by guesswork.

**Established.** The full descriptor constructor `sub_00216EE0` runs on exactly
eight descriptors in this boot:

```
00F7FD98  01091AB0  01096BB8  01097498  01097518  01097728  010977C0  01097858
```

The two failing descriptors, `01098358` and `01098550`, are **not among them**,
and both sit at higher addresses — allocated after construction stopped. That
matches their `+8 = FFFFFFFF`, since the constructor writes `0` there.

**Also established: the `-1` is never written.** All 40 dword writes of
`0xFFFFFFFF` to a `+0x20` offset in `.text` target `[esp + 0x20]` — SEH scope
slots, not object fields. So `-1` at `[desc+0x20]` is residue in memory that was
never written, not a sentinel some code set.

**The contradiction.** Only three instructions in the binary write the vtable
`0x003F5D88`:

| site | in | reaches the ctor? |
|---|---|---|
| `0x00216EFC` | `sub_00216EE0` — the constructor itself | is the ctor |
| `0x002222B5` | `sub_0022229A` | never runs — see below |
| `0x00222729` | `sub_00222708` | yes, unconditionally |

`sub_0022229A` is reachable only as the `je` target inside `sub_00222270`, and a
probe on `sub_00222270`'s entry got **zero hits** this run. `sub_00222708` sets
the vtable and then reaches `call 0x216ee0` with no branch in between:

```
00222729  mov [esi], 0x3f5d88
0022272F  mov dword ptr [esi+0x1c], 0
00222736  mov ecx, esi
00222738  mov dword ptr [esi+0x60], 0
0022273F  call 0x216ee0          <- unconditional
```

So no object should be able to carry that vtable without the constructor having
run on it — and two demonstrably do. Something outside these three paths is
producing them. A memcpy or clone of an already-constructed descriptor is the
leading candidate, since it would copy the vtable while carrying the source's
later field values, but it is untested.

Theories discarded along the way, each by measurement rather than argument: the
allocation failing, the store to `0x5BC508` being skipped, the registry being
null, register corruption writing the `-1`, a failed virtual call, and a
registry "ready" flag gating a minimal construction path.


## The contradiction, confirmed by paired probes

Probing object *creation* and object *construction* in the same run settles it.
`[NEWDESC]` fires where `sub_00222708` has just allocated, `[CTORON]` on entry
to `sub_00216EE0`:

```
created     : 01091AB0 01096BB8 01097498 01097518 01097728 010977C0 01097858
constructed : the same seven, plus 00F7FD98 (a stack object from sub_00219A10)
```

Seven created, seven constructed — that path is airtight. **`01098358` and
`01098550` appear in neither list.** They carry vtable `0x003F5D88` without ever
passing through an instruction that writes it, and the third writer,
`sub_0022229A`, runs zero times.

Two hypotheses remain, both untested:

1. **A clone.** A `memcpy` of a constructed descriptor would copy the vtable
   while carrying whatever the source held in the later fields.
2. **An embedded copy.** `esi` may point into a larger structure that contains a
   descriptor by value, in which case these are not standalone objects at all.

The next probe is the allocator: catch the block containing `0x01098358` at
birth with a native backtrace, which names whoever produces it.

### The boot chain, from one backtrace

The `--where` probe gave the whole path in a single frame list, which is worth
recording as the canonical route from entry point to the wall:

```
sub_001A1C97   XBE entry point
sub_0019F22E   CreateThread
sub_0019F196   thread wrapper          (hand-written in recomp_manual.c)
sub_001A1C23   CRT startup             (hand-written in recomp_manual.c)
sub_00011E40   static initialisers
sub_00239E50   registry singleton      (initialiser 3 of 11)
sub_0023666F
sub_002366BC
sub_00209650   container walker
sub_002221E0
sub_002235D0   registration driver
sub_00222708   descriptor constructor
```

**Correction.** An earlier note here called `sub_0019F196` "not lifted". It is
hand-written in `recomp_manual.c` as `static void` and registered in
`recomp_lookup_manual`; a grep for `^void` missed the `static`. It runs, as this
backtrace shows. The substantive point stands and is if anything stronger: two
of the twelve frames on the critical path exist only because someone wrote them
by hand, because nothing in the binary calls them directly.


## Origin found: the object is never constructed, it is returned

`sub_002226E0` is a lookup-or-create, gated on the registry's ready flag at
`[0x5BC508]+0`:

```
002226E0  mov eax, [0x5bc508]
002226E5  cmp byte ptr [eax], 0
002226E8  je  0x222708          ; flag == 0  -> CREATE (full constructor)
002226EA  ...                   ; flag != 0  -> LOOK UP via sub_0020E520
```

Probed across 13 calls: **7 with flag `00`** — exactly matching the 7 objects
`sub_00222708` builds — and **6 with flag `01`**, once the table at `0x5BC274`
is populated. The flag flips partway through startup, which is why the early
descriptors are healthy and the late ones are not.

The lookup path is where the bad object comes from:

```
0020E521  mov esi, ecx           ; esi = descriptor
0020E523  mov eax, [esi + 0x3c]  ; forwarding pointer
0020E530  call eax               ; follow it - INDIRECT
0020E532  mov esi, eax           ; esi = whatever it returned
0020E539  jne 0x20e530           ; loop while +0x3c is set
0020E53B  cmp byte [esi+0x1a], 1
0020E53F  jne 0x20e547           ; -> the crash path
```

`esi` becomes the **return value of an indirect call**, not an allocation. That
is why probes at both the constructor's entry and its vtable store list eight
objects and exclude the failing pair: the fatal descriptor was never constructed
here at all. It is produced by whatever `[desc+0x3c]` points at.

It also explains a loose end: `sub_0020E547`, the crash caller that appeared to
have zero callers anywhere in the binary, is simply the `jne` target inside
`sub_0020E520` at `0x0020E53F`, taken when `byte [esi+0x1a] != 1`.

### Discarded this round

- **The memcpy clone.** The only bulk copy in the registration chain is at
  `0x0022367D`, and reading it shows a `strcat` over a stack buffer — it
  measures a null-terminated string and appends it. Not a descriptor copy.
- **The registry ready-flag gating a *minimal* constructor.** The minimal path
  `sub_0022229A` runs zero times; the flag instead selects between create and
  look-up.

A useful discriminator fell out: every constructor-stamped object has
`+4 = 00000001`, while both failing objects have `+4 = 18000001`. The
constructor computes `+4 = (old & 0xFF000001) | 1`, so their prior top byte was
`0x18` — a value none of the eight constructed objects ever held. Whatever
produces them writes that field first.

**Next:** capture the target of `call [esi+0x3c]` at `0x0020E530`. That function
is the last unknown in the chain, and it is what hands back an object carrying
`-1` in its adjust field.


## Correction: `+0x20` is an allocation prefix, and the -1 is uninitialised

A previous section of this document identified `[desc+0x20]` as a base-class
displacement and read `-1` as MSVC RTTI's "not present" sentinel, on the
strength of the field being added in one function and subtracted in another.
**That was wrong**, and Ghidra settled it in two queries once the lab was up.

The decompiler shows the field added to an allocation **size**, which a base
displacement never is:

```c
/* FUN_0020e520 - create an instance of the type described by `this` */
iVar3 = *(int *)((int)this + 0x20);                                  /* prefix */
iVar4 = (**(code **)(*param_1 + 0xcc))(*(int *)((int)this + 0x48) + iVar3);
this_00 = (void *)(iVar4 + iVar3);                                   /* skip it */
if (this_00 != (void *)0x0) {
    FUN_002096b0(this_00, (int)this);                                /* init + register */
}
```

and subtracted again before the block is freed, in `free_object_instance`:

```c
param_1 = (int *)((int)param_1 - *(int *)(iVar1 + 0x20));   /* raw block */
piVar2 = FUN_001e8e20(param_1);                             /* owning allocator */
(**(code **)(*piVar2 + 0xfc))(param_1);                     /* free */
```

Allocate `size + prefix`, hand back `raw + prefix`, free `ptr - prefix`. It is a
per-type allocation header, and the constructor sets it to **0**. So `-1` is
neither a sentinel nor valid data: it is an uninitialised field on a descriptor
that never ran the constructor — which is what the earlier sections said, before
the RTTI detour. Full field map in [TYPE_DESCRIPTOR.md](TYPE_DESCRIPTOR.md).

### The crash needs two faults, and both are now named

1. `desc->prefix` (`+0x20`) is `-1`, because that descriptor arrives from the
   `+0x3c` forwarding chain rather than from the constructor.
2. The allocator virtual call at `[context+0xCC]` returns **0**.

`this_00 = 0 + (-1) = -1`, non-zero, so the null check passes and
`FUN_002096b0` initialises an object at `-1`. Its first statement is the
faulting write. With a correct prefix of `0`, a failed allocation would give
`0 + 0 = 0` and the check would catch it.

Either fix alone stops the crash. Neither cause is understood yet, and a
**failing allocator during type registration** is worth chasing on its own
merits.

### What this says about method

Three readings of one field in a day — residue, then RTTI sentinel, then
allocation prefix — and only the last came from a decompiler. Hand-reading
disassembly produced a plausible wrong answer twice, and both times the wrong
answer was *self-consistent*, which is why it survived. The lab was available
throughout.
