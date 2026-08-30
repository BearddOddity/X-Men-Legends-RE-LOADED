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
| **`+0x20`** | **base-subobject displacement. `-1` = "not a base of this type"** | see below |
| `+0x24` | observed `7` on healthy descriptors, `8` on the failing one | observed |
| `+0x28` | zeroed by ctor; set from `sub_00222600` on the full path | `0x0022274B` |
| `+0x2C` | zeroed by ctor | ctor `0x00216F35` |
| `+0x30`–`+0x44` | zeroed by ctor | ctor `0x00216F08`+ |
| `+0x3C` | **forwarding pointer, called indirectly to chain to another descriptor** | `0x0020E530` |
| `+0x48` | read by `sub_0020E547` | observed |
| `+0x60` | zeroed by both construction paths | `0x002222C2`, `0x00222738` |

Allocation size is `0x64` (100 bytes) on both construction paths.

## `+0x20` is a base adjustment, and `-1` is a sentinel

The field is used in **two directions**:

```
sub_0020E547    edi = <virtual call result> + [esi+0x20]    ADD
sub_002041D0    esi = esi - [eax+0x20]                      SUBTRACT
```

Added converting one way and subtracted converting the other is the signature of
a base-subobject displacement — the offset of a base class within a derived
object. MSVC's RTTI displacement records use `-1` for "not present", and this
type system follows the same convention.

So `0xFFFFFFFF` here is **valid data meaning "not a base of this type"**. It is
not corruption, not heap residue, and not an unfinished object — two earlier
notes in this repository said otherwise and were wrong.

## Why that kills the boot

The sequence is a dynamic cast: ask the object for a base pointer through a
virtual call, then adjust the result by the recorded displacement.

| case | virtual call | `+0x20` | sum | caller's `je` |
|---|---|---|---|---|
| cast succeeds | valid pointer | real offset | valid | passes, correct |
| cast fails cleanly | `0` | `0` | `0` | **caught** |
| what happens here | `0` | `-1` | `-1` | **missed** |

The caller tests only the **sum** against zero. A clean failure gives `0 + 0 = 0`
and is caught. This gives `0 + (-1) = -1`, which is non-zero, so it proceeds and
dereferences `-1`.

The guard is not wrong; it is exactly what the original compiler emitted. It
simply never has to handle "no object *and* no base relationship" on hardware,
because on hardware the relationship exists.

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

The type system is correctly reporting that the two types are unrelated. On
hardware they are related and the look-up returns a real displacement. So the
fault is upstream: **a registry that does not know the relationship**. Finding
where that relationship should be registered — a write of a real displacement
into some descriptor's `+0x20` — is the current question, and
`RECOMP_WATCH_GUEST` can catch it now that the field is understood.
