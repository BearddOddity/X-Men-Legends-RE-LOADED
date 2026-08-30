# The type descriptor at vtable `0x003F5D88`

The object the boot currently dies on. Every field below was read out of a
running boot with `tools_data/probe_struct.py` or taken from the constructor's
own disassembly, and the two sources are marked separately, because a field the
constructor writes is *known* while a field only ever observed is *inferred*.

Reading these as raw offsets across a dozen separate probes is how this
investigation went slowly. Recorded here so the next person reads names.

## Layout

| offset | what | evidence |
|---|---|---|
| `+0x00` | vtable pointer, always `0x003F5D88` | ctor `0x00216EFC` |
| `+0x04` | flags. ctor computes `(old & 0xFF000001) \| 1` | ctor `0x00216EF6` |
| `+0x07` | byte, zeroed by ctor | ctor `0x00216EF9` |
| `+0x08` | zeroed by ctor; observed `FFFFFFFF` later | ctor `0x00216F2C` |
| `+0x0C` | zeroed by ctor; observed `1`, `4`, `8`, `FFFFFFFF` | ctor `0x00216F29` |
| `+0x10` | set to `1` by ctor | ctor `0x00216F23` |
| `+0x14` | zeroed by ctor | ctor `0x00216F26` |
| `+0x18` | byte `0` | ctor `0x00216F1A` |
| `+0x19` | byte `1` — so the dword at `+0x18` reads `0x00000100` | ctor `0x00216F1D` |
| `+0x1A` | byte `0`. **Tested against 1** — a "ready/leaf" flag | ctor `0x00216F20`, tested `0x0020E53B` |
| `+0x1C` | zeroed by ctor; later a heap pointer (`011D5008`) | ctor, observed |
| **`+0x20`** | **allocation header prefix, in bytes. ctor sets 0** | ctor `0x00216F32`, see below |
| `+0x24` | observed `7` on healthy descriptors, `8` on the failing one | observed |
| `+0x28` | zeroed by ctor; set from `sub_00222600` on the full path | `0x0022274B` |
| `+0x2C` | zeroed by ctor | ctor `0x00216F35` |
| `+0x30`–`+0x44` | zeroed by ctor | ctor `0x00216F08`+ |
| `+0x3C` | **forwarding pointer, called indirectly to chain to another descriptor** | `0x0020E530` |
| `+0x48` | instance size in bytes, passed to the allocator | `FUN_0020e520` |
| `+0x60` | zeroed by both construction paths | `0x002222C2`, `0x00222738` |

Allocation size is `0x64` (100 bytes) on both construction paths.

## `+0x20` is an allocation header prefix

**Corrected 2026-08-29, after opening the binary in Ghidra.** An earlier
revision of this file called `+0x20` a base-class displacement and read `-1` as
MSVC's RTTI "not present" sentinel. That was wrong. The decompiler shows the
field used in a way a base displacement never is - added to an allocation
*size*:

```c
/* FUN_0020e520 - create an instance of the type described by `this` */
iVar3 = *(int *)((int)this + 0x20);                                  /* prefix */
iVar4 = (**(code **)(*param_1 + 0xcc))(*(int *)((int)this + 0x48) + iVar3);
this_00 = (void *)(iVar4 + iVar3);                                   /* skip it */
if (this_00 != (void *)0x0) {
    FUN_002096b0(this_00, (int)this);                                /* init + register */
}
```

and subtracted again before the block is freed:

```c
/* free_object_instance */
iVar1 = (**(code **)(*param_1 + 0x50))();      /* the descriptor */
param_1 = (int *)((int)param_1 - *(int *)(iVar1 + 0x20));   /* back to the raw block */
piVar2 = FUN_001e8e20(param_1);                /* find the owning allocator */
(**(code **)(*piVar2 + 0xfc))(param_1);        /* free */
```

Allocate `size + prefix`, hand back `raw + prefix`, free `ptr - prefix`. That is
a per-type allocation header, and the constructor sets it to **0** (`xor ebx,
ebx`, then `mov [esi+0x20], ebx`). Most types have no header.

`-1` is therefore not a sentinel and not valid data. It is an uninitialised
field on a descriptor that never ran the constructor - which is what this
document said before the RTTI detour, and what the evidence supported all along.

## Why that kills the boot

Two faults have to coincide, and both do:

1. `desc->prefix` is `-1` on a descriptor the constructor never touched.
2. The allocator virtual call at `[context+0xCC]` returns **0**.

`this_00 = 0 + (-1) = -1`, which is non-zero, so the `!= NULL` check passes and
`FUN_002096b0` initialises an object at address `-1`. Its first statement is the
faulting write:

```c
*(undefined4 *)((int)this + *(int *)(DAT_005bc508 + 0x394)) = *(undefined4 *)(param_1 + 0x5c);
```

With a correct prefix of `0`, a failed allocation would give `0 + 0 = 0`, the
check would catch it, and nothing would crash. The null check is not wrong; the
prefix is.

## Where the descriptors come from

Three paths write the vtable, and which one runs is decided by a flag byte at
offset 0 of the registry singleton at `[0x5BC508]`:

```
002226E5  cmp byte ptr [eax], 0
002226E8  je  0x222708        ; flag == 0  -> CREATE via sub_00222708 (full ctor)
                              ; flag != 0  -> LOOK UP via sub_0020E520
```

Measured over 13 calls: **7 with the flag clear** (matching exactly the 7 objects
`sub_00222708` builds) and **6 with it set**. The flag flips partway through
startup, and after it flips descriptors come from the look-up path — which
follows the `+0x3c` chain and returns objects that never passed through any
constructor.

That is why probes at both the constructor's entry and its vtable store list
eight objects and exclude the two failing ones.

## Root cause: the descriptor is entirely uninitialised

Probing every call to the real allocator settles it. `FUN_0020e520` allocates
`desc->size (+0x48) + desc->prefix (+0x20)` through the context's vtable slot
`0xCC`; that slot is a thunk (`sub_001EC5E0`) forwarding to slot `0x1AC`, which
is `sub_00211530` — a full MSVC-style debug heap. Across its **235 calls** this
boot:

```
[ALLOCN] ctx=01088A90 size=0000000C     healthy
[ALLOCN] ctx=01088A90 size=00000010     healthy
[ALLOCN] ctx=01088A90 size=00000034     healthy
[ALLOCN] ctx=01218000 size=FFFFFFFE     the fatal one
```

The request is for **`0xFFFFFFFE` bytes**. Since the size passed is
`field_48 + prefix` and the prefix is `-1`, **`field_48` is `-1` as well**. The
allocator is asked for roughly 4 GB, correctly fails, and returns `0`. Then
`this = 0 + (-1) = -1`, which passes the non-null check and is dereferenced.

**There is one fault, not two.** Earlier notes here framed the `-1` prefix and
the failing allocator as separate problems worth chasing separately. They are
the same problem: a descriptor whose fields were never written. Both the absurd
size and the bad prefix come from that.

Ruled out along the way, each by measurement:

- **The allocator.** It behaves correctly given a 4 GB request.
- **The heap budget.** `+0x98` (limit) and `+0x9C` (no-limit flag) both read
  `FFFFFFFF`, so the budget branch is bypassed entirely.
- **The `0x005BB700` subsystem registry.** `FUN_001f5c20`, the fallback
  context-getter that reads it, is never called this boot — zero probe hits. The
  empty registry is a real defect but is not implicated in this crash.

## Chain of custody, and a correction

The previous revision said the bad descriptor "arrives from the `+0x3c`
forwarding chain". **It does not.** Probing `FUN_0020e520` across all 18 calls
shows `fwd[+0x3c] = 00000000` on every descriptor, including the fatal one, so
that `while` loop never executes. The descriptor is simply the incoming
argument, already carrying `size=-1, prefix=-1`.

The real chain, each step measured:

| step | what | how established |
|---|---|---|
| `sub_0020E960+0x243` | `mov ecx,[edi+0x38]; call 0x20e520` — the descriptor comes from an **owner object's field** | backtrace |
| owner class | all owners share vtable `0x003F7EA0` | probe over 37 calls |
| healthy owners | point at registered descriptors (`01097498`, `01097518`, `01091AB0`, `01096BB8`) | probe |
| **fatal owner `010982C8`** | points at the unregistered `01098358` | probe |
| `sub_0021B060` | a **copy** constructor that copies `+0x38` from another instance | decompiled; probed, runs **zero** times |
| `sub_0021C3F0` | the **default** constructor — sets `+0x38` to **NULL** (`param_1[0xe] = 0`) | decompiled |

So the owner is default-constructed with a null descriptor, and something
**assigns** `0x01098358` to it afterwards. That assignment is the last unknown.

### The watchpoint could not catch it

`RECOMP_WATCH=0x010982C8+0x38` reported:

```
guest 0x01098300: 8 access(es) reported, last seen 00000000, actually 01098358  <-- MISSED WRITES
```

Eight writes seen, none of them installing the value, and the field ends holding
it. The page-unprotect window is hiding the write — the tool says so rather than
letting the log be believed, which is what it is for, but it means a different
instrument is needed here.

## Answered: the descriptor comes from a dead fallback path

The page-protection watchpoint could not see the write. A **software poll** —
read the watched dword after every recompiled call, report a change with the
callee's name — found it in three hits:

```
[WATCH-POLL] #1 guest 0x01098300 changed 00000000 -> 00010424 across sub_001F3680
[WATCH-POLL] #2 guest 0x01098300 changed 00010424 -> 00000000 across icall sub_00211530
[WATCH-POLL] #3 guest 0x01098300 changed 00000000 -> 01098358 across icall sub_001E9380
```

`FUN_001e9380` is a type-descriptor lookup:

```c
iVar2 = *(int *)((int)this + param_1 * 0x90 + 0x14);   /* my slot for this type */
if (iVar2 == 0) {                                       /* not present */
  if ((flags & 0x4000000) != 0) {
    (**(code **)(*(int *)this + 200))(&param_1, param_1);   /* build it (vfunc 0xC8) */
    return *(int *)((int)this + iVar1 * 0x90 + 0x14);
  }
  iVar1 = DAT_005bc544;                                 /* the subsystem registry COUNT */
  if (1 < DAT_005bc544) {                               /* needs at least two */
      ... find self in &DAT_005bb704, then ask the one registered before me ...
  }
}
```

Probed at runtime: **`DAT_005bc544 == 1`**. The fallback requires `1 < count`, so
with a single registered subsystem **that entire branch is dead code**. A type
this subsystem does not own cannot be inherited from the one before it, and the
lookup yields a descriptor that nothing ever initialised.

### This is BLOCKER_005BB700

`DAT_005bc544` is the count and `&DAT_005bb704` the table from
[BLOCKER_005BB700.md](BLOCKER_005BB700.md) — the four-entry subsystem registry
whose writer, `SubsystemRegistry_Register` at `0x00216210`, never runs because
its only reference is a **data** pointer rather than a call.

**This corrects an earlier note in this repository**, which said the registry was
"a real defect but not implicated in this crash". That was based on
`FUN_001f5c20` never being called — but that is a *different* consumer of the
same registry. This one is on the path.

## The wall is a bootstrap ordering problem

Measured precisely. Of **123** type lookups in a boot, exactly **one** fails:

```
[LOOK2] idx=00000002 slot14=00000000 flags=00000000 regcount=00000001
```

Dumping the pool slots at each lookup says why:

```
[SLOTS] 01087000  all eight slots 00000000     <- the failing lookup
[SLOTS] 01087000  all eight slots 01088A90     <- every later lookup
```

A backtrace on that first lookup shows it comes from `sub_00216251` — **inside
`SubsystemRegistry_Register` itself**. While constructing the very first memory
pool the registrar calls `vfunc 0x58`, which calls back into the type lookup for
type 2, *before any slot has been stored*. Its own loop then fills slots 1–3 and
8+ by copying slot 0, and 4–7 with pools of their own.

On hardware the lookup's fallback to a **previously registered** subsystem
covers exactly this bootstrap window. Here only one subsystem ever registers —
probed: a single invocation, `this=01087000`, count `0 -> 1` — so `1 < count` is
false and the fallback is dead code.

So the fix is not in the descriptor, the allocator, or the create path. It is
that the registry has one entry where the original had more.

## A failed experiment, recorded

`0x005BC51C` is a `char *` the registrar `strcmp`s against `"igArenaMemoryPool"`
and `"igMallocMemoryPool"` to choose which pool class to construct. It has
**5 readers and 0 writers** — a genuinely uninitialised global of the usual
kind, and NULL falls through to the arena path.

Setting it to `"igMallocMemoryPool"` looked like a clean candidate fix. It was
applied next to the TLS-index fix, and the write was verified to land
(`poolname_ptr=003F5C68`, first bytes `igMa`).

**The result was identical** — 434 kernel calls, 92 heap allocations, 415 call
sites, same crash — because all three branches of that `if` call the same
`vfunc 0x58` before allocating. The pool type does not avoid the bootstrap
lookup at all.

Reverted. There is no measured benefit and no evidence the game wants the malloc
pool, and a speculative global write with neither would be a false fix sitting
in the tree. The uninitialised global is still real and still worth fixing
eventually; it is simply not this wall.

## Avenues closed by measurement

Recorded so they are not re-run.

**The failed indirect calls are symptoms, not causes.** The boot logs 271
distinct unresolved targets; 105 fall in the code range. Applying the same
prologue-and-not-inside-a-known-function filter that found the 512 data-referenced
pointers leaves **one** survivor, `0x0012BA6B`, which is unaligned and preceded
by real code — mid-function, not an entry point. The rest are null-derived
garbage: `0`, `1`, `2`, `0x394`, `0xCCCCCCCC`, and runs like `0x0003FFE3`,
`0x0007FFE3`, `0x000FFFE3`. Separately, all 512 targets rejected by the
plausibility filter fall **outside** the code range entirely, so the filter is
not eating real calls. Seeding will not move this wall.

**`g_IsEngineRunning` is faithful.** It lives at `0x005BC548`, and
`sub_00011E40` sets it to `1` at `0x00011E60`, before the registrar runs. So the
registrar's "engine already running" branches are the correct ones — which is
exactly why all eight pool slots end up holding the same pool. Not a defect.

**The software poll bounds a region; it does not name a writer.** It fires after
a call *returns*, so it attributes a change to that call's entire dynamic
subtree. Reporting the write as "across `icall sub_001E9380`" narrows it to that
subtree and no further. `sub_001E9380` can legitimately return `0` — flags clear,
slot empty, `regcount = 1` so the fallback is dead — while something deeper
inside it writes `owner+0x38`. An earlier section of this document read that
report as identifying the writer. It does not.

## The `-1` is a marker, and the filler is `sub_00216FD0`

Watching a **healthy** descriptor's size field with the software poll gives both
writes, with backtraces:

| write | from | meaning |
|---|---|---|
| `00000000 -> FFFFFFFF` | `sub_00216EE0+0x2e3` — the descriptor constructor, under `sub_00222708` | "size not yet known" |
| `FFFFFFFF -> 00000010` | **`sub_00216FD0+0x741`**, under `sub_002235D0` → `sub_002225B0` | the real size, from registration |

So `-1` is written **deliberately** by the constructor as an unset marker, and
the registration driver fills the real value afterwards. Earlier sections of this
document called it residue and then a sentinel; it is a marker, and this is the
measurement that settles it.

The fatal descriptor gets the first write and never the second:

- the **seven healthy** descriptors are built by `sub_00222708` and then sized by
  registration
- **`01098358`** is manufactured by the **look-up** path — `FUN_002226e0` with the
  registry ready-flag set, calling `sub_0020E520` to create an *instance* of the
  descriptor type — and nothing sizes it afterwards

### Cleared this round

- **No failed icall is involved.** The class-construct vfunc at slot `0x24`
  resolves for every object and both targets are lifted (`0013D370` for the
  descriptor class, `001EAED0` for the owner class).
- **Construction cannot set the size.** `sub_0013D370`, the descriptor class's
  construct vfunc, is an **empty function** — `return;`. Sizing is registration's
  job, not the constructor's.
- **The NULL vtable at `sub_002096B0` entry is faithful.** Its first statement
  installs the vtable, reading the value from `[desc+0x5c]` and the offset from
  `[registry+0x394]`.

## The ordering, from the original's own code

Watching the ready flag at `[0x5BC508]+0` shows it flip `0 -> 1` exactly once,
inside `sub_002366BC` — a fragment of `sub_00236500`. The decompilation shows
the sequence plainly:

```c
FUN_00209650(&LAB_002221e0);   /* first type module - flag still 0 */
*param_1 = 1;                  /* the ready flag is set HERE       */
FUN_00209650(&LAB_00220870);   /* ~24 more modules - flag now 1    */
FUN_00209650(&LAB_002361a0);
...
```

So the first module registers through the **create** path and everything after it
goes through the **look-up** path. That is the original's own ordering, so it is
not a bug: the look-up path is *expected* to hand back a blank descriptor which
the registration then populates, and `sub_00216FD0` is what populates the size.

It does so for descriptors reached through `sub_002235D0`. The fatal one is
reached through `sub_00223960` → `FUN_002226e0`, stored by `sub_002263F0` — which
sets `owner+0x34 = 1` and never sizes it.

`sub_00223960` caches its result in `DAT_005bc1ac`, which is BSS and null, so the
type it wants was not registered during the flag-clear window. Its argument
`DAT_0046a684` is `6`, a memory-pool index rather than a type name, so the type
cannot be identified from the call site alone.

## Wall broken

| | before | after |
|---|---|---|
| kernel calls | 434 | **514** |
| call sites | 415 | **435** |
| heap allocations | 92 | **94** |
| crash | `sub_002096B0+0xd5` | **`sub_001EA600+0x2c3`** |

Deterministic 2 of 2. One line, in `sub_0020E547` immediately after
`edi = edi + ebx`:

```c
if (ebx == 0xFFFFFFFFu) { edi = 0; }
```

`ebx` is the descriptor prefix from `[esi+0x20]`, and the allocator is asked for
`[esi+0x48] + ebx`. A descriptor that has been created but not yet sized carries
`-1` in both, so the request is `0xFFFFFFFE`, the allocator correctly returns
NULL, and the line computes `0 + (-1) = -1`. That is non-zero, so the `je` on the
next line lets it through and `sub_002096B0` writes through `-1`.

With a *sized* descriptor the same allocation failure gives `0 + 0 = 0` and the
`je` catches it. **The guard restores the behaviour that `je` was written for**
rather than inventing one — which is why it is a defensible bypass and not a
papered-over crash.

**It is a bypass, not a fix.** The real defect is unchanged and still open:
descriptors reached via `sub_002263F0` are never sized. This only stops an
unsized one from being dereferenced.

Persisted into `manual_edits.json`, so it survives a regeneration —
`manual_edits.py verify` reports **1068/1068** placed.

### A trap worth recording

`manual_edits.py extract` rewrote the store with **1061** entries against the
**1067** held, silently dropping six edits that exist in the store but not in the
current `gen/`. Extraction reads `gen/`, so anything not currently applied there
disappears. Restored from git and the single new entry added surgically instead.
Do not blind-extract after a regeneration or a probe sweep.

## Open

Unchanged by the bypass: **why are descriptors reached via `sub_002263F0` never
sized, when those via `sub_002235D0` are?** Both take the look-up path and both
receive a blank descriptor; only one is populated afterwards.

The next wall is `sub_001EA600+0x2c3`, faulting on `0xD3010000`.
