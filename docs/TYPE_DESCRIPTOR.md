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

## Open

The instance the boot dies on is created by `FUN_0020e520`, which is
"create an instance of this type": follow the `+0x3c` forwarding chain, refuse
if `+0x1a` is 1, then allocate `size + prefix` through the context's
`[+0xCC]` and initialise via `FUN_002096b0`.

Two questions remain, and they are separate:

1. **Why is the prefix `-1`?** Because that descriptor never ran the
   constructor. It arrives from the `+0x3c` chain rather than from
   `sub_00222708`, which is measured: probes at the constructor's entry and at
   its vtable store both list eight objects and exclude this one.
2. **Why does the allocator return 0?** `[context+0xCC]` is an allocation that
   fails. On its own that would be handled - a correct prefix of `0` makes the
   null check catch it. This is the fault worth chasing next, because a failing
   allocator during type registration is a problem in its own right.

Either fix alone stops the crash. Neither is understood yet.
