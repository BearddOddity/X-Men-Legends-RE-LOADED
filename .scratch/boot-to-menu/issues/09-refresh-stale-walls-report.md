# Re-run walls.py to refresh the stale report

Status: resolved
Type: task
Blocked by: 01

## Question

Ticket 01 established that `walls_report.md` (Aug 7 11:11) predates the
2026-08-08 fixes by ~12 hours, so everything it says describes a build that
no longer exists. Three separate parts of this map are currently reasoning
from it:

- ticket 07 (`spin@sub_001F7930`) — almost certainly dead; the live run
  measures 2,117 dispatches where a spin was 677M
- ticket 08 (`crash@sub_0020E547`) — live crash is `sub_00204800+0x24D`
  instead
- the fogged static-findings triage (570 never-written globals, 1094
  faithful.py findings) in the map's **Not yet specified**

Re-run walls.py against the current build so all three are reasoning from
real data. Check `afk_status` first — walls.py holds the build lock, so it
must not race an interactive build/run.

Resolve by recording: which walls still reproduce, which are gone (and
which landed fix most plausibly killed each), and whether the refreshed
static findings are now worth graduating out of fog.

Note the run this refreshes was launched with `--hours 0.167` (10 minutes)
and reported "Ran 0 minutes / No progress", so pick a duration that actually
lets it work rather than reproducing a zero-length run.

## Answer

Resolved **without** running walls.py, deliberately. Two reasons, both
worth carrying forward:

1. Both known walls are marked `exhausted: true` in `walls.json` — walls.py
   has no pattern left to try on either, so a grind pass would have spent
   hours re-confirming that.
2. walls.py applies **scaffolding** bypasses to generated code (rule #11 —
   measurement only, never a fix). The working tree currently holds the
   best-ever uncommitted state (reached 101). Spending that risk to answer a
   question two cheaper read-only checks could answer was the wrong trade.

Instead: one grep against the live log for the wall question, and the three
static sweeps run directly (walls.py invokes exactly these, read-only, at
`tools_data/walls.py:859-866`).

### Both stale walls are DEAD — confirmed

`grep` for `001F7930|0020E547` against the current run's `stderr.txt`
(Aug 8 03:33) returns **zero matches**. Neither wall occurs in the current
build at all.

- **`spin@sub_001F7930`** — gone. Corroborated independently: the live run
  measures 2,117 indirect-call dispatches; a spin was 677,120,348. The
  2026-08-07 entry predicted this ("replacing the CRT allocator ... the
  registry corruption and the `sub_001F7930` spin should both be
  downstream-fixed"). That prediction is now confirmed.
- **`crash@sub_0020E547`** — gone. Live terminating crash is
  `sub_00204800+0x24D`.

Tickets 07 and 08 both closed as resolved-downstream on this evidence.

Note `walls.json` still lists both walls. Left un-edited on purpose — it is
walls.py's own state file and the tool re-detects walls per run; hand-editing
a tool's knowledge store to match a conclusion it did not reach is how that
store stops being trustworthy.

### Static sweeps refreshed — and the fog note's reasoning was WRONG

| sweep | stale (Aug 7 11:11) | now | delta |
|---|---|---|---|
| `unwritten.py --min-readers 3` | 570 globals | **570** | **0** |
| `faithful.py --sweep 0` | 1094 findings / 29996 fns | **1097 / 30005** | +3 / +9 |
| `recon.py` | 13864 orphans, 5683 ghosts | **13871 / 5685** | +7 / +2 |

**The findings barely moved, and that is the actual result.** The +9
functions are exactly the newly-seeded ones; every delta is downstream of
that. The globals count did not move by even one.

This corrects the map's fog note, which justified deferring these as
"generated from a stuck boot, so most are probably unreached-yet code."
That reasoning was wrong. These sweeps read the **generated code**, not the
run — they are static properties of the lifted output and do **not** go
stale when the boot advances. Unlike the wall list, they never needed
refreshing on those grounds.

What actually gates their relevance is different and sharper: **which
findings sit in code the boot reaches.** That is a cross-reference of the
findings against the `[COVERAGE-VA]` set, and it is specifiable now — so the
fog graduates into ticket 10 rather than waiting on anything further.

### Handover detail for ticket 03

`faithful.py` persists nothing — it prints to stdout only, no JSON or report
file. Ticket 03 needs its own `--sweep 0` pass filtered on the **`flags`**
column, not `labels`. The distinction matters: the highest-`labels` entries
are all `flags 0` and all sit in `recomp_stubs_unresolved.c`, i.e. dropped
branch edges inside functions that were never lifted — a different class
from the deferred-flag miscompile ticket 03 targets. Only `flags > 0` is
that class (e.g. `sub_00129F4E`, flags=1).
