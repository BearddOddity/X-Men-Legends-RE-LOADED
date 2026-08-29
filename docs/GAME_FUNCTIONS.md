# Game functions

The subset of `default.xbe` that is actually X-Men Legends — platform and engine
code excluded. This is the work list.

| | |
|---|---|
| Game functions | **6,431** |
| Named | 2,077 |
| **Unnamed** | **4,354** |
| Game code units | 472,274 |
| Unnamed share of game code | **72%** (340,549 units) |

Per-function data: [`game_functions.tsv`](game_functions.tsv) —
`addr`, `bucket`, `code_units`, `status`, `name`. **Sorted largest first**, because
a 2,000-unit unknown is worth more attention than a stub.

Buckets:

| bucket | meaning |
|---|---|
| `GAME_UNIQUE` | only in this title |
| `GAME_PORTABLE` | also in the PC build of Legends II |
| `XMEN_SERIES` | shared with Legends II on Xbox only |

Excluded: 7,815 Xbox platform functions (XDK/CRT/D3D8), 1,496 cross-platform
engine, and everything else shared with Marvel: Ultimate Alliance. See
[FUNCTION_CLASSIFICATION.md](FUNCTION_CLASSIFICATION.md) for how the split was
derived and its caveats.

## Highest-value unnamed targets

The largest unidentified game functions:

| address | code units |
|---|---|
| `00040e60` | 2,072 |
| `0001a5f0` | 1,524 |
| `000c1f40` | 1,516 |
| `00115830` | 1,409 |
| `00016b00` | 1,342 |
| `00061040` | 1,096 |
| `00303210` | 986 |
| `000955c0` | 981 |

## What is already named, by class

RTTI gave class-qualified names for the virtual functions. The busiest game
classes:

| class | functions |
|---|---|
| `CCombatNodeHandler` | 101 |
| `CCEAtkTeleport` | 87 |
| `CGame` | 58 |
| `CModelIGB` | 50 |
| `CCamera` | 41 |
| `CInventorySystem` | 40 |
| `CActor` | 34 |
| `CHud` | 32 |
| `CStaticQuery` | 27 |

Names of the form `tmpl__AV_...` are C++ templates kept in sanitised raw form
rather than demangled — an earlier attempt to demangle them produced
wrong-but-plausible names, which is worse than ugly ones because everything
downstream trusts them.

## Why 4,354 are still unnamed

They are **not virtual**. Every name here came from walking RTTI to vtables, and
RTTI only reaches virtual functions. Non-virtual methods, free functions and
statics are invisible to it.

Importing more binaries will not help — that was measured, not assumed. Two
other Alchemy titles carrying 8,533 and 6,384 RTTI-derived names contributed
**four** names, because 6,277 of the unnamed functions here have a counterpart in
those binaries that is *also* unnamed. Every binary was named the same way, so
they are all blind in the same place.

Routes that reach non-virtual code:

1. **Diagnostic string cross-references** — a function referencing
   `"FileMgr::Save open failure %s"` is `FileMgr::Save`.
2. **Call-graph propagation** from the 2,077 named game functions and the
   4,560 named overall.
3. **Alchemy's `ig*` type-registration table** — 693 class names in one
   contiguous blob; it names constructors and factories, which are not virtual.
