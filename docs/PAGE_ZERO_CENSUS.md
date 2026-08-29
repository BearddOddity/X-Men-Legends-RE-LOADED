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

## Fixed at the generator, which is where ledger #145 said it belonged

Ledger #145 recorded the real remedy and left it open:

> The real remedy is translator.py's `_fixup_icall_esp_save`, which cannot
> distinguish an argument push from a callee-saved register save - still unfixed.

`_icall_esp` has to sit **below** the prologue's callee-saved pushes and
**above** the argument pushes. Both mistakes have shipped here, in opposite
directions:

| where the capture went | what breaks |
|---|---|
| too high, above the prologue saves | a failed ICALL rolls `g_esp` back past the saves, and the epilogue's `POP32`s read the wrong slots — ledger #145, `sub_001EA770` |
| too low, below an argument push | a failed ICALL leaves the argument on the stack, and the following `POP32` takes the register from the wrong slot — `sub_00209650` `loc_0020969D` |

The generator produced the first. `find_icall_esp_saves.py --fix` corrected some
of those and produced the second on four sites, because it classified a push as
a save whenever the epilogue popped that register — and the epilogue pops it
either way when a register is saved in the prologue *and* later passed as an
argument.

The discriminator both tools now use: a push is a prologue save only if it is
that register's **first** push in the function and the function pops it again.
Every later push of the same register is an argument.

Audit of the whole tree found exactly **9** sites with the capture below a
callee-saved push — 5 genuine prologue saves, 4 misapplied to arguments
(`sub_00209650` ×2, `sub_002235D0`, `sub_00236500`). The 4 are corrected, and
`tools/recomp/test_icall_esp_fixup.py` covers the boundary in both directions.

Generated sources are gitignored, so the durable fix had to be in
`translator.py` — a hand edit to `src/recomp/gen/*.c` does not survive a
re-lift.

**Measured: no behaviour change.** 226 kernel calls, 95 heap allocations, crash
unchanged at `sub_001FBA90+0x76`. Kept on the same precedent as ledger #145,
which was also coverage-neutral: the sites are real defects on paths the boot
does not currently fail on, and leaving a known-wrong capture in place because
it has not bitten yet is how #145's crash survived as long as it did.

## Correcting ledger #149: sub_002002B0 is innocent

Ledger #149 named four callbacks that fail to preserve `esi`, and put
`sub_002002B0` first because it produces the fatal `4`:

> cb=002002B0 turns 01097498 into 4 ... sub_002002B0 never pushes esi at all and
> opens with `esp -= 0x10`, so either the original genuinely does not touch esi
> and a callee does, or its prologue is mis-lifted.

Neither branch of that disjunction holds. Disassembled from the XBE, all eleven
instructions:

```
002002B0  sub    esp, 0x10
002002B3  xor    eax, eax
002002B5  mov    [esp + 8], eax
002002B9  mov    [esp + 0xc], eax
002002BD  mov    eax, [0x5bc508]
002002C2  mov    ecx, [eax + 0x394]
002002C8  mov    [esp + 4], 1
002002D0  mov    [esp], 0x3eef28
002002D7  mov    eax, [esp + ecx]
002002DA  add    esp, 0x10
002002DD  ret
```

The lift is faithful, byte for byte. The function contains **no call
instruction at all**, so it has no callee, and it never reads or writes `esi`.
It cannot be the source. It is reached in the current build
(`[COVERAGE-VA] 0x002002B0`) and `RECOMP_ICALL_WATCH` does not flag it.

There is no manual override for `0x2002B0` either, so the lifted function is
what runs.

## The same signature, from sub_0020B850

`01097498 -> 00000004` — #149's exact before and after values — is reported
against `sub_0020B850`, called from `sub_002235D0+0x1365`. `0x20B850` is also
one of the eleven callbacks `sub_002225B0` pushes, so this is the same table.

Its prologue and epilogue are balanced in the original. `pop edi` sits *before*
the branch and is shared by both exits; each exit then pops `esi` and `ebx`:

```
0020B855  push ebx / push esi / push edi
...
0020B8BA  pop  edi              <- shared by both paths
0020B8BB  jne  0x20b8ca
0020B8BD  mov  ecx, esi
0020B8BF  pop  esi / pop ebx
0020B8C1  jmp  0x2041d0         <- TAIL CALL
0020B8CA  pop  esi / pop ebx / ret
```

The lifter reproduces this correctly, splitting each branch target into a
fragment (`sub_0020B8C6`, `sub_0020B8CA`) that carries the remaining pops.

**The tail call is the attribution trap.** `jmp 0x2041d0` transfers to
`sub_002041D0` — the function ledger #143 pinned as the site of the fatal
`esi = esi - MEM32(eax + 0x20)`. A register comparison taken after
`sub_0020B850` returns is therefore measuring across `sub_002041D0` as well, and
blames the thunk for its callee's damage. The same trap as `sub_002225F0`
earlier in this document: both "violators" are pass-throughs.

### Tail calls are unchecked by both ABI instruments

The lifter emits a tail call as a bare call:

```c
loc_0020B8BD: ;
    POP32(esp, esi);
    POP32(esp, ebx);
    g_seh_ebp = ebp; sub_002041D0(); return;   /* tail jmp 0x002041D0 */
```

Not `RECOMP_ABI_CALL`, so the direct checker never sees it; and not an icall, so
`RECOMP_ICALL_WATCH` never sees it either. That is why `sub_002041D0` — the
function two separate ledger entries identify as the crash site — has never
appeared in an ABI report.

Wrapping tail calls is not obviously right: a tail call legitimately hands its
frame to the callee, so a post-return comparison spans the whole remaining
chain, which is what produces the misattribution in the first place. The useful
change is for a report to say when its target ends in a tail call, so the reader
knows the damage may belong further down.

**Read every ABI-ICALL line as naming a subtree, not a function.** Three of the
reported violators so far — `sub_002225F0`, `sub_0020B850`, and by extension
anything ending in `jmp` — are pass-throughs whose own code is faithful.

## Stepping back: the register hunt was chasing symptoms

Following `sub_0020B850` into what it actually does:

```
0020B850  mov  eax, [0x5bb930]     ; a registry/type pointer
0020B861  push 0x3e226c            ; the string "_data"
0020B866  push eax
0020B867  mov  ecx, esi            ; this = [ [0x5bc2fc] + 0x28 ]
0020B869  call 0x1e8800            ; -> a name lookup: [ecx+0xc] count, [ecx+8] array
```

`0x005BB930` sits in the **BSS tail**, so it starts as zero, and no instruction
in the code section stores to it or takes its address as an immediate. It is
read 32 times. That is the `0x005BB700` shape from
[BLOCKER_005BB700.md](BLOCKER_005BB700.md) a second time.

So it is not one global. Measured across the lifted code:

| | |
|---|---|
| `0x5Bxxxx` globals referenced by lifted code | 1,770 |
| **read but never written by any lifted code** | **613** |
| reads of those globals | 3,123 |

Then, sweeping the XBE's code section for stores and asking whether a writer
exists in the *binary* even though none was lifted:

| store found within | globals covered | still none |
|---|---|---|
| exact address | 60 | 553 |
| ±4 | 360 | 253 |
| ±0x10 | 514 | 99 |
| ±0x40 | 564 | 49 |

Exact matching badly under-counts, and `0x005BB700` is the proof: the blocker
documents its writer as `mov [eax*4 + 0x5bb704], ecx` — an indexed store at a
*different* displacement, so an exact match misses it. Read the ±0x10/±0x40 rows
as the honest ones: **roughly 500 of the 613 sit in structures the binary does
write, from code that was never lifted.**

That reframes this whole investigation. The callee-saved corruption is real and
worth fixing, but it is downstream. The wall is that several hundred globals —
registries, type tables, subsystem pointers — are never initialised, because the
code that initialises them is only reachable through indirect dispatch the
recompiler never followed. Functions then run against NULL registries, produce
NULL or small-integer objects, and those propagate into the register and page-0
symptoms recorded above.

`BLOCKER_005BB700.md` already named the durable fix, and this is the measurement
that says how much it is worth: **treat data-referenced function pointers as lift
roots.** It reached 609 functions when applied to code runs with prologues; the
same idea applied to the initialiser and factory tables is what would populate
these globals.

### Does this need decompiling?

For the immediate mechanical questions — which callee clobbers a register, what
a call site resolves to — no. Runtime instruments (`RECOMP_ICALL_WATCH`, the
page-zero census) answer those faster and more reliably, and twice now they have
corrected a static conclusion.

For *why*, yes, and it paid here: reading `0x3e226c` as the string `"_data"` and
recognising `sub_001E8800` as a name lookup over a `[ecx+8]/[ecx+0xc]` container
is what turned "a callback clobbers esi" into "the type registry was never
built". That question was not answerable from a register trace.
