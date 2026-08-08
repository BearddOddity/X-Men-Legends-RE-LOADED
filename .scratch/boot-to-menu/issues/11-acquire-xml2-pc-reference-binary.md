# Acquire X-Men Legends II (PC) and import it as a reference binary

Status: open
Type: task

## Question

Verified 2026-08-08 (Wikipedia, both titles): X-Men Legends (2004, Xbox —
our target) and X-Men Legends II (2005, **Windows**) are both Raven Software
titles on the **same engine, Vicarious Visions Alchemy**, one year apart,
sharing at least one credited programmer (Daniel Edwards). XML1 never
shipped on PC; XML2 did.

Both are **x86**. The Xbox CPU is a Pentium III derivative and XML2 PC is a
32-bit x86 PE, so the two binaries are directly comparable at the
instruction level — not merely conceptually similar.

That makes XML2 PC a potential Rosetta stone for this port, and it aims
squarely at where we are stuck: every wall so far has been in memory
management (allocator, pool growth, coalescer, registry), which is engine
middleware — the layer most likely to be near-identical across the two
titles.

**This ticket is acquisition and import only.** Do not start the diff here;
that is ticket 12, which owns the go/no-go.

Steps:

1. Obtain a legitimately-owned copy of X-Men Legends II for PC (user-driven;
   this is the HITL part).
2. Extract the game executable.
3. Import into Ghidra and run auto-analysis (`mcp__ReVa__import-file`, then
   `analyze-program`).
4. Cheap sanity checks before anyone invests further: confirm it is 32-bit
   x86 PE, get the function count (`get-function-count`), and check whether
   strings/symbols show any Alchemy or Raven markers (`get-strings`).

**Where it must live:** put the binary in the toolbox directory
`D:\My Games\Xbox recomp tools`, alongside the XML1 xiso — **not** in this
repo. It is copyrighted game data under exactly the same "ship no game data"
rule as the XBE. The toolbox directory is not a git repo, so nothing can
commit it by accident; putting it under `Xbox Recomp/` would put it one
`git add -f` away from a licence violation.

Resolve by recording: the Ghidra program name for XML2 PC, its architecture
and function count, and whether the sanity checks support or undercut the
same-engine premise.

## Progress 2026-08-08 — PC copy obtained, but redirect to the Xbox build

**A PC copy is on disk** at
`D:\My Games\Xbox recomp tools\x-men-legends-ii-rise-of-apocalypse_202310\`
(correct location — toolbox, not the repo). Surveyed:

- The ISO holds an **Inno Setup installer**, not loose game files:
  `setup.exe` (2.58 MB) + `setup-1.bin` (1187 MB payload). The game
  executable is inside the payload.
- Extracting it needs **`innoextract`**, which is not installed. Do not run
  `setup.exe` to get at the files — installing software is a system change
  and unnecessary when the archive can be extracted directly.
- Also present in the toolbox and worth knowing about:
  `X-Men Legends II - Rise of Apocalypse (USA).7z` is the **PlayStation 2**
  release (contains `SLUS-21138 (1.03).iso`; SLUS is a Sony serial). PS2 is
  MIPS, so it is useless for instruction-level diffing against an x86 Xbox
  binary. A `..._NoCD_Win_EN.zip` is also present — a NoCD patch is a
  *modified* binary and is the wrong reference for RE; prefer the clean one.

### Redirect: prefer the Xbox build of XML2

The user can also obtain an **Xbox** copy, and that is the better target.
This ticket should acquire that instead of (or before) extracting the PC
installer:

1. **Correlation quality.** XML1 Xbox ↔ XML2 Xbox shares CPU, XDK compiler,
   platform libraries, CRT and calling conventions. XML1 Xbox ↔ XML2 PC
   shares only the instruction set, leaving a different compiler *and*
   different libraries for similarity matching to absorb.
2. **Zero new tooling.** An Xbox XML2 is an XBE, and this repo already has
   the full XBE path — `tools/setup_game` (dump verification), the
   disassembly pipeline, `tools/ghidra_naming/xbsym_names.py`. The PC build
   is a PE needing a separate import route.

Keep the PC copy regardless — it costs nothing to retain, and it remains the
only place to see how the *same middleware* behaves when targeting a PC,
which is what this port is ultimately recreating.

## Progress 2026-08-08 (later) — Xbox build acquired and extracted

**The Xbox build is on disk**, which was the recommended target. Both
reference binaries now exist:

| binary | path | size | notes |
|---|---|---|---|
| **XML2 Xbox** (primary) | `Xbox recomp tools\XML2-Xbox\extracted\X-Men Legends II - Rise of Apocalypse (USA, Europe)\default.xbe` | 5.46 MB | **use this** |
| XML2 PC (secondary) | `Xbox recomp tools\X-Men-Legends-II-Rise-of-Apocalypse_NoCD_Win_EN\XMen2.exe` | 2.98 MB | 32-bit x86 PE, NoCD-patched |
| XML2 PS2 | `Xbox recomp tools\X-Men Legends II - Rise of Apocalypse (USA).7z` | — | **useless**, MIPS not x86 |

Extraction route, recorded because neither step is obvious:

1. **`.7z` needs no install** — Windows ships `bsdtar` (libarchive 3.8.4) as
   `tar.exe`, which reads 7-Zip archives: `tar -xf <archive> -C <dest>`.
   7-Zip itself is not installed and does not need to be.
2. **Xbox ISO → files** via the toolbox's
   `extract-xiso\extract-xiso.exe -x <iso>` (XDVDFS, so ordinary ISO tools
   will not work). 380 files, 2.35 GB.

`alchemy.ini` sits beside XML2's `default.xbe` exactly as it does in XML1 —
same engine, third independent confirmation.

Note the PC ISO route was abandoned as unnecessary: it holds an Inno Setup
installer needing `innoextract`, while the NoCD zip already contained a
usable `XMen2.exe`.

### Sanity checks — premise strongly confirmed

RTTI class-name sets compared directly between the two **Xbox** binaries:

```
XML1 Xbox classes : 563
XML2 Xbox classes : 668
shared (in BOTH)  : 499     <-- 88.6% of XML1's classes
XML1-only         :  64
XML2-only         : 169     (added in the sequel)
```

**Every** memory/allocator class is shared: `IAlchemyObjectPool`, `CMemory`,
`IMemory`, `IMemoryPoolInfo@CMemory`, `CEntityAllocator`,
`IEntityAllocator`, `CBlock`, `CLineBlock`, `CResponseBlock`.

Also checked and negative, so nobody re-checks: **neither binary contains
embedded source paths** (`.cpp`/`.h`). Both are release builds with paths
stripped. RTTI is the naming source; there is no richer one hiding.

### Remaining before this closes

Import `XML2-Xbox\...\default.xbe` into Ghidra (`mcp__ReVa__import-file`,
then `analyze-program`) and record its program path and function count.
Acquisition and extraction are done; only the import remains.

## Answer

<!-- filled on resolution -->
