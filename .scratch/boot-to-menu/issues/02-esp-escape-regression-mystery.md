# Why does the correct-looking esp-escape fix regress the boot

Status: open
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

## Answer

<!-- filled on resolution -->
