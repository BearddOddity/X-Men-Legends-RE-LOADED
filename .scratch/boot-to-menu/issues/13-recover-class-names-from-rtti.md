# Recover 562 C++ class names from RTTI already inside the XBE

Status: claimed
Type: task

## Question

**The XBE carries full MSVC C++ RTTI and the project has never used it.**
Discovered 2026-08-08 while investigating the XML2 reference-binary idea —
this finding is independent of that and needs no second binary.

Measured, not inferred:

- `default.xbe` contains **562 unique MSVC RTTI type descriptors**
  (`.?AV<name>@@` mangled strings). Confirmed by direct grep of the XBE.
- Ghidra's open `/default.xbe` program has **41 non-default symbols total**
  (`get-symbols` with `filterDefaultNames`, `totalProcessed: 41`) — all XBE
  headers and kernel import thunks. **Zero** class names.
- `seeded_functions.json` holds 26,506 functions, every one named
  `sub_XXXXXXXX`. No naming artifacts exist anywhere in the repo.

So 562 named classes sit in the binary as raw strings while every function
in the recomp is an anonymous address.

### The classes include exactly what we are stuck in

```
IAlchemyObjectPool      IChuckAllocator      CBlock
CMemory / IMemory       CEntityAllocator     CLineBlock
IMemoryPoolInfo@CMemory IEntityAllocator     CResponseBlock
```

Every wall for the past week has been in the allocator — manager object,
pools, free lists, coalescer. Those routines have **names sitting in the
binary we have not read.**

### Why this needs a script, not a checkbox

`list-analyzers` on `/default.xbe` returns 31 analyzers and **none is an
RTTI analyzer**. Ghidra's MSVC RTTI analyzer is PE-specific; the XBE loader
does not present the program as a Windows PE, so it never applies. The RTTI
*data* is still in standard MSVC layout — only the automatic discovery is
missing.

Write a Ghidra script (ReVa has `write-script` / `run-script`) walking the
documented MSVC RTTI chain:

1. Find each Type Descriptor — the `.?AV...@@` string with its vftable
   pointer and spare field ahead of it.
2. Find Complete Object Locators referencing each descriptor.
3. A vtable's COL pointer sits at **`vtable[-1]`** — so a pointer to a COL
   identifies the slot immediately after it as a vtable start.
4. Name the vtable and its slots `ClassName::vfN`, and demangle the
   `.?AV...@@` form to a readable class name.

`recon.py` already located **1,272 vtables**; those are the targets to
match against. Cross-check the script's output against that set — agreement
is the correctness signal, disagreement is a bug in one of the two.

### Consuming the result

`tools/ghidra_naming/merge_names.py:22` already defines an **`rtti`**
category for "class/vftable/RTTI-derived names", so the naming pipeline is
built to accept this — nothing new is needed downstream. Export through the
existing path rather than inventing a second one.

### Why this outranks tickets 11 and 12

It needs **no external binary, no acquisition, no cross-compiler
correlation guesswork**. The data is already on disk and confirmed present.
Tickets 11/12 (the XML2 diff) remain worthwhile — they could name
*non-virtual* functions RTTI cannot reach — but this is cheaper and far more
certain, so do it first.

Expect it to name only functions reachable through vtables; free functions
and non-virtual methods stay anonymous. That is still likely hundreds of
functions, and disproportionately the polymorphic engine code the walls live
in.

## Answer

Built `tools/ghidra_naming/rtti_names.py` — a standalone parser, not a
Ghidra script. PyGhidra was unavailable when this started, and parsing the
file directly turned out to be the better route anyway: it needs no running
Ghidra, and it emits the `{addr: name}` shape `merge_names.py` already
consumes.

### Results, XML1 `default.xbe`

```
type descriptors               873
complete object locators       743   (954 candidates rejected)
vtables                        743   (0 rejected)
distinct classes with vtables  726
functions named               1731
functions left ambiguous      2147   (appear in >1 vtable, deliberately unnamed)
```

**Correctness signal: 82.1%** of the 1,731 named functions land on an
address `seeded_functions.json` already knows is a function start. A broken
address walk would score near zero, so the chain
descriptor → COL → vtable → method is landing on real entries. The 954
rejected COL candidates show the validation is doing real work rather than
accepting every coincidental dword.

Artifacts:

- `tools/ghidra_naming/rtti_names.json` — 2,474 entries, merge_names format
- `tools/ghidra_naming/rtti_xml1.json` — full report incl. per-vtable methods
- `src/game/tools_data/rtti_missing_functions.json` — see below

### It does NOT crack the current wall — stated plainly

Every function this map is stuck on came back **not reached via any
vtable**: `sub_00204800`, `sub_00204020`, `sub_0020F209`, `sub_0020EFD0`,
`sub_00211530`, `sub_0020F860`, `sub_001F7930`, `sub_0020E547`.

They are non-virtual, so RTTI cannot see them. This ticket predicted that
limitation up front and it landed exactly there. Ticket 02 remains the route
to the live wall; this did not shortcut it.

### The genuinely useful by-product: 791 seed candidates

3,878 distinct addresses are vtable slot targets. **791 of them are not in
`seeded_functions.json`** — the binary proves each is a function (a vtable
points at it) and the recomp does not know it. That is the same
missing-function seam that produced `sub_00221E50` (+36 reached, 0 lost),
and this list is stronger evidence than the icall-failure lists used before,
because a vtable entry is a compiler-emitted fact rather than an inference
from a failed call.

Written to `src/game/tools_data/rtti_missing_functions.json`, each with the
class whose vtable references it.

**DO NOT BULK-SEED THIS LIST.** On 2026-08-05 batch-seeding 13
tool-recommended addresses took kernel_calls 1452 → 56, a 25x regression.
The rule that came out of that incident applies exactly here: "this address
is real" and "seeding this address is safe" are independent questions, and a
mid-function target emits a fragment that inherits a half-built frame. Seed
one at a time with a measurement between, and back up `seed_list.json` first.

### Known limitation: the demangler does not handle templates

Names like `$0CA::USAlphaFunctionAttrTraits::?$CAlchemyObjectPool` are
MSVC template instantiations (`?$` marks a template, `$0CA` an integer
argument) that the simple reverse-the-components demangler mangles further
rather than resolving. Plain and nested classes are correct
(`CMemory::IMemoryPoolInfo` is right); templates are cosmetic noise. Worth
fixing only if template class names turn out to matter.

### What the class list reveals about the engine

Even without naming the wall, the recovered classes are informative:
`CMemory`, `CMemory::IMemoryPoolInfo`, **`CMemory::SXMenMemoryPoolInfo`**
(a game-specific pool descriptor), `XMallocChuckAllocator`,
`IAlchemyObjectPool`, `Gap::Sg::igObjectPool`, plus `Raven::SceneLib::*` and
a `ratl::` namespace — Raven's own template library. The allocator we have
been reversing blind has a documented shape after all.

### Remaining

Not yet done, and both are cheap follow-ups rather than blockers:

1. Cross-check the 743 RTTI-confirmed vtables against `recon.py`'s 1,272
   detected vtables — agreement on the overlap is a second correctness
   signal, and recon's extra ~529 are probably vtable-shaped tables without
   RTTI (or false positives worth knowing about).
2. Run the same tool against XML2's XBE and diff the class sets, now that
   PyGhidra is up and ticket 12 can proceed.
