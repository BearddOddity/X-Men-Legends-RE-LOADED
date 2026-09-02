# Where this port stands

*1 September 2026. Figures from a deterministic two-of-two run of the current build.*

**28,318 functions are translated to C. 445 call sites execute.**

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

## Session close, 1 September 2026

**Baseline.** 514 kernel calls, 94 heap allocations, 445 distinct direct call
sites, 154 distinct indirect targets reached. Faults at RVA `0xDED4C5`, in
`sub_001EB890+0x1d5`, reading `0x8B0146F4`. Two walls were broken this stretch
(39 → 41 passed); the baseline moved 434 → 514 kernel calls and 415 → 445 call
sites.

### Wall 42 is a different mechanism, and it is now understood

Walls 40 and 41 were containers that were never filled. Wall 42 is not. The
holder object at `0x01092B58` is the same object on all three calls into
`FUN_0020ef90`, but its field 0 changes:

```
[HOLDER] 01092B58 table=01091B30 f20=00000001
[HOLDER] 01092B58 table=01091B30 f20=00000001
[HOLDER] 01092B58 table=0109863C f20=00000001   <- bogus
```

The guest watchpoint on `0x01092B58` named the writer:

```
[WATCH-POLL] #1 guest 0x01092B58 changed 00000000 -> 01091B30 across before icall
[WATCH-POLL] #2 guest 0x01092B58 changed 01091B30 -> 0109863C across sub_0020AA90
```

Backtrace for hit 2: `sub_0020EF90+0x22c` → `sub_0020EEE0` → `sub_001EC750` →
`sub_00211530`, which is the allocator.

The path is a refcount release. `FUN_00123600` decrements `param_1[1]` and, at
zero, calls `FUN_0020ef90`. That removes the entry from the table and then,
when `holder+0x20` is non-zero, calls `FUN_0020eee0(*this, this)` — a shrink
that stores a new table pointer back into `holder+0`. The replacement pointer
is the bad one, and `sub_001EB890` then binary-searches an object whose count
field holds a pointer and whose array field holds `0x680`.

The auto-release flag was checked and cleared of suspicion. Watching
`holder+0x20` showed it set `0 → 1` by `sub_001F8830`, under `sub_00216EE0` —
the descriptor constructor. It is legitimate initialisation, so the release
itself is intended behaviour and the defect is in what the shrink produces.

### Deliberately not guarded

Per the standing rule: restore a check the original wrote, never invent one to
survive bad data. Walls 40 and 41 were guarded because an empty container is
something the original checked for. Wall 42 is a *wrong object*, and guarding
it would hide the allocator defect rather than fix it.

### Instrument change

`RECOMP_ABI_CALL` and both icall macros now sample the watched value **before**
the call as well as after. An after-only poll fires when a call returns, which
bounds a whole subtree rather than naming a writer — that is what made hit #2
attributable to `sub_0020AA90` specifically.

### Next

1. Decompile `sub_0020EEE0` and `sub_0020AA90` to find why the shrink yields a
   bad table pointer. `sub_0020AA90` appears in the allocator decompilation as
   the free path, so a recycled or double-freed block is the leading
   hypothesis.
2. Seed Ghidra with the 512 heuristically found functions. `sub_001EC5E0`,
   `sub_0020E960` and `sub_002263F0` have no Ghidra function, so the
   decompiler currently covers only part of the critical path.
3. The root defect behind walls 40–42 is unchanged: the subsystem registry
   count stays at 1, the type-lookup fallback that needs ≥2 entries is dead,
   and descriptors are never sized. Fixing the count retires the family rather
   than one wall at a time.

## Session close, 2 September 2026: modern PC targets

Boot is still at wall 42. This session's work was the other half of the
project - making the port a *PC* port rather than a faithful console
reimplementation - which can proceed in parallel because none of it depends on
the boot surviving.

The framing that drove it: the Xbox's limits are the source binary's
constraints, not this port's. Where a limit is enforced by host code we now
write, it becomes a setting.

### Memory is no longer fixed at 64 MB

`XBOX_TOTAL_RAM` was a compile-time 64 MB. It is now `g_xbox_total_ram`, set at
startup by `xbox_ConfigureRam()` from `XBOX_RAM_MB`, accepting 64 or 128 - the
retail and devkit configurations. `kernel_memory.c` derives
`TotalPhysicalPages` from it, so a title querying free memory sees the larger
pool.

Only those two values are accepted, and that is deliberate rather than timid:
the guest's own allocators were built against a 26-bit memory bus and RAM that
wraps modulo 64 MB, which the port reproduces with mirrored views. An arbitrary
size would put the mirror somewhere the guest's arithmetic does not expect. 128
MB is the one larger layout the hardware itself defined.

### Payloads moved out of the guest's way

Host-side payload allocations sat where they could collide with guest memory.
They now live in a dedicated arena, `XBOX_PAYLOAD_BASE 0x0C000000` to
`XBOX_PAYLOAD_LIMIT 0x10000000`, sized by `XBOX_PAYLOAD_MB`, below the 256 MB
ceiling that the Xbox's 28-bit physical resource pointers impose. That ceiling
is real and cannot be raised without breaking every `ptr & 0x0FFFFFFF` the
binary performs, so it is documented as hardware rather than exposed as a
setting.

`d3d8_PayloadAlloc()` takes the arena first and falls back to the guest heap,
so exhausting the arena degrades rather than fails.

### Texture replacement

`src/d3d/d3d8_texrepl.c`. Replacement art is bound at draw time in place of the
game's own texture; the title keeps its small texture object and never learns
anything changed.

Replacements are **host** memory. None of the limits above apply to them - not
64 MB, not the 256 MB arena, not the 28-bit pointers. This is the one place in
the port where "modern PC budget" is literally true.

Identity is an FNV-1a hash of the game's own level-0 pixels, taken at upload.
Hashing content rather than hooking the `.igb` asset loader means replacement
is pipeline-independent: the same texture is recognised however it arrives.

Enable with `XBOX_TEXTURES=<dir>`; the log names the file to supply.

```
[TEXREPL] miss 3F2A9C41D0B7E856  64x64 fmt=6   <- dump this name
textures\3F2A9C41D0B7E856.bmp                  <- provide this file
```

32-bit uncompressed BMP. Not DDS: DDS needs a parser for a large format or an
external library, and neither earns its keep for a feature whose job is "let
someone drop in a bigger picture".

**Sizes.** Only an upper bound is enforced, `TEXREPL_MAX_DIM 4096`. Verified
against a 64x64 original:

| replacement | scale | VRAM with mips |
|---|---|---|
| 512x512 | 8x | 1.3 MB |
| 1024x1024 | 16x | 5.3 MB |
| 2048x2048 | 32x | 21.3 MB |
| 4096x4096 | 64x | 85.3 MB |
| 8192x1024 | - | rejected |

Each step is 4x the memory of the one below, which is why the ceiling sits at
4096 rather than at the 16384 D3D11 feature level 11 permits: a few dozen 4K
replacements would exhaust a mid-range card for detail nobody can resolve.
That is policy, and `TEXREPL_MAX_DIM` is one edit. There is no lower bound, so
art can arrive one resolution at a time.

**Mips are generated, not optional.** A 4K texture standing in for a 64x64
original is minified enormously; unmipped it aliases and crawls in motion -
worse than the texture it replaced. That forces the creation path, because a
mip chain cannot be generated on an `IMMUTABLE` texture created with initial
data: `MipLevels = 0`, `USAGE_DEFAULT`, `BIND_RENDER_TARGET`,
`MISC_GENERATE_MIPS`, upload level 0, then `GenerateMips`.

Negative lookups are cached. A texture re-uploaded every frame would otherwise
stat a missing file every frame.

`g_texrepl_vram_bytes` tracks the cost, because silent VRAM exhaustion is
miserable to diagnose.

### How any of this was testable

None of it could be exercised through the game, which does not reach rendering.
`tools/gfx_harness/` drives the graphics stack with no game attached, in five
stages: create device, clear and present, a D3D8 triangle, an NV2A push-buffer
dispatch, and a texture upload with replacement. All five pass.

The harness is the reason the graphics work is not speculative. It is also a
standing regression test for a subsystem the boot cannot yet reach.

### Scope

Everything here is off by default. Without `XBOX_TEXTURES` the replacement path
is inert; without `XBOX_RAM_MB` the port is a 64 MB Xbox. None of it changes
boot behaviour, and none of it is evidence about wall 42 in either direction.

## Wall 42, traced end to end (2 September 2026)

The boot has not moved. What moved is the understanding of why, and three
positions taken earlier had to be corrected along the way - twice by measurement
that contradicted something written here.

### The mechanism

`0x01092B58` is a **16 KB memory pool**: a 0x28-byte header over an arena, with
capacity at `+0x0C`, base at `+0x10` (which is the header's own end), and current
at `+0x14`. Three blocks allocated through it carry names at `+8` -
`igObject`, `igMetaField`, `igBoolMetaField` - and a refcount at `+4`.

When a block's refcount reaches zero, `sub_00123600` calls
`sub_0020EF90(this = block->field0, arg = block)`, and `field0` is the block's
owning pool. All three name the same pool, which is correct: they were allocated
from it.

`sub_0020EF90` then does three things:

1. `sub_001F87A0(this = pool->parent, block)` - looks the block's **name** up in
   the parent allocator's index, via `sub_001EB890`.
2. `sub_001EBA30(this = pool, block, strlen+9)` - returns the block to the pool.
3. `if (MEM8(pool+0x20)) sub_0020EEE0(this = pool->parent, param_1 = pool)` -
   and that frees the pool.

So the second removal frees the pool while the third block still points at it.
The third removal reads `pool->field0` out of freed memory - by then the
allocator's own free-list link - and looks a name up in it. That is the fault at
`sub_001EB890+0x1D5`.

### The decision to free

`sub_0020EEE0` opens with `if (MEM32(this+0x14) <= 0) goto loc_0020EF65`, and
`loc_0020EF65` is `eax++; MEM32(this+0x14) = eax;`. **The branch that skips the
destroy is the branch that increments the counter gating it.** First removal
spared, every removal after it frees the pool.

A software poll armed on that counter for the whole boot reports exactly one
write: that self-increment. Nothing initialises it.

Both functions in the allocator cluster that genuinely initialise a `+0x14`
write it as the third member of a *(capacity, base, current)* triple. The parent
has the first two set and the third zero. That is a lead and not yet a finding -
nothing has established the parent is of the same class as the objects those
functions construct.

### Corrections made this session

Recording these because each was asserted here or in the ledger first:

- A recursion explanation was retracted, and the **retraction was itself wrong**.
  It argued from "the function is entered on different objects", which proves
  nothing: a teardown walking object A releases A's *fields*, not A.
- Wall 42 was ruled **out** of the walls 40/41 family because its count "was being
  populated, 0 then 1". That 0 -> 1 is the guard incrementing itself, so the
  refutation is void. The family link is reopened, not established.
- The pool was called **exhausted** because `current == end`. That is the state
  its constructor leaves behind; the allocator fills downward.
- `field0` was called a **table**. It is the parent allocator.

### Method

Three instruments disagreed at various points and the disagreements were
resolved rather than voted on. A host backtrace was validated before being
believed - checking that none of the functions on the path were ICF-folded (255
addresses in this build carry multiple names, one carries 224) and that every
frame offset falls inside its function's host extent. Competing probes were run
in the **same build**, because two measurements from two builds can disagree for
reasons unrelated to the question.

The standard that settled it: a position is worth writing down when it explains
every number already observed, rather than discarding some of them.
