# The game names its own classes

X-Men Legends runs on Intrinsic Alchemy, Vicarious Visions' middleware. Alchemy
registers every class at startup through one function, and **each registration
call pushes the class name as a plain C string alongside the class's size in
bytes**. The binary carries its own symbol table for 696 classes, in data, with
no debug info and no SDK required.

This is the best naming source found for this binary. It needs no version
reasoning, and unlike the Alchemy SDK it also covers Raven's own classes.

## The registrar

`0x002235D0`. Every call site is a run-once flag followed by a block of
`push imm32` and the call:

```
00289490  mov al, [0x5bdf6c] ; test al,al ; jnz     ; run-once guard
002894a0  push 0x47180c
002894a7  push 0x255fc0                             ; A - registerFields
002894ac  push 0x27c080                             ; B - deleting dtor wrapper
002894b1  push 0xc8                                 ; SIZE - 200 bytes
002894b6  push 0x405f40                             ; NAME - "igBumpMapShader"
002894bb  push 0x250870                             ; C - own getClassMeta
002894c0  push 0x19c140                             ; D - parent accessor
002894c5  push 0x281670                             ; E - parent's arkRegister
002894ca  push 0x5bdb0c                             ; the class's _Meta global
002894cf  push 0
002894d1  call 0x2235d0
```

All 698 sites have exactly eleven arguments in this order.

Extract with `tools/ghidra_naming/ghidra_scripts/ExtractClassRegistrations.java`.
Output is checked in at `tools/ghidra_naming/data/class_registrations.tsv`.

## How it was found

Not by looking for the registrar. A destructor that the SDK body matcher could
not identify was followed backwards:

```
002766a0  the destructor
  <- called by 0027c080
     <- referenced at 002894ac, which is in .text but inside NO function
```

An address referenced from `.text` that no function covers is a `push` operand.
Reading the rest of that block found `0x405f40`, which is the literal string
`igBumpMapShader`. Listing the callers of the `call` target at the end of the
block gave the whole table.

## What the five code slots are

Each role was established by reading an example, not inferred from position:

| slot | role | how it was established |
|---|---|---|
| A | `registerFields` | `0x00255FC0` writes 25 fields into igBumpMapShader's *own* meta global `0x005BDB0C` |
| B | deleting destructor wrapper | calls the real destructor one level down (checked on igResource and igBumpMapShader) |
| C | own `getClassMeta` | igObject's C returns igObject's `_Meta` global |
| D | a **parent** accessor | co-varies exactly with E; 143 distinct values |
| E | **parent's** `arkRegister` block | points just before another site's call address |

Statistics across all sites, which is what suggested where to read:

| slot | uses | distinct | shared |
|---|---|---|---|
| A | 429 | 428 | 2 |
| B | 429 | 428 | 2 |
| C | 698 | 696 | 4 |
| D | 698 | 143 | 597 |
| E | 698 | 143 | 597 |

**A and B are present at only 429 of the 698 sites** - the instantiable classes.
Abstract classes register no fields and have no destructor.

### Slot D does not resolve the parent; slot E does

Reading D as "the parent's slot C" was generalised from one example
(igResource -> igObject) and is wrong. It resolves 120 of 696 classes, all of
them igObject, because a class has several meta accessors (`getClassMeta`,
`getClassTypeSafe`, `getClassTypeLazy`) and D is not the same function as the
parent's C.

Slot E points *into* the parent's push block, so matching it against the site
addresses resolves **695 of 696**. Hierarchy checked in at
`tools/ghidra_naming/data/class_parents.tsv`.

The general lesson: a resolution method that returns one dominant value for a
small fraction of the input is signalling a wrong anchor, not a real result.

## Validation

Checked against the inheritance declared in the Alchemy 5.0 SDK headers
(`class X : public Y`). Of the 581 classes present in both:

| | count |
|---|---|
| agree exactly | 399 |
| differ only in template form (`igObjectList` here, `igTObjectList` in the SDK) | 154 |
| genuinely different | 28 |
| **effective agreement** | **95.2%** |

The 28 are real version drift and worth knowing, since they are exactly the
classes refactored between the game's Alchemy and 5.0: `igGeometryAttr` derives
from `igVisualAttribute` here but `igDrawableAttr` in 5.0, and the `igDx*` array
classes gained an `igDxCommon*` intermediate later.

## Why this matters for the boot

`docs/TYPE_DESCRIPTOR.md` describes the object the boot dies on. That object is
**`igMetaObject`**, and the registry confirms it independently: igMetaObject is
registered with size **100 bytes = 0x64**, which is exactly the allocation size
that document already recorded for both of the descriptor's construction paths,
established weeks earlier by a different method.

So the SIZE column of this table is the value that belongs at `+0x48` of every
descriptor - the field the constructor deliberately sets to `-1` to mean "not
yet sized". **The true size of all 698 classes is now known**, which makes it
directly checkable which descriptors are unsized at runtime and what each should
have held.

Sizes for classes on the boot path: `igObject` 8, `igNamedObject` 12, `igNode`
28, `igGroup` 32, `igMetaField` 52, `igMetaObject` 100.

## Applied to the Ghidra database

`ApplyRegistrationNames.java` named **717 functions**
(`<Class>::registerFields`, `::scalar_deleting_dtor`, `::dtor`,
`::getClassMeta`) and labelled **696 metaobject globals** `<Class>_Meta`. Those
globals live in the `0x5Bxxxx` region that `docs/PAGE_ZERO_CENSUS.md` reports as
largely never written.

208 functions that already carried a non-generated name were left alone; the
script never overwrites a name a person chose.
