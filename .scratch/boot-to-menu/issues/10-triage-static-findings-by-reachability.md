# Triage the static findings by whether the boot reaches them

Status: open
Type: task
Blocked by: 09

## Question

Graduated from fog by ticket 09, which established the sharper framing.

Three static sweeps report a large, near-constant finding set:

- `unwritten.py --min-readers 3` — **570** globals read but never written
- `faithful.py --sweep 0` — **1097** functions with findings (of 30005)
- `recon.py` — **13871** orphans, **5685** ghosts

These are static properties of the generated code, not of the run. Ticket 09
confirmed they barely move when the boot advances (570 → 570 across a session
that took reached 55 → 101), so "wait until the boot moves" was never a valid
reason to defer them and re-running the sweeps will not sharpen them further.

What is missing is a **relevance filter**. 1097 findings is unusable as a
worklist; the subset sitting in code the boot actually executes is likely
small and is where a real defect would show up first.

Build that cross-reference: intersect each sweep's findings against the
`[COVERAGE-VA]` set from the live run (101 entries) plus the direct-callsite
set (357). Report per sweep how many findings are reachable-now, and rank
those. `tools_data/diff_reached.py` already parses the coverage set and is
the natural place to start rather than writing a new parser.

Expect the reachable subset to grow as the boot advances, so this wants to
be a re-runnable tool, not a one-off list — rule #4 (build a tool when a
task is done by hand twice) applies.

Note for whoever takes this: `faithful.py` persists nothing, printing to
stdout only, so the tool will need to capture its output rather than read a
report file.

## Answer

<!-- filled on resolution -->
