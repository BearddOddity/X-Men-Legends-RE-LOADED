# Why does the correct-looking esp-escape fix regress the boot

Status: claimed
Type: task
Blocked by: 01

## This is the live wall (confirmed by ticket 01)

Ticket 01 measured the current terminating crash as an access violation
writing VA `0xFFFFFF03` in `sub_00204800+0x24D`, deterministic 2/2 — the
same wall the 2026-08-08 session was working when it traced the esp escape
to `sub_00204020`. So this ticket is not a stale leftover like 07/08: it
targets what actually stops the boot right now.

Useful detail from ticket 01's triage that wasn't in the original write-up:
`eax=0xFFFFFF00` and `esi=0xFFFFFFFF` are both NULL-derived negatives, so
the faulting write is a null pointer with a small offset added rather than a
wild pointer. Full caller chain: `sub_00204800` → `sub_0020A360` →
`sub_0020DA95` → `sub_002155B0` → `sub_002392E0` → `sub_00239E50` →
`sub_00011E40`.

## Question

The 2026-08-08 session traced a 180-byte esp escape six levels deep to
`sub_00204020`, which captures `_icall_esp` above its callee-saved pushes.
The fix that looks structurally correct — matching every other confirmed
case of this skew — takes `reached` from 101 down to 64 (37 lost, 0 gained),
so it was reverted with a warning comment in the source.

Use `.claude/skills/diagnosing-bugs/SKILL.md` Phase 3 (rank 3-5 falsifiable
hypotheses before touching a probe) to work this: why would a fix that
removes a real, measured skew make the boot *worse* rather than better?
Candidate angles worth ranking: something downstream now reads the
(previously-skewed) value and depends on the old wrong offset; the "correct"
fix is only correct in isolation and a sibling site needs the same fix
simultaneously; or the skew was compensating for a second, still-undiscovered
bug elsewhere in the same call chain.

Resolve with either a landed fix (reached improves or at minimum doesn't
regress, with a second signal per rule #8) or a clear negative result
documented well enough that this doesn't get re-attempted blind.

## Progress 2026-08-08 — ranked hypotheses, and a strong lead already in the tree

Ticket claimed; no build cycle run yet. The lead below was found by reading
and is worth starting from.

### H1 (STRONGEST) — the fix may be CORRECT and `reached` is lying

`src/recomp/recomp_types.h:251-266` documents this exact phenomenon having
already happened once on this project, for a different fix:

> kernel_calls went 82 -> 330 and the backtrace 4 -> 18 frames, while
> g_reached_count FELL 55 -> 42, because the boot stopped re-resolving the
> same vtable slots and started running straight-line code instead.
> **Judged on g_reached_count alone that fix reads as a regression.**

That is precisely the shape ticket 02 is asking about. `g_reached_count`
counts *indirect dispatch targets*, so a fix that stops a retry/re-resolve
cycle lowers it while doing strictly more work. The esp fix removes a real
180-byte skew — exactly the kind of change that could stop a thrash.

**The 2026-08-08 measurement may therefore be incomplete, not wrong.** It
recorded "reached 101 -> 64, 37 lost and 0 gained". What is NOT recorded is
whether `g_callsite_count` (direct call sites — the blind spot `reached`
cannot see) rose or fell, nor whether kernel_calls or crash depth moved.

**Falsifiable test, one build cycle:** re-apply the fix and measure
`callsites`, `kernel_calls`, and the crash site *together with* `reached`.
- callsites UP and/or kernel_calls UP with a deeper crash → H1 confirmed,
  the fix is good and should land; `reached` was the wrong instrument.
- callsites DOWN too → H1 dead, it is a genuine regression; go to H2.

Note the "0 gained" detail cuts slightly against H1 — in the documented
vtable case new code *did* run. Worth weighing, not decisive, because
"gained" was measured on `reached` only.

### H2 — the skew is load-bearing for a second, undiscovered bug

Something downstream reads the skewed value and depends on the wrong offset.
Removing the skew alone then breaks it, and both need fixing together.
Predicts: the post-fix crash appears at a *different* site that reads a stack
slot near the 180-byte delta.

### H3 — a sibling site needs the same fix simultaneously

`sub_00204020` captures `_icall_esp` above its callee-saved pushes; if other
call sites do the same, fixing one desynchronises it from the rest.
Predicts: grep finds sibling capture sites; fixing all together behaves
differently from fixing one.

### Practical note for whoever picks this up

`_icall_esp` is not in `recomp_types.h` and the reverted fix's warning
comment was not located in this session — find it before re-applying, since
it may record detail the progress log omitted. `sub_00204020` appears in
`recomp_0015.c`, `recomp_0016.c` and `recomp_dispatch.c`.

Use `tools_data/diff_reached.py` on the `[COVERAGE-VA]` set rather than
comparing raw counts; baselines are in `tools_data/`.

## Progress 2026-08-08 (later) — H1 tested and DEAD; H2 is now the live lead

Ledger #82. One build cycle each way, deterministic 2/2 throughout.

First, a correction to the note above: callsites **had** already been
measured. Ledger #81's evidence says "reached 101 -> 64 and callsites
357 -> 265" verbatim, and the warning comment in `recomp_0015.c` carries the
same pair. The re-measurement was still worth doing, because the tree has
moved since #81 (RTTI seeding touched `seed_list.json`,
`seeded_functions.json`, `recomp_manual.c`) — but it reproduced #81 exactly.

### The numbers

| signal | baseline | with the fix |
|---|---|---|
| kernel_calls | 44 | 44 (unchanged) |
| failed_icalls | 5 | 2 |
| heap_allocs | 23 | 22 |
| safe_stub | 8 | 8 |
| `reached` | 101 | **64** |
| `callsites` | 357 | **265** |

`diff_reached.py`: 37 lost, **0 gained**.

H1's own criterion was "callsites UP and/or kernel_calls UP with a deeper
crash → confirmed; callsites DOWN too → dead." Callsites went down and
kernel_calls did not move. **H1 is dead.** There is no thrash-stopping
signature here — this is not the `recomp_types.h:251-266` shape.

### But the fix is mechanically correct — that is the useful part

1. **The esp escape is gone.** Baseline crash has `esp = 0x00F8031C`, above
   the `0x00F80000` stack ceiling. With the fix, `esp = 0x00F7FD9C` — a
   healthy in-stack value. The 180 bytes really were this skew.
2. **The crash relocates into `sub_0021ACD0`**, the *direct caller* of
   `sub_00204020` and the function #81 measured as gaining 8 bytes per loop
   iteration. New crash: read at Xbox VA `0xFF011D6C` in
   `sub_0021ACD0 + 0x35B` (`recomp_0016.c` ~2857), `edi = 0xFF011D70`,
   fault is exactly `edi - 4`; `eax`/`ecx`/`edx` all NULL, `esi = 1`.
3. It leaves `sub_00239E50` at `+0x1F9` where the baseline leaves it at
   `+0x390` — **earlier**, so this is genuinely less progress, not a lateral
   move.

**That is H2's predicted shape, not H1's.** H2 predicted "the post-fix crash
appears at a different site that reads a stack slot near the delta". It does,
and the site is inside the immediate caller.

### Concrete next step for H2

The natural candidate at that spot is
`MEM32(ebp + -4) = MEM32(ebp + -4) + 1;` at `recomp_0016.c:2872`
(`loc_0021AD68`) — a **refcount increment on a garbage pointer**. `ebp` is
set two lines earlier from `sub_0020DA80`'s return (`loc_0021AD62`,
`ebp = eax`) or zeroed at `loc_0021AD51`.

**Caveat that matters:** `triage` cannot print `ebp`, because `ebp` is the one
x86 register the lifter models as a per-function C local rather than a global.
An `ebp`-based fault is always reported against whichever *global* register
happens to hold the same value. Treat `edi - 4` and `ebp - 4` as both live
until probed.

With the fix applied, probe `ebp` and `edi` at `loc_0021AD55` /
`loc_0021AD62` / `loc_0021AD68` and find where the refcount pointer comes
from — whether `sub_0020DA80` returns garbage, or whether `ebp` is inherited
wrong (`sub_00204020` publishes `g_seh_ebp` on its tail-jumps).

Sets kept so the next attempt need not pay for the build cycle again:
`tools_data/baseline_reached_pre204020fix.txt` and
`tools_data/after_reached_204020fix.txt`.

Tree reverted and verified back at 44/5/23/8, reached 101, callsites 357,
crash RVA `0xEA904D`.

## Progress 2026-08-08 (later still) — `ebp` probed, and it is INNOCENT

Ledger #83. Fix re-applied purely to reach the crash, then probed. Two build
cycles, deterministic 2/2 throughout, signals identical to the fix alone
(44 / 2 / 22 / 8).

Probes: `[ADEBP62]` before `loc_0021AD64: ;` (i.e. right after
`ebp = eax;`), `[ADEBP68]` after `loc_0021AD68: ;`, then `[AD6B]` after
`loc_0021AD6B: ;` and `[AD72]` after `loc_0021AD72: ;`.

```
[ADEBP62] ebp=011D6000 eax=011D6000 edi=0046B150 esi=00000000 esp=00F7FDAC
[ADEBP68] ebp=011D6000 edi=0046B150 esi=00000000 esp=00F7FDAC
[AD6B]    esi=00000000 ebp=011D6000 esp=00F7FDAC
[ADEBP62] ebp=011D7000 eax=011D7000 edi=0046B154 esi=00000000 esp=00F7FDAC
[ADEBP68] ebp=011D7000 edi=0046B154 esi=00000000 esp=00F7FDAC
[AD6B]    esi=00000000 ebp=011D7000 esp=00F7FDAC
[AD72]    edi=011D6000 esi=00000000 ebp=011D7000
[AD6B]    esi=00000001 ebp=00000000 esp=00F7FD9C
[AD72]    edi=FF011D70 esi=00000001 ebp=00000000
```

### 1. `ebp` is healthy — the refcount lead is dead

`0x011D6000` and `0x011D7000`, both real heap. `MEM32(ebp - 4)` reads live
memory and does not fault. `sub_0020DA80` returns good pointers. Do not
re-chase this.

### 2. The real fault, measured

`eax = MEM32(edi + -4);` at [recomp_0016.c:2886](src/game/src/recomp/gen/recomp_0016.c:2886)
(`loc_0021AD72`), with `edi = 0xFF011D70` → fault `0xFF011D6C`, matching
triage exactly. `edi` is loaded one line earlier by
`edi = MEM32(esi + 0xC)` — and **`esi` is 1**, so that is `MEM32(0xD)`, a
misaligned read of low guest memory that returns garbage instead of faulting
because guest VA 0 is mapped.

**The bug is `esi`, not `ebp`.**

### 3. And `esi` was already wrong on the surviving iterations

Both healthy passes show `esi = 0`. So `edi = MEM32(esi + 0xC)` reads guest
VA `0xC`, and `MEM32(esi + 0xC) = ebp` at `loc_0021ADB4`
([recomp_0016.c:2927](src/game/src/recomp/gen/recomp_0016.c:2927)) **writes**
to guest VA `0xC`. That write is visible in the trace: pass 1 stores
`0x011D6000` there, pass 2's `[AD72]` reads it straight back. `sub_0021ACD0`
has been walking a NULL object the whole loop; only VA 0 being mapped hides
it. The sequence `esi` = 0, 0, 1 is junk, not objects.

`esi` is loaded at `loc_0021AD30` by `esi = MEM32(eax + edx)`
([recomp_0016.c:2844](src/game/src/recomp/gen/recomp_0016.c:2844)).

Signature match worth checking: `esi = 1` consumed as an object pointer is
the same shape as ledger #62's `sub_00205170` crash.

### 4. The fix does stabilise the loop

#81 measured this loop gaining 8 bytes of `esp` per iteration
(`0x00F7FDAC` → `FDB4` → `FDBC`). With the fix, `esp` is steady at
`0x00F7FDAC` across both passes. The final pass at `0x00F7FD9C` is a
separate, deeper invocation — it never hits `[ADEBP62]`, so it took the
`ebp = 0` path at `loc_0021AD51`.

**So H2 is confirmed in substance:** the skew was masking an independent
defect. The fix is correct; what it uncovers is a bad `esi`.

### Probe trap worth recording

`add_probe.py` happily accepted `MEM32(ebp + -4)` as a probe argument. That
would have dereferenced the very address under suspicion, faulting during
argument evaluation *before* `fprintf` emitted — destroying the answer on
exactly the iteration that mattered. It was removed by hand. **Never read the
suspect address inside the probe meant to identify it.**

### Next step

With the fix applied, probe `eax`, `edx`, `ecx` and the resulting `esi` at
`loc_0021AD30` ([recomp_0016.c:2841-2844](src/game/src/recomp/gen/recomp_0016.c:2841))
to find where the junk array comes from, and probe `ecx` at `sub_0021ACD0`'s
entry (thiscall) to see whether `sub_00221F50` hands over a bad object or the
array walk manufactures one.

Tree: probes stripped (13 lines), fix reverted, rebuilt and re-run —
44/5/23/8, reached 101, callsites 357, crash RVA `0xEA904D`.

## Progress 2026-08-08 (final) — the full chain, and a correction

Ledger #84. Probes at `sub_0021ACD0`'s entry, at `loc_0021AD30`, and after
`esi = MEM32(eax + edx);`. Deterministic 2/2, 44 / 2 / 22 / 8.

```
[ACD0ENT] this=01091AB0 esp=00F7FDCC a1=0046B0E8 a2=0046B150 a3=0046B1B8 a4=00000002
[AD30A]   this=01091AB0 edi=0046B150 ebp=FFFFFF98 esp=00F7FDAC
[AD30B]   elem=00000000 base=00000008 off=010906F0 ecx=01096B88
[AD30A]   this=01091AB0 edi=0046B154 ebp=FFFFFF98 esp=00F7FDAC
[AD30B]   elem=00000000 base=0000000C off=010906F0 ecx=01096B88
[AD30A]   this=00000002 edi=005BBEF0 ebp=00000068 esp=00F7FD9C
[AD30B]   elem=00000001 base=00000004 off=01091AB0 ecx=00000000
```

### Correction to the section above

It said the final pass "is a separate, deeper invocation, not loop drift".
**That is wrong.** `[ACD0ENT]` fires exactly **once**, so there is one call to
`sub_0021ACD0` and all three passes are iterations of its loop. The `esp`
difference *is* drift. That was inferred from the `esp` gap without an entry
probe; the entry probe settles it.

### The entry is clean

`this = 0x01091AB0`, a real heap object; args are three `.data` pointers with
a start index of 2. `sub_00221F50` is not handing over garbage.

`ebp = 0xFFFFFF98` at the loop head is also **correct**, not corruption: `edi`
walks the `a2` array (`0x0046B150` → `0x0046B154`, stride 4) and `ebp` is the
delta `a1 - a2`, so `eax = MEM32(edi + ebp)` resolves to `0x0046B0E8` = `a1`.
Standard pointer-difference idiom.

### 1. The container is empty at the indices read — primary defect

`ecx = MEM32(this + 0x28)` = `0x01096B88` (real heap object), `edx =
MEM32(ecx + 8)` = `0x010906F0` (the element array), `eax = MEM32(esp + 0x30)`
= the byte index (8, then 0xC). So the loop reads `0x010906F8` and
`0x010906FC` — **both return `0x00000000`.**

The count guarding the loop is `MEM32(MEM32(this + 0x28) + 0xC)` and it
exceeds the start index 2. **The container claims elements 2 and 3 exist while
its storage holds NULL for both.**

### 2. Why a NULL `esi` does not fault immediately

With `esi = 0`, `edi = MEM32(esi + 0xC)` reads guest VA `0xC` and
`MEM32(esi + 0xC) = ebp` at `loc_0021ADB4`
([recomp_0016.c:2927](src/game/src/recomp/gen/recomp_0016.c:2927)) **writes**
guest VA `0xC`. VA 0 is mapped, so neither faults. Iteration 1 stores
`0x011D6000` there; iteration 2 reads it back as a non-NULL "object" and falls
into the refcount/destructor path at `loc_0021AD72` that iteration 1 skipped.

### 3. A SECOND esp leak — 16 bytes — survives the fix

`esp` at the loop head: `0x00F7FDAC`, `0x00F7FDAC`, `0x00F7FD9C`. A drop of
`0x10` = 16 bytes, and a **leak** (too few pops), not the underflow the
`sub_00204020` fix addressed. It appears only after the iteration that took
the `loc_0021AD72` branch, so it is in that block or one of its calls:
`sub_001F87A0`, `sub_001EBA30`, `sub_0020EEE0`.

`loc_0021AD20` then reloads all three loop variables from stack slots that
`esp` has moved under (`ebp = MEM32(esp + 0x24)`, `ebx = MEM32(esp + 0x14)`,
`esi = MEM32(esp + 0x10)`). `esi` comes back as **2**, and the crash follows
one line later.

**This is a fourth instance of the #46 pattern** — loop variables in stack
slots while `esp` drifts underneath.

**So the `sub_00204020` fix is necessary but not sufficient.** It removes the
8-byte drift (iterations 1 and 2 are now stable, where #81 measured
`FDAC → FDB4 → FDBC`); a second, independent 16-byte leak survives it.
"The skew was load-bearing" is the wrong framing — there was simply another
leak underneath.

### Order of attack

The **NULL container elements** are upstream of everything else: they are why
the leaking destructor path executes at all. If elements 2 and 3 held real
objects, `MEM32(esi + 0xC)` would read a real member instead of guest VA `0xC`
and the mistaken-object branch would not be taken.

Next: find who populates the array at `0x010906F0` owned by `0x01096B88`
(= `MEM32(0x01091AB0 + 0x28)`), and why its count at `MEM32(0x01096B88 + 0xC)`
exceeds the number of elements actually stored.

Tree: probes stripped (10 lines), fix reverted, rebuilt and re-run —
44/5/23/8, reached 101, callsites 357, crash RVA `0xEA904D`.

## Progress 2026-08-08 — write-watch run: one confirmation, one dead end

Ledger #85. Two watch runs with the fix applied, deterministic 2/2 at
44 / 2 / 22 / 8 throughout.

```
[WATCH:elem2] armed on xbox VA 0x010906F8 (4 bytes); current value 0x00000000
[WATCH:va0c]  armed on xbox VA 0x0000000C (4 bytes); current value 0x00000000
[WATCH:elem2] write to xbox VA 0x010906F8 from RIP=0x7FFAD2EAE579 (RVA=0x34462E579) - disarmed
[WATCH:va0c]  write to xbox VA 0x0000000C from RIP=0x7FF78F7C1BDD (RVA=0xF41BDD) - disarmed

[WATCH:count]    armed on xbox VA 0x01096B94 (4 bytes); current value 0x00000000
[WATCH:arrayptr] armed on xbox VA 0x01096B90 (4 bytes); current value 0x00000000
[WATCH:count]    write to xbox VA 0x01096B94 from RIP=0x7FFAD2EAE579 (RVA=0x34462E579) - disarmed
```

### Confirmed: the VA `0xC` scribble is ours

`RVA 0xF41BDD` is inside our module. `sub_0021ACD0` starts at `RVA 0xF416B0`
(from the probe-free crash at `0xF41A0B` being `+0x35B`), so the writer is
`sub_0021ACD0 + 0x52D` = `loc_0021ADB4`, i.e. `MEM32(esi + 0xC) = ebp` at
[recomp_0016.c:2927](src/game/src/recomp/gen/recomp_0016.c:2927). The section
above predicted this from the source; now it is measured.

### Dead end: the watch cannot answer the initialisation question

`RIP 0x7FFAD2EAE579` is identical in both the `elem2` and `count` hits, and
resolves to `+0x34462E579` against image base `0x7FF78E880000` — far outside
our module, i.e. a system-DLL memset. That is **our own heap zero-fill**
(`xbox_HeapAlloc` / `xbox_HeapAllocAt`, ledger #76) touching each page as the
object is allocated.

The watch fires **once then disarms**, and it is **page-granular**. So on any
freshly allocated object the one shot is always spent on the zero-fill, and
every later game write is invisible.

**`arrayptr` never firing is not evidence of anything.** `0x01096B90` and
`0x01096B94` are four bytes apart and share page `0x01096000`; the single
fault was credited to `count` and cleared the page for both. We know
`arrayptr` was written afterwards — the probes above read `0x010906F0` out of
that slot. **Do not record a silent watch as a negative result.**

**Rule:** a write-watch answers *"who corrupted this live page"*, not *"who
initialised this heap field"*. Use a probe at the suspected writer instead.

### Better next step, cheap because of the RTTI work

Read the vtable pointer `MEM32(0x01096B88)` and look it up in
`tools/ghidra_naming/rtti_names.json` to get the **class name**, then find and
probe that class's element-add method. That turns an anonymous container into
a named one.

A probe at `sub_0021ACD0`'s entry printing `MEM32(this + 0x28)`,
`MEM32(... + 8)` and `MEM32(... + 0xC)` also pins the count exactly, in one
build cycle.

Tree: fix reverted, rebuilt and re-run — 44/5/23/8, reached 101, callsites
357, crash RVA `0xEA904D`, no probes anywhere.

## Progress 2026-08-08 — ROOT CAUSE REACHED (ledger #86-#88)

Five build cycles, all deterministic at 44/5/23/8, tree returned to baseline
after each. Two of my own hypotheses died on the way; both are recorded so
they are not retried.

### The chain, end to end

1. `sub_00221F50` reads the container's count as a **start index**, then calls
   `sub_00209470` to add N elements.
2. `sub_00209470` calls a factory per element. **The factory is FINE** —
   probes at `loc_002094CF` returned real objects every call (`0x010902A0`,
   `0x010902D8`, `0x01090310`, …). *"A failed icall returns 0 so NULL gets
   stored"* is **REFUTED**.
3. Trace-mode watch on the count field (`0x01096B94`) showed it climbing
   `1,2,3…0x16` from `RVA 0xECA1D2` = `sub_00209470 + 0x7C2`. **27 elements
   really were added — the count is honest, not corrupt.**
4. Yet the container ends with count 27, a 4-slot buffer, **all slots NULL**.

### Why: the grow wipes the array every time

`sub_00202B87` asks the allocator for the current block size via
`ICALL vtable+0x144`, `>> 2` for a capacity, remembers that as the **old**
capacity, and after growing zeroes only from old-capacity onward.

**The query returns 0 on all ten grows.** So old capacity reads 0, the
zero-run starts at **slot 0**, and the whole array is wiped on every add.

The decisive trace — slots 2/3 across grows, with the buffer **not** moving:

```
#4 PRE   oldbuf=0x01096BA0  s2=0x010902A0     element present
#4 POST  newbuf=0x01096BA0  s2=0x010902A0
#5 PRE   oldbuf=0x01096BA0  s2=0x00000000  s3=0x010902D8   erased IN PLACE
```

Exactly one survivor per grow (`#3` s1, `#4` s2, `#5` s3). Because the buffer
did not move, *"the realloc fails to copy"* is also **REFUTED**.

Capacity 0 additionally makes `if (count < capacity) skip grow` never true, so
it grows on **every** add — 12 grows for 27 adds. Two symptoms, one cause.

### Why the query returns 0: it is never called

`sub_001E8E20` (*"which allocator owns this pointer?"*,
[recomp_0014.c:773](src/game/src/recomp/gen/recomp_0014.c:773)) walks registries
`0x5BC53C` then `0x5BC538`, asking each entry `vtable+0x78` then
`vtable+0x148` (*"is this yours?"*). If nothing claims the pointer it falls to
`loc_001E8E9C` and returns **NULL**.

`sub_00202B87` does not check. With `eax = 0`, `edx = MEM32(0)` reads guest
VA 0 — **which is mapped** — so it yields 0 rather than faulting; the icall
target becomes 0, the call fails, and `eax` is left 0 as the "block size".

**Measured `owner=0x00000000` on all ten grows.** This also accounts for the
run's failed-icall target `0x00000000`.

### The sharp question now

**Why does no registered allocator claim a buffer that an allocator in that
same registry handed out?** The constructor `sub_00202C70` allocates it
through registry `0x5BC538` via `vtable+0xCC`.

Next probe: inside `sub_001E8E20`, print `MEM32(reg + 4)` (entry count) for
both registries and the per-entry `vtable+0x78` / `vtable+0x148` results —
does the walk run zero times, or run and get answered false?

`0x5BC538`/`0x5BC53C` are the same registry pair ledger #36/#43/#44 found NULL
and repaired. **Verify they are fully POPULATED now, not merely non-NULL.**

### Dead end closed (#86)

The container's class has **no RTTI** — vtable `0x003EEF28` has a zero COL
pointer at `vtable[-1]`, confirmed by reading the XBE directly. So ledger
#85's "look the class name up in `rtti_names.json`" cannot work. Not a parser
bug; the compiler never emitted RTTI for this class. Slot 0 is `sub_0020DA40`
(destructor, releases `+8` through the registry) and three of its first eight
slots are the shared no-op `0x0015FDF0`.

### Worth its own ticket: guest VA 0 is mapped

This bug and the VA `0xC` read/write in #84/#85 both stayed silent **only**
because page 0 is readable and writable. An opt-in debug build leaving guest
page 0 unmapped would turn every such null deref into an immediate,
precisely-located fault — likely collapsing this investigation from five build
cycles to one.

## Progress 2026-08-08 — CONTAINER BUG FIXED (ledger #89). The cause was ours.

`sub_001E8E20` walks two registries. Registry A (`0x5BC53C`) is **empty** (its
loop probe never fired); registry B (`0x5BC538`) holds exactly **one** entry,
`0x01088A90`.

A **manual guard this project added** threw it away:

```c
eax = MEM32(esi);                                            /* the VTABLE */
if (!(eax >= 0x00880000u && eax < 0x04000000u)) goto skip;   /* HEAP range */
```

Vtables live in the image, far below the heap. Measured
`vtable=0x003F4770  passes_heap_guard=0` on all eight calls, so the walk
skipped its only entry and returned NULL every time — which is #88's whole
chain.

**Fix:** widen to `0x00010000u .. 0x04000000u` at both sites (`loc_001E8E41`,
`loc_001E8E7F`). Still rejects small ints and wild pointers, including the
`0xBEEF0001` fake thread handle the guard's own comment documents. Also
collapsed **eight** duplicate copies, four jumping to the other walk's label.

**It fixed its target:**

| | before | after |
|---|---|---|
| container slots | `0, 0, 0, 0` | `0x01096C60, 0x01096CA8, 0x01096D20, 0x01096D58` |
| heap allocs | 23 | **49** |
| failed icalls | 5 | 4 |
| safe stubs | 8 | 6 |

A second container also appears (`this=0x01097748`, count 4) — structures the
boot never built before.

**Coverage is mixed, stated plainly:** `reached` 101 → 86, callsites 357 → 267,
**22 gained / 37 lost**. The 22 are a real new chain and most appear in the new
backtrace. The 37 are largely the `0x0022xxxx`/`0x0023xxxx` band ticket #77 won
plus the five callbacks `sub_00227F90` registers.

**Kept**, on the #72 precedent (4 gained / 17 lost, kept because provably
correct, vindicated by #76). The deciding difference from #81 — reverted — is
that #81 gained **zero**. And a vtable is not a heap pointer, so the guard
being wrong is not a judgement call.

Baselines saved: `tools_data/baseline_reached_101.txt` (pre-fix),
`baseline_reached_guardfix.txt` (post-fix).

### New wall — outside our module

`VCRUNTIME140.dll` (a `memcpy`), reading Xbox VA `0xFFFFFFE4` (NULL-derived),
from `sub_00342AA0+0x52C` ← `sub_001F87A0+0x3EE` ← **`sub_0021ACD0+0x5C7`**.
That loop previously died at `+0x35B`.

`sub_00239E50` sits at `+0x1F9` where the pre-fix baseline reached `+0x390` —
outer sequence stops earlier, inner path goes deeper. Same shape as #82.

### Method note worth keeping

Localised by a probe that fired (`[L:RAW2]`) next to one that **never** did
(`[L:B_PRED]`). The only code between them was the guard. **Silence between two
probes is evidence.**

## Answer

<!-- filled on resolution -->
