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

## Resolved: a callee-saved register does not survive the icall

`sub_00209650` null-checks the pointer before it uses it, and the check is
correct — the compiler's own listing confirms it:

```
001aa  mov  eax, [r12+r14]      ; eax = g_eax
001ae  mov  [r14+r15], eax      ; g_edi = eax        (r15 = OFFSET g_edi)
001b2  test eax, eax
001b4  je   loc_002096A8        ; exits on zero
```

The guard is not bypassed. It simply **runs once, before the loop**, and
`+0x22b` is inside the loop body — `jl $loc_00209666` at `+0x3a3` is the
back-edge. Each iteration then calls through a function pointer:

```c
loc_00209655: edi = eax; if (TEST_Z(edi, edi)) goto loc_002096A8;  /* once */
loc_00209666: eax = MEM32(edi);                    /* +0x22b, reads guest 0 */
              icall MEM32(eax + esi * 4);          /* +0x244 */
loc_0020966B: esi++; if (CMP_L(esi, ebx)) goto loc_00209666;
```

`edi` is callee-saved in the real x86 ABI, so the original code was entitled to
assume it survived that call. It does not. Nothing re-checks it, so every later
iteration dereferences whatever `edi` decayed to.

`RECOMP_CHECK_ABI` could not see this: `RECOMP_ABI_CALL` checks `ebx/esi/edi`
on **direct** calls, while the indirect path checked `esp` alone. Extending it
(`RECOMP_ICALL_WATCH`, reported by `recomp_abi_violation_va`) found 14 indirect
callees that fail to restore a callee-saved register, against 21 direct ones:

```
[ABI-ICALL] sub_00342D98 did not restore:  esi 01091B4C->01091B30  edi 01091B50->00000000
[ABI-ICALL] sub_002225F0 did not restore:  ebx 00000003->01096C50  esi 00000000->00000001  edi 01096C50->00000000
[ABI-ICALL] sub_000CC200 did not restore:  ebx 01096C50->FFFFFFFF  esi 00004BBF->0035197A  edi 00000000->00000004
```

**`edi` is clobbered to exactly `0` and `4`** — the two values the census
independently recorded at the two sites in this function (`+0x22b` reads guest
`0`, `+0x3b9` reads guest `0x4`). Two unrelated measurements agreeing on the
same pair is what makes this a diagnosis rather than a story.

Reproduce with `src/game/build_abi.bat && src/game/run_abi.bat`.

Caveat carried over from the direct-call version: lifter fragments are expected
to trip this, because a fragment's pushes can be matched by a pop in a sibling,
so in isolation it looks unbalanced while the pair is fine. Cross-check a name
against `find_reg_clobbers.py` before treating it as the culprit. The
`sub_00342D3C`–`sub_00342D98` cluster in particular is six near-adjacent
addresses, which is the shape of a thunk family rather than six real bugs.

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

## Which callee, and a defect found on the way

Adding the caller backtrace to `recomp_abi_violation_va` (the direct-call
reporter already had one) named the call sites. Three violators are reached
directly from `sub_00209650`, at two of its four icall sites — resolved against
an `/FAsc` listing built with the **ABI** flags, since that build's codegen
differs from the page-zero build's:

| callee | site | resolves to | edi |
|---|---|---|---|
| `sub_002225B0` | `+0x253` | line 28022 `edi = eax` — return of the **entry** icall | `01096C50->01097498` |
| `sub_002225F0` | `+0x51e` | line 28039 `esi++` — return of the **loop** icall | `01096C50->00000000` |
| `sub_000CC200` | `+0x51e` | line 28039 `esi++` — return of the **loop** icall | `00000000->00000004` |

Backtrace frames are return addresses, so each lands just after its call.

`0` and `4` are the two values the census recorded, and they arrive from the
loop's own icall. That is the mechanism, measured end to end.

### The pair is mutually recursive

`sub_002225F0` does nothing but call back in:

```c
void sub_002225F0(void) {
    PUSH32(esp, 0x2225B0);            /* push sub_002225B0 as the callback */
    RECOMP_ABI_CALL(sub_00209650);    /* and re-enter sub_00209650 */
    POP32(esp, ecx);
}
```

`0x2225B0` is the function pointer `call [esp+8]` reads at `sub_00209650`'s
entry. So "`sub_002225F0` did not restore edi" means "the recursive
`sub_00209650` subtree did not restore edi" — the damage is inside the
recursion, not in that three-line thunk.

`sub_002225B0` pushes eleven arguments and calls `sub_002235D0`, unwinding
`0x2C` — balanced. Among those arguments are `0x5BC2FC`, adjacent to the
`0x005BB700` / `0x005BC544` subsystem registry in
[BLOCKER_005BB700.md](BLOCKER_005BB700.md), and `0x3F9780`, which appears in the
ABI report as `sub_0020B850`'s incoming edi. This is the subsystem registration
path, reached from the other direction.

### A misapplied esp fix at loc_0020969D

```c
loc_0020969D:
    edx = MEM32(eax);
    PUSH32(esp, edi);                 /* an ARGUMENT to the virtual call */
    uint32_t _icall_esp = g_esp;      /* captured AFTER that push */
    ecx = eax;
    _icall_target = MEM32(edx + 0xFC);
    PUSH32(esp, 0); RECOMP_ICALL_SAFE(_icall_target, _icall_esp);
loc_002096A8:
    POP32(esp, edi);                  /* restores the callee-saved edi */
```

`RECOMP_ICALL_SAFE` restores `g_esp = _icall_esp` when the target cannot be
resolved. Because the capture sits *after* the argument push, a failed icall
leaves that argument on the stack — the real callee would have removed it, the
safe stub does not. `POP32(esp, edi)` at `loc_002096A8` then takes `edi` from
the wrong slot and the frame ends 4 bytes off.

The function's own manual-fix comment explains why the capture was moved after
the push: at the **entry** icall that push is a genuine callee-saved register
save, and capturing after it is right. At `loc_0020969D` the push is an
**argument**, which is the opposite case. The same fix was applied to both by
`tools_data/find_icall_esp_saves.py --fix` — the duplicated comment blocks in
the generated source are the fingerprint of that pass running repeatedly.

Not yet confirmed as the source of the `0`/`4` values: the backtraces put those
at the **loop** icall, not this one. This is a real defect found alongside, and
it should be fixed on its own merits.

### The static clobber checker cannot adjudicate this

`tools_data/find_reg_clobbers.py` reports **no** callee-save violation in
`sub_002225F0`, `sub_000CC200` or `sub_002225B0`, and `--callees` finds only
1 function reachable from each — it follows direct calls, so it is blind to
indirect dispatch for exactly the reason the recompiler is.

For `sub_00209650` it reports `edi push=3 pop=1`, which is a false positive:
of the three pushes only the entry one is a register save, and the other two are
call arguments (one cleaned by `esp = esp + 4`, one by the callee). It emits
**6,321+ findings** overall, so treat it as a lead generator, not a verdict.
The runtime check is the trustworthy instrument here.
