# Prior art in static recompilation

What other projects doing this have solved, what transfers here, and — more
usefully — what does not and why.

## XenonRecomp

<https://github.com/hedge-dev/XenonRecomp> — Xbox 360 executables to C++, 6.5k
stars, active. Itself inspired by [N64: Recompiled](https://github.com/N64Recomp/N64Recomp).
The closest thing to this project that exists publicly: same technique, one
console generation later, PowerPC instead of x86.

Their README documents solutions to several problems this project has hit.

### What we already do the same way

Independently arrived at, which is some evidence the approach is sound:

| they do | we do |
|---|---|
| manual function boundaries in a TOML config | `seed_list.json` + `seed_missing_functions.py` |
| jump-table detection emitting real `switch` cases | `sweep_jumptables.py` |
| indirect calls through an address→function lookup | `recomp_lookup` / `recomp_lookup_manual` |
| CPU state passed around explicitly | guest registers as globals (`g_eax`, …) |

Their indirect-call design is worth noting as an alternative: a perfect hash
table where dereferencing a pointer derived from the original instruction
address yields the recompiled function. Ours is a lookup call. Theirs is faster;
ours is simpler to debug, and debuggability has mattered more here so far.

### What we have that they explicitly do not

> DISCLAIMER: This project does not provide a runtime implementation. It only
> converts the game code to C++, which is not going to function correctly
> without a runtime backing it. Making the game work is your responsibility.

The runtime is the half they leave to the user, and it is the half this project
has ~29,000 lines of: kernel, D3D, NV2A, APU, audio, input. Worth remembering on
a day when the boot dies in the first three initialisers — that part is built.

### What does not transfer, and why it explains our hardest problem

**Function boundary analysis.** XenonRecomp gets function boundaries from the
`.pdata` segment of the XEX:

> Functions with stack space have their boundaries defined in the `.pdata`
> segment of the XEX. For functions not found in this segment, the analyzer
> detects the start of functions by searching for branch link instructions.

**The original Xbox has no equivalent.** PowerPC requires unwind tables, so an
Xbox 360 binary carries an authoritative function table. x86 MSVC of this era
uses stack-based SEH — `push handler; mov fs:[0], esp`, which this binary is
full of — and emits no function table at all. Confirmed against our XBE: the
header carries sections, libraries and kernel imports, and nothing resembling an
exception directory.

So their single best discovery mechanism is structurally unavailable to us. That
is worth stating plainly, because function discovery has been the dominant
defect class on this project all along:

- `0x005BB700` — a subsystem table whose writer was never lifted, because its
  only reference is a pointer in a table
- 609 orphan instruction runs with prologues, recovered by heuristic
- 512 data-referenced function pointers, recovered by heuristic
- the CRT startup at `0x001A1C23`, which nothing in the binary calls — reachable
  only as an argument to `CreateThread`, and running today only because someone
  hand-wrote it

Every one of those would have been free with a `.pdata` table. They are not
failures of method; they are the platform's missing metadata, and the heuristics
we have had to invent are the substitute for it.

## Where to look next

- **N64: Recompiled** — the parent project, and the one with the most writing
  about the general technique.
- **XenonAnalyse** (inside XenonRecomp) — their function-boundary and jump-table
  analyser. The jump-table half may transfer; the `.pdata` half cannot.
- Their handling of **setjmp/longjmp** and the absence of **exception support**
  are both relevant: this binary uses SEH heavily, and `__SEH_epilog` has
  already caused one wall here (ledger, esp drift).

## Provenance note

The XBE's debug header names the original build:

```
c:\Projects\XMen\Code\Engine\Xbox_EXE\XMen_XBox_fnl.exe
```

`fnl` is the final build, and `Code\Engine` matches the engine/game split the
function classification work assumed.
