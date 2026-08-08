# Break the spin@sub_001F7930->0x00000000 wall

Status: resolved
Type: task
Blocked by: 01, 09

## Answer — DEAD, resolved downstream

Closed by ticket 09 without being worked. `grep` for `001F7930` against the
current run's `stderr.txt` returns zero matches: this spin does not occur in
the current build.

Corroborated independently — the live run measures 2,117 indirect-call
dispatches, where this spin was 677,120,348.

The fix that killed it was almost certainly replacing the CRT
`RtlAllocateHeap` with the native heap (2026-08-07, `df8fd7a`). That entry
predicted exactly this outcome at the time: "the registry corruption and the
`sub_001F7930` spin should both be downstream-fixed." Confirmed.

No further work needed. Both scaffolding attempts that failed on it
(iteration-cap, heap-range-guard) failed because the wall was a *symptom* of
the allocator, which is precisely why neither bound helped.

## SUSPECT — probably already fixed, verify before working this

Ticket 01 established that this ticket's source (`walls_report.md`, Aug 7
11:11) predates the 2026-08-08 fixes by ~12 hours. Two independent signals
say this spin no longer happens:

1. The live run measures **2,117 indirect-call dispatches**. A spin was
   677,120,348. There is no spin in the current build.
2. The 2026-08-07 progress entry predicted exactly this: replacing the CRT
   allocator "settles ledger #22 ... so the registry corruption and the
   `sub_001F7930` spin should both be downstream-fixed."

**First action on this ticket is to confirm it is dead, not to work it.**
Re-run walls.py against the current build and check whether this wall still
appears. If it does not, close this ticket as resolved-downstream and record
which fix killed it. Only investigate if it actually reproduces.

## Question

Seen 4 times. Two scaffolding attempts already failed on it (iteration-cap,
heap-range-guard) — walls.py's own note says "no known pattern matches this
shape; needs a human to look." Deepdive is already captured: lifted C at
`recomp_0014.c:38743` (188 lines), 2 indirect call sites
(`MEM32(eax + 0x64)`, `MEM32(eax + esi * 4)`), 2 backward branches (the spin
is in one of them), reads global `0x5BC508`, callers include
`sub_0019FBC7`, `sub_001F7922`, `sub_00209610`, `sub_002235D0`.

Since scaffolding has failed twice, this needs the deeper read: use
`diagnosing-bugs` Phase 3 (ranked falsifiable hypotheses) rather than another
clamp/guard attempt — per its own framing, "every bypass is scaffolding, not
a repair" and two failures on the same shape means the fix is structural,
not a bound.

Note: may turn out to depend on ticket 02 (the esp-escape mystery) if this
spin's garbage object pointer traces back to the same corrupted-frame
family. Check that before assuming independence.

## Answer

<!-- filled on resolution -->
