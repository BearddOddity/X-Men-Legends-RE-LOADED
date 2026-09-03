# Open regressions and defects

Findings from the audit of 2026-09-03. R3, R4, R5 and R6 were all chased to conclusions the same day; all four turned out to share one cause. Each is stated so it can be picked up and
fixed on its own, without re-deriving how it was found. Nothing here is fixed —
this is the worklist.

Audit method: the tree was snapshotted first, both mirrored repos were confirmed
pushed, and the two repos with no remote were bundled to
`D:\My Games\recomp-snapshots\repo-bundles\`. Every check below is read-only
except the stub census, which instrumented a copy and was restored and
re-verified afterwards.

## What the audit confirmed is real

Stated first, because the rest of this file is problems and that would otherwise
read as the whole picture.

- **The current build reproduces its claimed numbers.** Rebuilt from source and
  run twice: 230 kernel calls, 136 heap allocations, 292 failed indirect calls,
  196 reached VAs, 551 call sites. Identical both runs.
- **196 reached and 551 call sites exceed every figure in the entire 193-entry
  history.** The highest previously recorded anywhere, including prose, are 170
  and 481.
- **All 1,093 recorded manual edits are physically present**, tested by exact
  block text rather than by the tool's own report. Nothing has been silently
  lost.
- **Seeds are intact**: 1,656 of 1,656 seeded addresses have bodies, and there
  are no duplicate function definitions anywhere in `gen/`.
- **The 2026-08-09 figures of 4,000 kernel calls and 826 heap allocations are
  not a lost high-water mark.** Entry #143 documents the change that dropped
  them as removing a page-zero junk dispatch — they were a runaway, not
  progress. Today's numbers are not a regression against them.

---

## R1 — the primary tracked signal is neither recorded nor checked

`signals.py` names `reached` as the first gated signal, "FIRST because it is the
only signal with any resolution". But `progress.json` has no `reached` or
`callsites` field, and `progress.py`'s `moved()` never tests either.

**Consequence.** Rule #14's stall detector is blind to the one signal that moves
when a fix works but the boot still stops in the same place. Only 39 of 193
entries mention `reached` at all, and only in prose. Today's records are
provable from the run log but not from the history.

**Fix.** Add `reached` and `callsites` to the recorded fields and to `moved()`.
Backfill what can be parsed out of existing messages; leave the rest null rather
than guessing.

## R2 — `progress.py` advertises a best that the project itself documented as junk

Every run prints `best: 4000 kernel calls`. That 4,000 is the watchdog clip on a
runaway, and entry #143 says so directly: the change that ended it removed "the
page-0 junk dispatch". So each session is told it stands 17x below a record that
was never real, which is exactly the misleading-number problem rule #8 exists to
prevent.

**Fix.** Exclude clipped runs from the best-ever calculation, or label them. The
clip value is a constant and easy to detect.

## R3 and R4 — both correct, both harmful, and both blocked on the same NULL

**Chased together 2026-09-03, as one experiment. Both reverted; the cause they
share is now named.**

### What they are

**R3** — `0x0020B8C6` is a two-instruction island (`mov ebx, edx`, `jmp
0x0020B88D`) read as a call target, so `sub_0020B850` returns without its
epilogue. The inline repair is in ledger #159.

**R4** — `0x001995AD` is a **109-byte gap**: `sub_00199519` ends exactly there
and the next known function starts at `0x0019961A`. Ledger #158 rejected seeding
it partly because "its enclosing function is only 41 bytes" — that was the
*recompiler's* extent, not the truth. MSVC pads with `int3`, and the real
function is `0x001994F0`–`0x00199862`, 882 bytes; the recompiler truncated it at
the first `ret`. The seeder's extent finder was fixed to stop at the next known
function start, and now reports the correct 109 bytes. Ledger #170.

### What measuring them proved

| configuration | kernel | heap | reached | call sites |
|---|---|---|---|---|
| baseline | **230** | **136** | **196** | **551** |
| R3 alone | 48 | 96 | 169 | 437 |
| R3 + R4 | 48 | 96 | 169 | 437 |
| R4 alone | 248 | 87 | 143 | 433 |

R3 accounts for the entire change — R3+R4 is byte-identical to R3 alone. R4 on
its own raises kernel calls but drops `reached` from 196 to 143, and `reached`
is the signal `signals.py` gates on first. So R4's rejection in #158 stands, on
new evidence and for a different reason: not a wrong extent, but the same
load-bearing behaviour as everything else in this class.

### The cause they share

With R3 applied the boot dies at `sub_001F7930+0x1E5` reading `0xFE000064`, a
**kernel thunk** address. Two probes on the field walk show why:

```
[FW]    outer=01099148 cont=010991B0 arr=01099238 idx=00000012
[ELEM2] elem=01097DA0 vt=003F7AB0          <- healthy
...
[FW]    outer=00000000 cont=00000000 arr=01098F88 idx=00000007
[ELEM2] elem=00000001 vt=FE000000          <- fabricated
```

**`sub_001F7930` is called with `this` = NULL.** `MEM32(0+0x28)` reads page zero
as 0, `MEM32(0+8)` yields the stale word `0x01098F88` which becomes the array
base, and the walk invents elements until one dispatches through `0xFE000000`.
Nothing is corrupted — page zero is simply being read as an object.

Ledger #115 already recorded where that NULL comes from: every call to
`sub_001F7930` arrives from `sub_002235D0` with `ecx` cached from
`sub_002226E0`'s return, and the **alt path** of `sub_002226E0` — taken once the
guard byte at `MEM32(0x5BC508)` flips 0 to 1 — returned `eax = 00000000` as its
first result.

**Next target, and it is a single one:** `sub_002226E0`'s alt path, through
`sub_00209650(0x2221E0)` and `sub_0020E520` on `MEM32(0x5BC274)`. Fixing that
unblocks R3, and R4 becomes testable again on a boot that gets further.
Ledger #169.

## R5 — `0x0006702C`: found, fixed in one line, and held

**Chased 2026-09-03. The cause is completely understood; the fix is written and
deliberately not applied.**

`0x0006702C` is **one byte** — opcode `0x48`, `dec eax` — falling straight into
`0x0006702D`. Its enclosing function is `0x00066FE0`–`0x0006703C`, an
initialiser that fills a 100-entry table at `[ecx+0x644]` with sequential
values.

The `jl` at `0x0006701D` is the **normal** path, taken on 99 of the 100
iterations; the fall-through is only the wrap case. So landing in a stub whose
whole body is `g_esp += 4` returned from the entire function on the first
iteration — leaving the table unfilled **and** the caller 12 bytes deep, because
the two prologue pushes and the return address were never popped.

`sub_0006702D` is already seeded and complete: the store, the loop back, the
epilogue. Only the decrement was missing, so no seeding is needed:

```c
/* in sub_00067000, replacing the generated jl */
if (CMP_L(eax, 0x64)) { eax--; g_seh_ebp = ebp; sub_0006702D(); return; }
```

**Why it is held.** Measured deterministically both ways: kernel calls 230 to
**60**, heap 136 to 91, reached 196 to **175**, call sites 551 to **489**,
indirect dispatches 44,941 to **128**. Every tracked signal moved the wrong way,
so rule #1 applies. The boot then dies somewhere entirely different —
`sub_0020F860+0x274F` reading `0xFC015AD8`, under `sub_00211530` and
`sub_001EC600`.

**This is now a pattern, not a coincidence** — the third faithful fix a broken
stub turned out to be load-bearing for, after R3 and the field-container fix. An
empty table read as empty is survivable; a table filled with real indices sends
execution down paths that have their own defects. Expect the next one to behave
the same way.

Ledger #168.

## R6 — not a missing fragment: the stack-cookie check is failing

**Reclassified 2026-09-03. Do not seed this.**

`0x00340F24` is `__report_gsfailure`:

```
push 8 / push 0x430848 / call 0x3432A8   ; handler setup
call 0x345B7F / or [ebp-4], -1
push 3 / call 0x3428C6 / int3            ; abort
```

It never returns, which is why the seeder reports "no terminating ret found" —
the tool is right to refuse. It is reached from `0x00340F5D`, the failure arm of
`__security_check_cookie` at `0x00340F54`:

```
cmp ecx, MEM32(0x47A050) / jne 0x340F5D / ret
```

So **the stack cookie check is failing on every run**, and the stub is
swallowing the abort. Seeding it would make the process abort by design — the
opposite of a repair.

The cookie global *is* written, by two stores in `recomp_0025.c`. The open
question is whether that initialiser runs before the checks do, or whether a
real overrun is being detected. Either way this is a symptom of something else,
and it is the only item on this list that is a *detector firing* rather than a
missing piece of code. Ledger #169.

## R10 — a manual guard that has never executed

In `sub_0020E547` the generated line runs **before** the guard meant to
constrain it:

```c
eax = MEM32(esp + 8);
if (TEST_NZ(eax, eax)) goto loc_0020E56D;                     /* generated */
if (TEST_NZ(eax, eax) && eax >= 0x00880000u && ...) goto ...  /* guard - dead */
```

For any non-zero `eax` the generated line has already jumped, so the
plausible-pointer guard never runs. Its own comment says to keep it precisely
for the `eax = -1` case it was written for. It has never been in force.

`manual_edits.py` is insert-only, and this guard needed to **precede** the line
it constrains rather than follow it. Worth a sweep: any guard whose condition
duplicates the generated line immediately above it is dead the same way.

Not switched on — enabling a diagnostic bypass does not address the cause, and
the cause is now known (below). Ledger #172.

## The current wall, and it is one function away

`sub_0020E547` faults reading `0xFFFE00CC`. Eighty-three calls carry a healthy
allocator; the eighty-fourth carries **`alloc = 00000002`**. `MEM32(2)` reads
mapped page zero as `0xFFFE0000`, and `MEM32(0xFFFE0000 + 0xCC)` is the fault.

Probes on both candidate producers fired zero times on the fatal call, so the 2
arrives as the **argument**, and `recomp_where` names the caller:
**`sub_002219A0+0x839`**. A small integer where a pointer belongs is ledger
#149's signature — callee-saved corruption. That is the next target.


## W — the wall audit: what is actually broken

All 49 distinct crash sites across 198 recorded runs, scanned 2026-09-03.

### The misleading number

**Kernel calls stopped measuring depth somewhere around 29 August.**

| date | kernel | reached | call sites | dispatches |
|---|---|---|---|---|
| 9 Aug | 226 | 154 | 434 | 22,313 |
| 29 Aug | 434 | 159 | 437 | **22,313** |
| 2 Sep | 578 | 165 | 481 | 24,681 |
| **3 Sep** | **230** | **196** | **551** | **44,941** |

On 29 August the kernel count nearly doubled while the dispatch count did not
move **by one** and `reached` rose by five. A count that rises with no more work
happening is measuring something else.

Today the kernel count is the lowest since August and the work is **double
anything ever recorded**.

The mechanism is proven separately. Entry #148's own summary says "the wall
moves off `sub_001FBA90`" — which is exactly where ledger #136's carry-flag fix
lives, and a seeding run silently discards the manual edits in that file. With
the fix absent the predicate answers yes to everything and the boot *skips* work
while still counting kernel calls; with it present the boot sits at 226 on
9 August and 230 today.

**`reached`, call sites and dispatches have risen monotonically throughout and
are trustworthy. `kernel_calls` between 29 August and 2 September should not be
read as depth.**

### Eleven walls are not proven broken

They sat deeper than the 230 kernel calls the current build reaches, so their
repairs are untested by any run made today:

| wall | depth | last seen |
|---|---|---|
| `sub_00202B87+0x35E` | 1426 | #90 |
| `sub_001EBA9E+0x1A4` | 582 | #191 |
| `sub_00209414+0x3FA` | 530 | #183 |
| `sub_00221070+0x647` | 530 | #184 |
| `sub_001E8800+0x182` | 530 | #185 |
| `sub_001EA600+0x2C3` | 514 | #162 |
| `sub_001EB890+0x1D5` | 514 | #181 |
| `sub_001EA6B0+0x2FB` | 514 | #182 |
| `sub_002096B0+0xD5` | 434 | #161 |
| `sub_001EA5A0+0x341` | 338 | #138 |
| `sub_00205170+0x7D1` | 274 | #142 |

Thirty-five other walls sit shallower than 230 and **are** passed on every run.

### Seven walls were revisited

Left and later returned to. Three are substantive; four oscillated within two
entries during a bisect and are noise.

- `VCRUNTIME140.dll` — 3 visits across 106 entries
- `sub_002235D0+0xC0` — 3 visits across 54 entries
- `sub_00205170+0x7D1` — 2 visits across 15 entries

### Fifteen walls were left by a bypass, not a repair

Classified from the entry that moved off each one. These are passed in the
counter and not in the code: `sub_001A0B0C`, `sub_001F7930+0x1FC`,
`sub_00221F50+0x2E2`, `sub_001F7560+0x2F8`, `sub_0013AE50+0xB10`,
`sub_0013B0E0+0x2CE`, `sub_001186A0+0xC7`, `sub_0013AE50+0x3C1`,
`sub_0034139A+0x25A`, `sub_00204800+0x24D`, plus five whose exit entry is
unclear.

Ledger #174.

## R7 — one undocumented drop in the recorded history

Entry #26 (2026-08-02), kernel calls 92 to 72, with no note. Every other large
drop in 193 entries carries an explanation. Low priority, but it is the only
gap in an otherwise complete record.

## R8 — `manual_edits.py apply` duplicates three functions on every run

Deterministic and reproduced three times: each `apply` writes a second copy of
`sub_001EA600`, `sub_001EA640` and `sub_001EA6B0` into `recomp_0014.c`, one of
them a hybrid carrying `loc_001EA5EF`, a label belonging to `sub_001EA5E0`.

`tools_data/dedupe_functions.py` now repairs it, so this no longer costs a build
— but the duplication itself is unfixed, and the repair must be run after every
`apply`. Ledger #163 and #166.

**Fix.** Find why `apply` re-emits the region. Likely a `wrap` record whose
`wrapped` content spans more than it should.

## R9 — `manual_edits.py verify` reports placeable as though it were present

`verify` prints `would place 1093/1093` whether or not the edits are actually in
the tree. That is how four edits, including ledger #136's carry-flag fix, were
silently lost earlier today and a whole baseline was measured wrong.

**Fix.** Add a presence check: for each record, count exact occurrences of its
block in the target file and report any group with fewer copies than records.
That test is what this audit used and it found the tree clean; it belongs in the
tool, not in an audit script.

---

## Preservation risk, outside the code

`oddity-skills` — the local marketplace holding the `oddity-re` plugin and all
twelve of its skills — **has no git remote**. It is committed, but on one disk.
A bundle now exists at
`D:\My Games\recomp-snapshots\repo-bundles\oddity-skills-*.bundle`, which is a
backup and not a mirror.

**Fix.** Add a remote. That is an outward-facing action and needs an explicit
decision, so it is recorded here rather than done.
