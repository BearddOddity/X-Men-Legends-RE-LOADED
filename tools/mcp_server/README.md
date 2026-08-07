# XboxRecomp MCP server

Exposes the recompilation engine to tooling — including Claude — the same way
ReVa exposes Ghidra.

## Why

Before this, every interaction with the engine went through shell commands and
regexes over `stderr.txt`. That cost real mistakes:

- a C string literal built through a shell heredoc arrived with a mangled
  escape and broke the build — the exact trap `CLAUDE.md` documents
- an `add_probe.py --limit` silently truncated a log and hid the allocation
  failure being hunted
- several tools each re-parsed the same log with their own regexes

These tools return structured data and wrap the scripts in
`src/game/tools_data/` that already work, so there is one source of truth for
what a number means.

## Tools

| tool | purpose |
|---|---|
| `build` | compile; returns errors only, not the whole log. Fails fast if an AFK tool holds the build lock |
| `run` | run N times, report signals, spread and determinism. Same lock check |
| `triage` | how the last run ended — crash site or hang details |
| `progress` | read the history (`tail=N`, default 10 — the full history can exceed 150K chars), or record a verified result |
| `function` | lifted C for a function, by name or address |
| `search` | regex over generated code, no shell quoting |
| `probe` / `strip_probes` | probes without escaping hazards. Lock-checked — walls.py's snapshot/restore would otherwise silently discard a mid-run probe |
| `log` | grep the last run's stderr |
| `afk_start` | launch `walls`/`overnight`/`investigate` in the background - returns at once, does not wait. Refuses to stack a second launch over a still-running one |
| `afk_status` | is the last-launched AFK process still alive, who holds the build lock |
| `afk_stop` | stop the last-launched AFK process. `force=True` is required in practice — the non-forced path always fails for these detached processes, and a forced kill skips the tool's own gen/ cleanup, so check `git status` on gen/ afterward |
| `walls_report` / `overnight_report` / `investigation_report` | read that tool's last report without running it |
| `faithful` | compare generated C against the original x86, one function or a sweep |
| `ledger` | check/record disproven or confirmed theories - `check` before starting a new thread. Now also written to by `overnight.py` (one entry per night) and cross-checked by `investigate.py` (flags a finding that matches a prior REFUTED claim). Matching uses `ledger.similar()`'s identifier-aware check (exact `sub_XXXXXXXX`/`0xADDR` match bypasses the fuzzy word-overlap threshold, which missed a real case at 0.27 against 0.34) |
| `deepdive` | everything already known about one function in one call — lifted C location, `faithful.py` verdict, **every ledger entry naming it**, hand-proven guards, walls.json record, callers, struct-field layout (read vs written), globals, indirect-call targets, loops, live probes, and mentions in the project write-ups. Call it **before** starting new analysis |
| `walls_knowledge` | walls.json, structured - every known wall, whether it's bypassed/exhausted, and its faithful.py verdict if checked |
| `patterns` | walls.py's PATTERNS library - name, summary, proven-for/candidate-for wall kinds, and promotion status, without reading the source |
| `manual_edits` | the 139 hand-proven guards in manual_edits.json - a per-function tally by default, or one function's full edit(s) with `function=` |
| `bisect_journal` | per-experiment history from overnight.py/bisect_core.py, summarized (candidate subsets can be dozens of addresses - reports `subset_size`, not the raw list) |

`walls.py` now checks `faithful.py` on a wall's own function once, before
trying any bypass on it (`faithful_check_once` in `walls.py`) - a real
lifter defect (dropped branch label, stale-flag read) is reported and
ledgered separately from the bypass, so "we got past it" and "we found the
actual bug" don't get conflated. Needs `capstone` under the SAME interpreter
this MCP server runs under (Python 3.12) - it was previously only installed
for `py -3`, so anything launched via `afk_start`/`_py()` (which inherit the
MCP server's own interpreter) silently couldn't use `faithful.py` until this
was fixed.

All build/run/write tools that touch `gen/*.c` or `stderr.txt` share one
lock (`recomp_lock.py`) with `walls.py`/`overnight.py`/`investigate.py`/
`bisect_core.py` — calling one while an AFK tool is running fails fast with
`busy: true` instead of racing it. `ledger.json` has its own separate lock
(`ledger.locked()` in `ledger.py`) since it can be written by any of the
above independently of the build lock.

`build`, `run`, `progress(record=…)`, `probe` and `strip_probes` take
`force=True`. The lock has two layers: a **PID lockfile** (a real holder) and
an **activity heuristic** (“`stderr.txt`/`seed_list.json` changed in the last
300 s”). `force` bypasses **only the heuristic** — a held lockfile still
refuses — so it can never stomp a running AFK tool. It exists because an
interactive edit→build→run→edit loop trips the heuristic on its own exhaust;
the alternative was idling five minutes or shelling around the tool, and
shelling around it is how a stale binary got measured for two cycles. The
`busy` response now carries a `hint` telling you when forcing is the right
call.

**Shelling out to `build_compile.bat` from Git Bash is a trap**: bare `cmd /c`
gets its `/c` path-mangled by MSYS and silently opens a shell that exits
without building, leaving the previous binary in place. Use the MCP `build`
tool. If you must shell out, it is `cmd //c` with an **absolute** path, and
check for `Linking C executable` in the output rather than trusting `$?`
after a pipe.

## Pairing with ReVa

`function("0x001A3554")` gives the lifted C; ReVa's `get-decompilation` on the
same address gives Ghidra's ground truth. Diffing those two is what found the
1,065 dropped fall-through edges.

## Registered as

```
claude mcp add --scope user XboxRecomp -- py -3.12 "D:\My Games\Xbox Recomp\tools\mcp_server\__main__.py"
```

Absolute path on purpose: an MCP server inherits the client's working
directory, so `-m tools.mcp_server` would only work when launched from the repo
root. The module locates the repo from `__file__` instead.

Needs the `mcp` package under Python 3.12 (same interpreter as PyGhidra).

**Restart Claude Code to pick it up** — MCP servers bind at session start.
