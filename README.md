# X-Men Legends: RE-LOADED

Static recompilation of **X-Men Legends (Original Xbox, 2004)** into a native
Windows executable. **No emulator at runtime** — the Xbox x86 code is translated
to C ahead of time and compiled, and the console's hardware is replaced with
modern equivalents rather than simulated.

**Bring your own disc.** No game code or assets are included here. You supply an
image of a copy you own; everything derived is built on your machine. Same model
as [OpenGOAL](https://opengoal.dev) and
[UnleashedRecomp](https://github.com/hedge-dev/UnleashedRecomp).

---

## Status: not playable

Honest state — it boots partway into the C runtime's static initialisers and
stops. It does not render. Every figure below is from a clean rebuild, run twice
with identical results.

| signal | value |
|---|---|
| Functions translated to C | 28,319 |
| Kernel calls before halt | **230** |
| Distinct functions reached | **196** |
| Direct call sites executed | **551** |
| Heap allocations | 136 |
| Indirect dispatches | 44,941 |
| Walls passed / runs recorded | 48 / 193 |
| Current halt | `sub_0020E547+0x1C3`, reading `0xFFFE00CC` |
| Definition of done | boots to the main menu |

**There is deliberately no completion percentage.** The goal is "boots to the
main menu", and the remaining work is however many defects stand between here
and there — a number nobody knows. Every figure above is something countable;
inventing one for overall progress would be the most quotable and least true
number on this page.

Progress is tracked in the file, not from memory:

```bash
py -3 src/game/tools_data/progress.py
```

Open defects are listed individually in
[docs/REGRESSIONS.md](docs/REGRESSIONS.md), each written so it can be picked up
on its own.

---

## How the work is done

Two rules shape everything here, both learned the expensive way.

**Probe, don't guess.** A cause is proved with a measurement before anything is
edited. The project keeps a ledger of every claim with a confirmed or refuted
verdict beside it — currently 167 entries, 45 of them refutations, several of
those refuting earlier entries in the same ledger. Recording what was
*disproved* has repeatedly stopped a plausible idea from being chased twice.

**A fix needs a second signal.** Kernel calls alone are a narrow proxy: when a
change alters which path runs, the counts before and after are measuring two
different programs. A rise in one number with no support from the others is not
evidence of a fix.

The full ruleset is in [CLAUDE.md](CLAUDE.md).

---

## Credits and sources

This project stands on other people's work. Where a name below carries a
licence, that licence governs the parts taken from it.

### The recompiler and runtime

**[sp00nz/xboxrecomp](https://github.com/sp00nznet/xboxrecomp)** — MIT,
Copyright (c) 2026 sp00nz. The lifter, runtime libraries and platform layers all
come from here; this repository is one game target plus the tooling written
while getting it to boot. See [LICENSE](LICENSE) and the original
[README.upstream.md](README.upstream.md), preserved verbatim. **Upstream is the
canonical toolkit — file toolkit issues there, not here.**

Lifter fixes made here are not game-specific and belong upstream: dropped
fall-through edges at internal branch targets, `cpuid` emitted as a comment, a
safe-ICALL stub truncating a 64-bit host pointer into a 32-bit guest register,
and `sbb reg, reg` emitted without the carry flag the preceding `neg` sets.

### Technique and prior art

- **[XenonRecomp](https://github.com/hedge-dev/XenonRecomp)** — the same
  technique one console generation later. Their jump-table handling and
  indirect-call design were both read closely; what does *not* transfer, and
  why, is written up in [docs/PRIOR_ART.md](docs/PRIOR_ART.md).
- **[N64: Recompiled](https://github.com/N64Recomp/N64Recomp)** — the project
  XenonRecomp itself credits, and the origin of this whole approach.
- **[OpenGOAL](https://opengoal.dev)** and
  **[UnleashedRecomp](https://github.com/hedge-dev/UnleashedRecomp)** — the
  bring-your-own-disc distribution model this project copies exactly.

### Analysis tools

- **[Ghidra](https://ghidra-sre.org/)** (NSA) — decompilation and headless
  analysis. Every wall here is read in Ghidra before it is fixed.
- **[XbSymbolDatabase](https://github.com/Cxbx-Reloaded/XbSymbolDatabase)**
  (Cxbx-Reloaded) — fingerprints the statically linked Xbox SDK and supplies
  real names for D3D8, DSOUND, XAPILIB and XGRAPHC, sections Ghidra's own
  signature databases do not cover at all. These are signature matches, not
  inferences.
- **[Capstone](https://www.capstone-engine.org/)** — disassembly inside this
  project's own analysis tools.
- **[extract-xiso](https://github.com/XboxDev/extract-xiso)** (XboxDev) and
  **[xdvdfs](https://github.com/antangelo/xdvdfs)** (antangelo) — disc image
  extraction.

### The game's own engine

The title is built on **Intrinsic Alchemy** (Intrinsic Graphics), and the engine
names itself: a contiguous type-registration table in the binary carries 693
`ig*` class names with their sizes, which turned out to be a better naming
source than any external symbol database — it reaches constructors and
factories, which RTTI cannot, because RTTI only walks virtual functions. Method
and evidence in [docs/GAME_FUNCTIONS.md](docs/GAME_FUNCTIONS.md).

Names are also recovered by matching code shape against retail Alchemy runtime
libraries. One negative result is worth recording alongside that: importing two
other Alchemy titles carrying 8,533 and 6,384 RTTI-derived names contributed
**four** usable names here, because every one of those binaries was named the
same way, so they are all blind in the same place.

### Assistance

Analysis, tooling and this documentation were written with
**[Claude Code](https://claude.com/claude-code)** (Anthropic), working against
the rules and the ledger described above.

### Not included, and not creditable

Nothing from the game, the Xbox SDK, or the Alchemy SDK ships in this
repository. Those were reference material for understanding a binary the user
already owns; none of them are redistributed here in any form.

---

## Quick start

```bash
py -3 -m tools.setup_game "X-Men Legends.iso"
```

Extracts, reads the XBE certificate, SHA-256s it against a manifest of verified
dumps, and stages locally. An unrecognised dump exits `2` rather than silently
recompiling into failures that look like lifter bugs.

Then follow [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) to lift and
build.

Install the pre-commit guard once after cloning — `.gitignore` only filters, and
`git add -f` walks straight past it:

```bash
tools/hooks/install.sh
```

---

## Tooling added here

Everything under `src/game/tools_data/` is diagnostic; run from `src/game/`.
Reach for these before hand-editing — hand-rolling what a tool already does is
how solved bugs come back.

| tool | what it answers |
|---|---|
| `progress.py` | what actually changed, recorded not recalled |
| `ledger.py` | has this claim been tried before, and what did it measure |
| `triage_crash.py` | which function faulted, at which source line, and why |
| `whatis.py` | what is at this Xbox address — section, owner, disassembly |
| `probe_struct.py` | many fields of one object in a single build |
| `walk_chain.py` | is a callee-saved register accounted for across a tail-call chain |
| `find_missing_functions.py` | functions reachable only through data pointers |
| `seed_missing_functions.py` | translate those additively, without a full regeneration |
| `bisect_seeds.py` | a batch of seeds regressed the boot — which one |
| `neuter_seed.py` | disable one seeded function in place, to bisect in one rebuild |
| `fix_sbb_carry.py` | restore the carry flag `neg` sets and `sbb` consumes |
| `dedupe_functions.py` | remove duplicated function bodies, keeping the correct copy |
| `manual_edits.py` | extract and re-apply hand edits across a regeneration |
| `snapshot.py` | archive and restore a known-good tree outside the repo |

Plus [`tools/setup_game`](tools/setup_game) (disc verification),
[`tools/ghidra_naming/`](tools/ghidra_naming) (Ghidra headless → real names) and
[`tools/hooks`](tools/hooks) (the pre-commit guard).

---

## What is never committed

- The XBE, any ISO, and anything under `game/`
- `src/game/src/recomp/gen/` — over a million lines mechanically derived from
  the copyrighted executable, regenerated from your own disc

The pre-commit hook enforces this. `.gitignore` alone does not.

---

## Working notes

- [CLAUDE.md](CLAUDE.md) — the rules and the full tool inventory
- [docs/REGRESSIONS.md](docs/REGRESSIONS.md) — open defects, individually
  actionable
- [docs/PRIOR_ART.md](docs/PRIOR_ART.md) — what other recompilation projects
  solved, and what does not transfer
- [docs/GAME_FUNCTIONS.md](docs/GAME_FUNCTIONS.md) — how 4,560 functions got
  real names, and why 4,354 still have none
- [src/game/DEBUGGING_NOTES.md](src/game/DEBUGGING_NOTES.md) — the full
  investigation log, wrong turns included

## Licence

The recompiler and runtime are MIT, Copyright (c) 2026 sp00nz — see
[LICENSE](LICENSE). The tooling and documentation added in this repository are
offered under the same terms. No game content is covered, because none is here.
