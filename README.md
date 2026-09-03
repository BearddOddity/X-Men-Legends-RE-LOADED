# X-Men Legends — PC Port ## NOW UNDER NEW NAME ## X-Men Legends: RE-LOADED

Static recompilation of **X-Men Legends (Original Xbox)** into a native Windows
executable. **No emulator at runtime.**

**Bring your own disc.** No game code or assets are included here. You supply an
image of a copy you own; everything derived is built on your machine. Same model
as [OpenGOAL](https://opengoal.dev) and
[UnleashedRecomp](https://github.com/hedge-dev/UnleashedRecomp).

---

## Status: not playable

Honest state — it boots partway and stops.

| signal | value |
|---|---|
| Kernel calls before halt | **200** |
| Total indirect calls executed | **149,902** |
| Definition of done | boots to the main menu |

It does not render. The current blocker is a subsystem pointer table at
`0x005BB700` that **nothing in the discovered code ever writes**, so the class
registrar reads NULL and faults.

Progress is tracked in the file, not from memory:

```bash
py -3 src/game/tools_data/progress.py
```

---

## Built on sp00nz/xboxrecomp

The recompiler, runtime libraries and platform layers come from
**[sp00nz/xboxrecomp](https://github.com/sp00nznet/xboxrecomp)** — MIT licensed,
Copyright (c) 2026 sp00nz. See [LICENSE](LICENSE) and the original
[README.upstream.md](README.upstream.md), preserved verbatim.

**Upstream is the canonical toolkit.** File toolkit issues there. This repo is
one game target plus the tooling written while getting it to boot.

Lifter fixes made here are not game-specific and belong upstream:

- **Fall-through edges.** A function split at an internal branch target never
  handed control to the next fragment, so the real epilogue never ran — leaking
  the prologue pushes and destroying callee-saved registers. 1,065 sites.
- **`cpuid`** was dropped as a comment, leaving the CPU feature word as garbage.
- **The safe ICALL stub** returned a truncated 64-bit *host* pointer into a
  32-bit guest register, so results shifted with code layout between builds.

---

## Quick start

```bash
py -3 -m tools.setup_game "X-Men Legends.iso"
```

Extracts, reads the XBE certificate, SHA-256s it against a manifest of verified
dumps, and stages locally. An unrecognised dump exits `2` rather than silently
recompiling into failures that look like lifter bugs.

Then follow [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) to lift and build.

---

## Tooling added here

Everything under `src/game/tools_data/` is diagnostic; run from `src/game/`.

| tool | what it answers |
|---|---|
| `progress.py` | what actually changed, recorded not recalled |
| `smoke_spread.py` | is this build deterministic? |
| `triage_crash.py` | which function faulted, and why |
| `find_dropped_fallthrough.py` | which functions fall off the end (1,005) |
| `trace_stubs.py` | which of 3,203 unresolved stubs actually run (10) |
| `restore_lost_guards.py` | re-place guards after a regeneration, safely |
| `find_missing_functions.py` | functions reachable only via data pointers |
| `seed_missing_functions.py` | lift those additively, no full regeneration |

Plus [`tools/setup_game`](tools/setup_game) (disc verification),
[`tools/ghidra_naming/xbsym_names.py`](tools/ghidra_naming/xbsym_names.py)
(XbSymbolDatabase → real XDK names), and
[`tools/hooks`](tools/hooks) (a pre-commit guard that blocks game data —
`.gitignore` only filters, `git add -f` walks past it).

Install the guard once after cloning:

```bash
tools/hooks/install.sh
```

---

## What is never committed

- The XBE, any ISO, and anything under `game/`
- `src/game/src/recomp/gen/` — 1M+ lines mechanically derived from the
  copyrighted executable, regenerated from your own disc

The pre-commit hook enforces this. `.gitignore` alone does not.

---

## Working notes

- [CLAUDE.md](CLAUDE.md) — the 15 rules and the tool inventory
- [src/game/DEBUGGING_NOTES.md](src/game/DEBUGGING_NOTES.md) — the full
  investigation log, wrong turns included
