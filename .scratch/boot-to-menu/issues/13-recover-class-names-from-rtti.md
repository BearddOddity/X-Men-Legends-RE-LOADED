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

<!-- filled on resolution -->
