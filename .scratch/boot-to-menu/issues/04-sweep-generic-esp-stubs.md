# Sweep generic g_esp+=4 stubs that are tail-jump targets of push-heavy functions

Status: open
Type: task
Blocked by: 01

## Question

Confirmed twice: a generic `g_esp += 4` stub is only correct for a plain
`ret` with no callee-saved pushes. `sub_001E6CC9` (12-byte leak, reached
43→47) and the pair `sub_00011B2B`/`sub_001E9558` (36-byte leak, part of the
2026-08-08 40-byte-leak fix) were both this bug. `sub_001E6C5B` was seeded
too and was neutral (strictly more faithful, no measured regression).

Sweep `recomp_stubs_unresolved.c` for stubs that are tail-jump targets of
functions with `push` prologues — the pattern is mechanical (a function
pushes N registers, its real exits restore N bytes, but a stub reachable via
tail-jump only restores 4). Seed each one at a time via
`seed_missing_functions.py --va` (additive, no regeneration) and measure per
rule #7.

## Answer

<!-- filled on resolution -->
