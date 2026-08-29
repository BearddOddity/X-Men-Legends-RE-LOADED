# Diff the Alchemy engine between XML1 Xbox and XML2 Xbox, and decide go/no-go

> **Retargeted 2026-08-08: use the XML2 _Xbox_ build, not the PC one.**
> An Xbox copy was obtained, so the diff is now same-platform — same CPU,
> same XDK compiler, same libraries, same CRT. Correlation should be far
> higher than against a PC build fighting a different compiler *and*
> different libraries. The PC binary stays as a secondary reference only.
>
> **Go/no-go: QUALIFIED GO — narrower than first claimed.** Corrected
> 2026-08-08 after actually running the RTTI parser over both binaries.
>
> The earlier "88.6%" was raw type-descriptor *string* overlap. Restricting
> to classes that actually carry a vtable, and comparing vtable shape:
>
> ```
> classes with vtables    XML1 726    XML2 881    shared 549 (75.6% of XML1)
> shared classes whose vtables have IDENTICAL method counts:  173 / 549 (31.5%)
> ```
>
> **The allocator classes are among those that DIFFER**, which is precisely
> the opposite of what the optimistic reading assumed:
>
> | class | XML1 methods | XML2 methods |
> |---|---|---|
> | `CMemory` | 12 | 13 |
> | `CMemory::IMemoryPoolInfo` | 44 | 47 |
> | `CMemory::SXMenMemoryPoolInfo` | 34 | 36 |
> | `IAlchemyObjectPool` | 91 | 110 |
> | `CEntityAllocator` | 5 | 6 |
>
> XML2's engine gained virtual methods, so **slot-by-slot transfer is not
> safe** for these classes: XML1 slot N and XML2 slot N are not the same
> method once a virtual is inserted anywhere above N.
>
> **THE ABOVE NUMBERS WERE WRONG — corrected below.** The `CBlock` 41-vs-1
> reading was not a real class change, it was the detector failing, exactly
> as suspected. Root cause: the XBE marks *every* section executable
> including `.rdata` and `.data`, so the vtable walk ran off the end of each
> table into read-only data. Fixed in `rtti_names.py` (`CODE_SECTIONS`).
>
> **Corrected measurement:**
>
> ```
> shared classes with vtables                     549
> identical vtable shape                          329  (59.9%)   [was 31.5%]
> ```
>
> | class | XML1 | XML2 | |
> |---|---|---|---|
> | `CBlock` | 1 | 1 | SAME (was a bogus 41 vs 1) |
> | `CMemory` | 12 | 13 | DIFF |
> | `CMemory::IMemoryPoolInfo` | 9 | 10 | DIFF |
> | `IAlchemyObjectPool` | 2 | 3 | DIFF |
> | `CEntityAllocator` | 5 | 6 | DIFF |
>
> **The divergence finding survives the fix, and now has a coherent story.**
> Every memory class gained *exactly one* virtual method in XML2 — a uniform
> +1 is what a shared base class acquiring one virtual looks like, propagated
> to derived classes. That is credible engine evolution rather than detector
> noise, so the conclusion stands: slot-by-slot transfer is unsafe for these
> classes, and `slot N` in XML1 is not `slot N` in XML2 below the insertion
> point.
>
> What survives, and what this ticket should therefore do:
> - **Class identity transfers** (549 shared names) — safe and useful.
> - **Slot-by-slot method transfer does NOT transfer wholesale** — it needs
>   per-class verification, starting by tightening the vtable-end detection
>   so the counts can be trusted at all.
> - Do not plan on this cracking the allocator wall. Ticket 02 remains the
>   route there.

Status: open
Type: task
Blocked by: 11

## Question

With the XML2 PC binary imported (ticket 11), establish whether the two
binaries actually correlate well enough to be worth mining — and if they do,
transfer what we can onto the functions we are stuck in.

**This ticket owns the go/no-go.** State the verdict explicitly rather than
drifting into open-ended analysis; the premise is plausible but unproven,
and an unproven premise plus an interesting tool is how a week disappears.

Approach:

1. Create a ReVa diff session between the XML1 Xbox program and XML2 PC
   (`diff-create-session`, then `diff-status` / `diff-summary`).
2. Read the correlation quality (`diff-list-functions`, `diff-summary`).
   This is the go/no-go evidence.
3. **If correlation is good**, target the allocator chain first — it is
   where every wall has been. Known XML1 addresses to look up:
   `sub_00204800` (live crash site), `sub_00204020` (the esp escape),
   `sub_0020F209` / `sub_0020EFD0` / `sub_00211530` / `sub_0020F860` (the
   allocator chain mapped across 2026-08-05). Use `diff-transfer-markup` to
   pull names and types across, and `diff-function` to read how the PC build
   of the same routine behaves where ours fails.
4. **If correlation is poor**, say so, record why, and close. Do not keep
   digging.

### The premise got stronger on 2026-08-08

Two confirmations landed after this ticket was written:

1. **The engine is confirmed from the game data, not inferred.**
   `src/game/game/alchemy.ini` is headed "Alchemy.ini for X-Box", and the
   XBE references `alchemy.ini`, `showLeaksOnExit` and `defaultReportLevel`.
2. **The allocator we are stuck in is Alchemy's own memory manager** —
   middleware, not game code, not the CRT. So the exact routines failing
   here shipped *working* in XML2's PC build. That is the strongest form
   this ticket's premise can take: not "similar code somewhere" but "the
   same middleware, known-good, on the same instruction set."

### Set expectations honestly before starting

- XML2 PC retail is almost certainly **symbol-stripped**, so this most
  likely yields structural correlation, not free function names.
- The compilers differ (Xbox XDK vs PC MSVC), so codegen will not match
  byte-for-byte even where the source was identical. Expect
  similarity-based matching to do the work, not exact hashes.
- Alchemy is **middleware**, so engine-layer correlation should be much
  stronger than game-layer. Judge the premise on engine functions
  (allocator, file I/O, math), not on X-Men-specific code.

### This is a parallel track, not the critical path

Ticket 02 (the live `sub_00204800` NULL-derived write) remains the direct
route to the current wall, and it does not depend on this. If this ticket
pays off it accelerates 02 and every wall after it; if it does not, nothing
is lost but the time spent here. Do not let it displace 02.

## Answer

<!-- filled on resolution -->
