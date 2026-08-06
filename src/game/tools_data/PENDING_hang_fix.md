# PENDING: the two guards that killed the boot hang (2026-08-06)

These are applied in `gen/recomp_0015.c` RIGHT NOW but **not** recorded in
`manual_edits.json`, so the next `seed_missing_functions.py --apply` will
regenerate over them and the hang comes back. Record them before regenerating.

They are what took the boot from a permanent 8-second hang (716,328,071
indirect calls to one address) to a diagnosable crash, so losing them costs the
single biggest result of the session.

## What was wrong

`sub_0020E520` walks a chain: `node = call(node->fn)`, continue while
`node->0x3C != 0`. Two guards validated the node pointer, set it to 0 when
invalid, and then dereferenced it anyway:

    if (!(esi >= 0x00880000u && esi < 0x04000000u)) { esi = 0; }
    eax = MEM32(esi + 0x3C);        /* <-- reads MEM32(0x3C) */

Guest `0x3C` holds `0x030FEFE8` (a real heap buffer, written there by
`sub_001F3680` through a null container). Non-zero, so the walk loops back and
calls it again. The ICALL is rejected (heap address, not code), the safe stub
returns 0, `esi` becomes 0 again, and the read picks up the same value. The
loop cannot terminate. Measured: all 16 ring-buffer slots identical, 716M
dispatches, watchdog kill every run.

## The change, applied twice in sub_0020E520

An invalid node means the chain is broken, so stop walking. Cannot loop.

Before (both sites identical):

    if (!(esi >= 0x00880000u && esi < 0x04000000u)) { esi = 0; }
    eax = MEM32(esi + 0x3C);

After:

    if (!(esi >= 0x00880000u && esi < 0x04000000u)) { esi = 0; eax = 0; }
    else eax = MEM32(esi + 0x3C);

Site 1 is followed by `if (TEST_Z(eax, eax)) goto loc_0020E53B;`
Site 2 is at `loc_0020E532`, followed by `if (TEST_NZ(eax, eax)) goto loc_0020E530;`
The `pre` field distinguishes them - the guard text alone appears twice.

## Why this needs care rather than a quick append

A `replace_line` record whose `block` ends with a bare `else` lets the existing
`eax = MEM32(esi + 0x3C);` line become the else body, which is the smallest
faithful record. But the tree currently has the edit already applied as a
single `else eax = ...` line, so that regex will not match and
`manual_edits.py` will report it MISSING rather than already-present.

Correct order:
  1. copy `manual_edits.json` first
  2. revert `gen/recomp_0015.c` to pristine (re-run the seeder)
  3. add the two records
  4. `manual_edits.py apply` twice, confirm 141/141
  5. rebuild and confirm no `HUNG` in `signals.py`

## Also uncommitted and NOT recorded

- `recomp_0014.c` carries a temporary `recomp_where("001F7A2B", ...)` probe in
  `sub_001F7930`. Diagnostic only - delete it, do not record it.
- `recomp_0015.c` carries a `recomp_where("0020E532", ...)` probe. Already
  removed when the guard went in; verify.
- 31 stale-flag fixes from `fix_stale_flags.py` are in `gen/` and are
  reproducible by re-running that tool, so they need no record - but the tool
  is not yet wired into the pipeline, so someone must run it.
