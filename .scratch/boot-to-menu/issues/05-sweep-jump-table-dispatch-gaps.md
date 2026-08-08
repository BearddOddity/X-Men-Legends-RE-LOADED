# Sweep RECOMP_ITAIL jump-table targets missing from recomp_dispatch.c

Status: open
Type: task
Blocked by: 01

## Question

Confirmed once: `sub_001F83D0`'s switch dispatches via
`RECOMP_ITAIL(MEM32(esi*4 + 0x1F84AC))`, but none of its 7 jump-table
entries were registered in `recomp_dispatch.c` — the lifter emitted the
switch cases as unreachable blocks but never wired them in. Seeding the one
entry actually taken (`0x001F840B`) moved reached 47→54. Four more entries
at that same table (`0x001F83EF`, `0x001F83F6`, `0x001F83FD`, `0x001F8404`)
are known-unseeded and will fail identically when their index comes up.

This is flagged as mechanically sweepable: any `RECOMP_ITAIL` whose
jump-table targets are absent from `recomp_dispatch.c` has this bug. Write
or reuse a checker over the whole generated tree, seed every gap found
(starting with the four already-known ones at 0x001F84AC), and measure.

## Answer

<!-- filled on resolution -->
