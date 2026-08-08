# Sweep remaining deferred-flag miscompiles

Status: open
Type: task
Blocked by: 01

## Question

One instance of this class (0x001F0AB2: `cmp ecx, eax` dropped to a no-op,
re-tested after `ecx` was clobbered) was worth reached 37→43 on 2026-08-07.
`faithful.py` detects this pattern by name and the last walls.py static scan
found 1094 functions with findings, a nonzero-flags subset of which are this
exact class (e.g. `sub_00129F4E` at `recomp_stubs_unresolved.c:1043` shows
1 flag finding).

Run the sweep: use `faithful.py` / the recon findings
(`recon_findings.json`) to enumerate every function with a flags-mismatch
finding, triage which are reachable from the current boot path, fix them one
at a time per rule #7 (one behaviour change per build), and record which
ones moved `reached` vs which were inert (unreached code, correctly fixed
but no measurable signal yet).

## Answer

<!-- filled on resolution -->
