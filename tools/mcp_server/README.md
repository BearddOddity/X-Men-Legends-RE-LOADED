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
| `build` | compile; returns errors only, not the whole log |
| `run` | run N times, report signals, spread and determinism |
| `triage` | how the last run ended — crash site or hang details |
| `progress` | read the history, or record a verified result |
| `function` | lifted C for a function, by name or address |
| `search` | regex over generated code, no shell quoting |
| `probe` / `strip_probes` | probes without escaping hazards |
| `log` | grep the last run's stderr |

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
