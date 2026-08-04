# Scoping: `fs:`-segment state and the corrupt SEH chain

Status: **scoping only, nothing implemented.**
Written 2026-08-04, at 200 kernel calls / 6 failed icalls / deterministic.

---

## The headline correction

I previously called this "the per-context SEH fix". **That framing is probably
wrong, and the scoping is what showed it.**

`PsCreateSystemThreadEx` is called **once** in the current boot. There is one
logical thread. So giving each thread its own TIB would fix nothing here — the
corruption happens *within* a single thread.

Anything below that assumes multi-threading is deferred, not required.

---

## What is actually broken

`_except_handler3` is entered with garbage arguments:

```
ExcRec=00000000  EstFrame=00000000  Ctx=001A1C23  Disp=00000000
ExcRec=FFFFFFFF  EstFrame=00F7FF98  Ctx=00F7FF2C  Disp=00F7FF10
```

`Ctx = 001A1C23` is the XAPI startup **function address**. A function pointer
cannot be a ContextRecord, so these are not SEH arguments — they are stack
garbage. And `0xFFFFFFFF`, the end-of-chain marker, arrives as an
ExceptionRecord.

Every call shows the chain head `fs:[0]` reading **`0x000000FF`**.
`xbox_memory_layout.c` initialises it to `0xFFFFFFFF`.

Ruled out already:

- The single `MEM8(0)` site is in `sub_00067DF0`, which is **never reached**.
- A byte write of `0xFF` over `0xFFFFFFFF` leaves `0xFFFFFFFF` anyway.

So a **dword** write placed it there. The only writer on that path is
`__SEH_epilog`'s `MEM32(0) = ecx`, where `ecx = MEM32(ebp - 16)` — i.e. a
corrupt frame. **The loop is self-reinforcing:** corrupt frame → corrupt chain
→ corrupt frame.

---

## Why `fs:` is involved at all

The lifter **drops segment prefixes on memory operands**. It handles segment
*register* moves (`mov fs, x`) but a prefixed operand like `mov eax, fs:[0]`
becomes a plain absolute `MEM32(0)`.

The port's standing workaround is a fake TIB mapped at VA 0, so those absolute
accesses land on something. That is a deliberate, documented trade — not an
accident — and it works until a field needs real semantics.

Measured surface: **2,660 sites, 10 distinct offsets.**

| offset | sites | field |
|---|---|---|
| `fs:[0x00]` | 2,625 | SEH exception-list head |
| `fs:[0x04]` | 9 | TLS slot array |
| `fs:[0x28]` | 8 | RW engine context |
| `fs:[0x20]` | 7 | KPCR Prcb |
| `fs:[0x24]` | 5 (byte) | — |
| `fs:[0x0C]` | 2 | — |
| `fs:[0x54]`, `fs:[0x1C]`, `fs:[0x14]`, `fs:[0]` byte | 1 each | — |

`fs:[0x00]` is **98.7%** of all of it. Whatever is done here is really a
decision about one field.

SEH surface: **63 `__SEH_prolog` call sites, 58 `__SEH_epilog`.**

---

## Options

### A. Fix the frame corruption at source *(recommended first)*

The chain head only goes bad because an epilog wrote a bad value, and it wrote
a bad value because `ebp` was wrong. That is the same class already fixed twice
this session — dropped fall-through edges, and the missing `g_seh_ebp` publish.

- **Cost:** low. Diagnostic work, then a targeted lifter change.
- **Risk:** low. Precedent exists; both prior fixes held.
- **Payoff:** unknown but plausibly large — removes the cause rather than the
  symptom.
- **How to start:** probe `ebp` and `MEM32(ebp-16)` at every `__SEH_epilog`
  call site, find the first one that writes a non-pointer, and work back.

### B. Validate the chain head on write

Make `MEM32(0) = x` reject values that cannot be a registration record
(`< 0x10000`, not in the stack range, not `0xFFFFFFFF`).

- **Cost:** very low, a few lines in the epilog.
- **Risk:** **it is another guard.** This session produced seven confident
  fixes that measurement rejected, and the existing `sub_003433B1` guard is
  already masking this exact corruption. Stacking a second mask makes the real
  fault harder to find, not easier.
- **Payoff:** may buy kernel calls; will not fix anything.
- **Verdict:** only as a diagnostic that *logs* and does not alter behaviour.

### C. Emit real TIB-relative accesses in the lifter

Teach the lifter to keep the segment prefix: `fs:[N]` becomes
`TIB32(N)` against an explicit per-context TIB base, rather than absolute
`MEM32(N)`.

- **Cost:** high. Lifter change plus a full regeneration (~20 min) plus
  re-placing manual edits — the last regeneration lost 33 of 200 guards.
- **Risk:** high. Touches 2,660 sites at once.
- **Payoff:** correctness. Required eventually, and the only option that makes
  multi-threading possible.
- **Verdict:** right destination, wrong next step. Do A first; if A shows the
  corruption is genuinely inherent to sharing one TIB, C becomes justified.

### D. Per-thread TIB

**Deferred.** One thread exists. Revisit when `PsCreateSystemThreadEx` is
called more than once, or when the thread model stops running bodies inline.

---

## Recommendation

**A, then re-measure. Do not start with C.**

The evidence points at frame corruption, not at TIB sharing. C is a large,
irreversible-feeling change justified by a premise that scoping has just
weakened.

## How we will know it worked

- `fs:[0]` holds `0xFFFFFFFF` or a plausible stack address at all times
- `_except_handler3` receives an ExceptionRecord that is not `0` or `0xFFFFFFFF`
- The `sub_003433B1` guard stops firing — it is currently worth 156 kernel
  calls (200 vs 44), so it must keep earning that until the cause is gone
- `distinct_ordinals` moves past 15, the spin-proof breadth signal

## What not to do

Do not remove the `sub_003433B1` guard before the cause is fixed. Disabling it
alone reproduces the entire regression: 200 → 44 kernel calls, heap 49 → 2.
The other thirteen heap-range guards are inert and can go, but only once the
boot advances past them.
