# Re-baseline current live signals

Status: resolved
Type: task

## Question

`walls_report.md` (latest walls.py run) and `progress.md` (latest recorded
entry) disagree: the report shows 2144 steps with a crash at `sub_0020E547`;
the progress log shows 44 kernel calls with a crash at
`sub_00204800+0x24D`. Before any other ticket on this map trusts a number,
establish which is current.

Do a clean build + run cycle (`mcp__OddityRecomp__build`, then
`mcp__OddityRecomp__run`), confirm the build lock is free first
(`mcp__OddityRecomp__afk_status`), and record the result
(`mcp__OddityRecomp__progress` with `record=`). Note in the answer whether
the discrepancy was just staleness (walls_report predates the last landed
fix) or something that needs its own investigation.

## Answer

**The discrepancy was pure staleness. No second investigation needed.**

Established by timestamp rather than by argument:

| file | mtime | meaning |
|---|---|---|
| `walls_report.md` | Aug 7 11:11 | predates the fixes below |
| `kernel_bridge.c`, `xbox_memory_layout.c` | Aug 7 22:59 | the base_hint/`xbox_HeapAllocAt` fix, ~12h later |
| `build/xmen_legends_recomp.exe` | Aug 8 02:12 | current build |
| `tools_data/progress.json` | Aug 8 02:21 | current record |

The walls report describes a build that no longer exists — it predates the
entire 2026-08-08 session (pool-refusal fix, the two seeded `g_esp+=4`
stubs, `sub_00221E50`, `sub_00216F70`). `progress.md` is the current source
of truth. The walls report needs re-running, not investigating.

**Live signals, deterministic 2/2 with zero spread on every tracked signal:**

- kernel_calls **44**
- failed_icalls **5**
- heap_allocs **23**
- safe_stub **8**
- reached **101** (`[COVERAGE-VA]` line count)
- callsites **357**

Reproduces the 2026-08-08 entry exactly, which also confirms the uncommitted
working tree still holds that session's work intact after the session gap.

**Live wall** (now confirmed as the real one): access violation writing Xbox
VA `0xFFFFFF03` in `sub_00204800+0x24D` (`recomp_0015.c` ~17974, ~74%
through). `eax=0xFFFFFF00` and `esi=0xFFFFFFFF` are both NULL-derived
negatives — a null pointer with a small offset added, not a wild pointer.
Caller chain: `sub_00204800` → `sub_0020A360` → `sub_0020DA95` →
`sub_002155B0` → `sub_002392E0` → `sub_00239E50` → `sub_00011E40`.

The 5 failed indirect calls are `0x00000001`, `0x00000000`, `0x00000010`,
`0xFFFFFFFF`, `0x0000000B` — small integers and -1, not plausible code
addresses. These are bad pointers, **not** missing functions; do not seed
them.

### Two incidental findings worth carrying forward

1. **`build` returning `linked: false` with zero errors is not a failure.**
   `linked` is a string match for `"Linking C executable"` in the build
   output, so an already-up-to-date tree reports `false`. Verified
   independently: the exe (Aug 8 02:12) is newer than every source under
   `src/`, `../kernel`, `../d3d` (newest Aug 7 23:17). Don't read this as a
   broken build and start clean-rebuilding.

2. **`src/game/tools_data/sweep_jumptables.py` already exists** (untracked).
   That is the detector ticket 05 was about to specify from scratch — that
   ticket should start by reading and running it, not writing one.

Recorded to the progress log (2026-08-08, +0 delta — a verification, not a
fix).
