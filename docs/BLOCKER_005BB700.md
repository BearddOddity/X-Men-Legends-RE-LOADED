# The `0x005BB700` blocker

The README says the subsystem pointer table at `0x005BB700` is one that **nothing
in the discovered code ever writes**. That is accurate, and the operative word is
*discovered*. The writer exists. It was never discovered, so it was never lifted,
so the table stays NULL and the class registrar faults reading it.

## The writer

`0x00216210`, now named `SubsystemRegistry_Register`:

```
00216210  SUB ESP,0x20                  ; prologue
00216213  MOV EAX,[0x005bc544]          ; registered count
0021621c  CMP ECX,EBX / JZ              ; null check on the incoming object
0021622a  CMP EAX,0x4  / JGE            ; the table holds at most 4
0021622f  MOV [EAX*4 + 0x5bb704],ECX    ; table[count] = object
00216237  MOV [0x005bc544],EAX          ; count++
0021623c  INC dword ptr [ECX + 0x4]     ; refcount++ on the object
```

So `0x005bc544` is the count, the table caps at four entries, and `[ECX+4]` is an
`igObject`-style refcount — Alchemy. Subsystems register themselves here at
startup, and in the recompiled build none of them do.

## Why it was never lifted

The chain matters more than the function:

1. `0x005BB700` is NULL because `SubsystemRegistry_Register` never runs.
2. It never runs because **nothing calls it**. Its only reference is
   `0x003f4478`, and that reference is `DATA`, not a call — the address sits in
   a table of function pointers.
3. A recompiler that follows direct calls from an entry point never reaches it.
4. Never reached means never promoted to a function, and the lifter works from
   functions.

**This is not a missing write. It is an indirect-dispatch gap.** The same root
cause the README already records from the other side: a crash in `ICALL dispatch`
on an unknown function pointer, and 149,902 indirect calls executed.

## It is systemic

Measured across the whole binary:

| | |
|---|---|
| Instructions outside any function | **143,501** |
| Contiguous runs of such code | 6,973 |
| Runs both referenced *and* opening with a prologue | **609** |

Those 609 were created as functions. Afterwards:

| | before | after |
|---|---|---|
| functions | 17,308 | **17,917** |
| orphan instructions | 143,501 | **98,575** |
| referenced runs with a prologue | 609 | 11 |

**44,926 instructions moved from invisible to liftable.** `0x00216210` is one of
them, so re-lifting should now include the table writer.

## What to do next

1. **Re-lift.** The writer is a function now; the registrations may simply work.
2. **Follow `0x003f4478`.** Identify what that table is and who walks it. If it
   is an array of init or factory pointers, the durable fix is to treat
   data-referenced function pointers as lift roots — which would also reach a
   share of the remaining 98,575 orphan instructions.
3. **Compare against the PC build.** Marvel: Ultimate Alliance's 2006 PC build
   is the same engine at the same architecture, with this path working. It shows
   what belongs in those four slots and in what order.

## Reproducing

Scripts are in the `re-lab-tools` repo under `analysis/`:

```bash
# measure, then apply
analyzeHeadless <projects> <Project> -process <program> -noanalysis \
    -scriptPath ~/ghidra_scripts -postScript OrphanCode.java
analyzeHeadless ... -postScript OrphanCode.java apply

# investigate any address
analyzeHeadless ... -postScript ProbeAddress.java 0x5BB700 0x400
analyzeHeadless ... -postScript ProbeUndefined.java 0x0021622f 22
analyzeHeadless ... -postScript WhoRefs.java 0x00216210
```
