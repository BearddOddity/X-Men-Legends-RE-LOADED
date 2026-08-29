# The guest null page

Guest VA 0 is mapped and readable in the normal build. That is deliberate, and
it is the reason a whole class of bug on this project stays invisible: a null
pointer dereference in lifted code does not fault, it quietly returns 0.

This is what actually touches it, measured rather than assumed.

## How to reproduce

`RECOMP_TRAP_PAGE_ZERO` makes guest `0x0`–`0xFFF` `PAGE_NOACCESS`, installs a
vectored exception handler that records every access — guest offset, read or
write, and faulting RVA — then resumes, so one boot yields the whole list
instead of dying on the first hit.

```bash
src/game/build_page0.bat && src/game/run_page0.bat
```

It builds into `build_page0/`, separate from `build/`. **It is a diagnostic
build: never measure coverage or progress on it.** Resolve an RVA against
`build_page0/xmen_legends_recomp.map` — preferred load address `0x140000000`,
so the map key is `0x140000000 + rva`.

## The measurement

22,403 accesses across 74 distinct sites, of which **22,382 are reads and 21
are writes**.

A run on 2026-08-09 and a run on 2026-08-29 — across a re-lift, the 609
recovered functions, the anchor repair and the manual-edits repair — produced
22,402 and 22,403 accesses with identical top sites at identical function
offsets. The behaviour is deterministic and none of that work moved it.

| site | accesses | guest VA |
|---|---|---|
| `sub_00209650+0x22b` | 19,390 r | `0x0` |
| `sub_00209650+0x244` | 1,022 r | `0x8`–`0xFFC` |
| `kernel_thunk_dispatch+0x389` | 896 r | `0x1`–`0x72C` |
| `kernel_thunk_dispatch+0x179` | 896 r | `0x1`–`0x72C` |
| `sub_001FBA90+0xb7` | 14 r | `0x0` |
| `sub_001FBA90+0x76` | 14 r | `0x0` |

## It is reads, not corruption

The working hypothesis before this ran was that something *writes* into page 0
— specifically that `g_fs_base` is `RECOMP_TLS` and assigned only once, in
`xbox_memory_layout.c`, so a secondary thread would have `g_fs_base == 0` and
its `fs:[0x20]` stores would land at guest `0x20`.

**The census refutes that.** Guest `0x20` is read and never written, and page 0
takes 21 writes in total against 22,382 reads. Whatever is wrong here, it is
not a stray-write problem, and it is not `g_fs_base`.

## Do not unmap it

Unmapping page 0 is what this build already does, with a census and a resume
instead of a crash. Leaving it unmapped in the normal build would also regress
a documented fix: low memory is deliberately left as readable zeros, which is
what resolved an earlier 7.6-million-iteration spin (see the comment in
`src/kernel/xbox_memory_layout.c`). The guest mapping starts at `0x00000000` by
construction — `XBOX_MAP_START` — so page 0 is mapped by the flat offset model,
not by accident.

## One function is 91% of it

`sub_00209650` accounts for 20,412 of the 22,403 accesses. Disassembling
`build_page0` at those two RVAs (`rax` holds `g_memory_offset`, so `[rcx+rax]`
is a guest read):

```
ECB1F0  mov  ecx, [r14 + r15]        ; load a guest register
ECB1FB  mov  edx, [rcx + rax]        ; <-- +0x22b: MEM32(that register), guest 0
ECB20A  lea  ecx, [rdx + rax*4]      ; base + index*4
ECB214  mov  ebx, [rcx + rax]        ; <-- +0x244: MEM32(base + index*4)
```

That pair is `eax = MEM32(edi)` followed by `_icall_target = MEM32(eax + esi*4)`
— the function is loading a pointer out of an object and walking it as an array
of function pointers. With the base at 0 it walks the null page instead, and the
`0x8`–`0xFFC` span is one pass of ~1,024 iterations across the whole page.

This is the same indirect-dispatch failure recorded in
[BLOCKER_005BB700.md](BLOCKER_005BB700.md), seen from the other side: bogus
targets read out of page 0 feed straight into the failed-icall count.

## Open contradiction — do not skip this

`sub_00209650` null-checks the pointer before it uses it:

```c
loc_00209655: edi = eax; if (TEST_Z(edi, edi)) goto loc_002096A8;
loc_00209666: eax = MEM32(edi);              /* +0x22b, reads guest 0 */
```

`TEST_Z` is operand-based and correct (`((a) & (b)) == 0`), so this is not a
flags-model bug. Yet the read at `+0x22b` lands on guest 0 every time, which
means `edi == 0` at a point the guard should have made unreachable.

Both cannot be true. Either the basic block at `+0x22b` is not the one this
source line maps to — the offsets do not follow source order, and the single
`MEM32(edi + 4)` read of guest `0x4` sits at `+0x3b9`, *after* both hot sites,
so the optimiser has reordered blocks — or the guard is being bypassed. The
scaled read at `+0x244` is certain from the `lea`; the exact source line for
`+0x22b` is not yet pinned down. Resolve this before treating the C above as
the fix site.

## The crash site reads page 0 too

The normal build crashes at `sub_001FBA90+0x76`. That exact offset appears in
the census reading guest 0, 14 times.

`sub_001FBA90` is a heap-block validator: it checks a `0xAAAAAAAF` guard cookie
at `[eax-4]`, reads a header offset at `[eax-8]` capped at `0x100010`, and
subtracts it to find the block header. Reading guest 0 from `[eax-4]` or
`[eax-8]` means **`eax` is a small value like 4 or 8, not null** — so it passes
the `TEST_Z(eax, eax)` null guard at the top of the function and walks into the
header arithmetic with a garbage pointer. In the normal build those reads return
0, the cookie check fails, and it proceeds into `sub_001F34E0` and dies.

That makes the crash a downstream symptom, not the root cause. The pointer is
already wrong by the time it arrives.
