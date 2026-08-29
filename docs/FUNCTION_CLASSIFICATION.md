# Function classification

`default.xbe` has 17,308 functions. Most of them are not this game.

Splitting them tells you which code has to be understood and recompiled, and
which can be replaced with a real implementation. That distinction is worth more
here than names are.

## The split

Of 15,742 functions that could be hashed:

| bucket | count | | what it is |
|---|---|---|---|
| `XBOX_PLATFORM` | 7,815 | 49% | XDK, Xbox CRT, D3D8 — **replace with Win32/CRT, do not recompile** |
| `GAME_UNIQUE` | 5,557 | 35% | X-Men Legends' own code |
| `ENGINE` | 1,496 | 9% | Intrinsic Alchemy / Raven framework, present cross-game *and* cross-platform |
| `GAME_PORTABLE` | 730 | 4% | game code that also appears in a PC build |
| `XMEN_SERIES` | 144 | <1% | shared with Legends II only |

Per-function data: [`function_classification.tsv`](function_classification.tsv)
(`addr`, `bucket`, `code_units`, `name`).

**The real surface is ~6,400 functions, not 14,000.**

## How the buckets were derived

Comparison is by Ghidra FID hash, not raw bytes. The same library function links
at a different address in each image, so every absolute operand differs and a
byte compare finds almost nothing; the FID hash masks exactly those operands.

Four other builds were used, all x86, all XDK 5849, all Intrinsic Alchemy:

| build | role |
|---|---|
| X-Men Legends II (Xbox) | sequel — shares engine *and* game code |
| Marvel: Ultimate Alliance (Xbox) | different game, same engine |
| Marvel: Ultimate Alliance (PC, 2006) | **same game as above, other platform** |
| X-Men Legends II (PC, 2005) | second cross-platform reference |

`XBOX_PLATFORM` is the important one and comes from a single clean rule: code in
MUA's **Xbox** build that is absent from MUA's **PC** build is Xbox platform
code by construction — same game, same source, so the difference is the
platform. That gave 14,244 Xbox-only functions against 3,973 portable ones,
and 7,815 of those Xbox-only functions also appear here.

## Caveats that matter before trusting a number

- **`ENGINE` is a floor, not a measurement.** The PC builds are separate
  compilations, so only functions that compiled identically match at all — the
  PC build of Legends II yields 11,699 hashed functions against the Xbox
  build's 19,490. Engine code that simply failed to match sits in
  `XBOX_PLATFORM` without being Xbox-specific. Treat `XBOX_PLATFORM` as
  "safe to replace" only after checking the individual function.
- **Architecture must match.** FID hashes are instruction bytes. The 2016
  Marvel: Ultimate Alliance re-releases are x64 and cannot participate in any
  byte-level comparison; only the 2006 x86 build works.
- **Retail PC executables may be copy-protected.** A SafeDisc-wrapped binary is
  encrypted and matches nothing. Entropy ~6.4 with ~14k strings indicates a
  clean build; 7.5+ means it is still wrapped.

## Naming state

714 functions were named when this started; 4,560 are now.

Nearly all of the gain came from **walking MSVC RTTI to vtables** — 873 classes,
743 vtables, 3,748 functions named, plus 2,484 functions discovered that
analysis had never found because they were only reachable as vtable targets.
Ghidra's RTTI analyzer only runs on PE, so it never fired on an XBE.

3,866 functions on the game surface are still unnamed, and **more binaries will
not fix that**. Two other titles carrying 8,533 and 6,384 RTTI-derived names
contributed four names between them: 6,277 of the unnamed functions here have a
counterpart in those binaries that is *also* unnamed, because every binary was
named by the same technique and they are all blind in the same place —
non-virtual functions.

Routes that reach non-virtual code:

1. **Diagnostic string cross-references.** A function referencing
   `"FileMgr::Save open failure %s"` is `FileMgr::Save`.
2. **Call-graph propagation** from the 4,560 named functions.
3. **Alchemy's `ig*` type-registration table** — 693 class names sit in one
   contiguous blob, and it names constructors and factories rather than virtuals.

## Reproducing this

Tooling lives in the `re-lab-tools` repo under `analysis/`:

```bash
# per binary
analyzeHeadless <projects> <Project> -process <program> -noanalysis \
    -scriptPath ~/ghidra_scripts -postScript WalkMsvcRtti.java
analyzeHeadless <projects> <Project> -process <program> -noanalysis \
    -scriptPath ~/ghidra_scripts -postScript DumpFunctionHashes.java hashes_X.txt
# then
analysis/classify-shared-functions.py hashes_A.txt hashes_B.txt -o out/
```
