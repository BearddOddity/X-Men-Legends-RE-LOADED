# Diagnose crash@sub_0020E547

Status: resolved
Type: task
Blocked by: 01, 09

## Answer — DEAD, resolved downstream

Closed by ticket 09 without being worked. `grep` for `0020E547` against the
current run's `stderr.txt` returns zero matches: this crash does not occur
in the current build.

The live terminating crash is `sub_00204800+0x24D` writing VA `0xFFFFFF03`,
already covered by ticket 02 — so no replacement ticket is needed.

Which 2026-08-08 fix killed this one is not established. The candidates are
the base_hint/`xbox_HeapAllocAt` fix or one of the four seeded functions;
distinguishing them would cost a bisect, and there is no open question
riding on the answer, so it is left unattributed rather than guessed at.

## SUSPECT — probably stale, verify before working this

Ticket 01 established that this ticket's source (`walls_report.md`, Aug 7
11:11) predates the 2026-08-08 fixes by ~12 hours. The live crash measured
deterministically 2/2 is at **`sub_00204800+0x24D`** writing VA
`0xFFFFFF03`, not at `sub_0020E547`.

**First action on this ticket is to confirm whether this crash still occurs
at all.** Re-run walls.py against the current build. If `sub_0020E547` no
longer appears, close this as resolved-downstream. If it appears but is no
longer the terminating crash, re-scope this ticket to whatever role it
actually plays now.

Note the live wall (`sub_00204800+0x24D`) is not currently ticketed — if
this ticket turns out to be dead, the replacement ticket should target the
live crash instead.

## Question

Seen 2 times, nothing tried yet. Deepdive already captured: lifted C at
`recomp_0015.c:39091` (83 lines), 1 indirect call site
(`MEM32(edx + 0xCC)`), 2 hand-proven guards already applied here, callers
are all `sub_0020E520` (repeated — vtable calls invisible to static
analysis so the real caller diversity may be wider).

Untouched territory — start with Phase 1/2 of `diagnosing-bugs` (confirm the
feedback loop reproduces this exact crash deterministically, not a
neighbour) before hypothesising.

## Answer

<!-- filled on resolution -->
