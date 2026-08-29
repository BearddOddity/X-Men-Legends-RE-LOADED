# Analysis exports

Ghidra analysis kept as text so it lives in git and can be reviewed.

| file | contents |
|---|---|
| `xmen_legends_xbox.names.txt` | 5,210 function names with prototypes and calling conventions, plus 6,856 labels |

734 KB, against 465 MB for the Ghidra project the names came from. A project is
an opaque binary — it cannot be diffed, reviewed in a pull request, or merged
when two people work at once — and it is regenerable from the XBE plus the
scripts. The names are the part worth versioning, and a diff here shows exactly
which functions gained one.

## Format

Tab separated, one record per line:

```
F <addr> <name> <callingConvention> <prototype>
L <addr> <name>
```

`F` is a function, `L` a label (vtables, RTTI structures, the `D3DRS_*` render
state globals). Header lines start with `#` and carry the program name, image
base and the SHA-256 of the executable.

## Applying it

Scripts are in the `re-lab-tools` repo under `analysis/`.

```bash
# import the XBE into a Ghidra project, then:
analyzeHeadless <projects> <Project> -process default.xbe -noanalysis \
    -scriptPath ~/ghidra_scripts \
    -postScript ApplyAnalysis.java xmen_legends_xbox.names.txt
```

`ApplyAnalysis` **refuses to run if the SHA-256 does not match**. Names are
applied by address, so pointing an export at a different build would not fail —
it would quietly produce a program full of confident, wrong labels with nothing
downstream able to tell. That check is the reason the hash is in the header.

## Where the 5,210 names came from

| source | names |
|---|---|
| RTTI vtable walk | 3,748 |
| Vtable-store constructors/destructors | 634 |
| Xbox SDK signature database | ~340 |
| Pre-existing analysis | 714 |
| Cross-binary transfer | 85 |

`ClassName::vfuncN` is a virtual function at slot N of that class's vtable.

`ClassName::ctor_or_dtor_<addr>` writes that class's vtable pointer into an
object. It is deliberately not called a constructor: MSVC frequently shares code
between constructor and destructor, and a function storing a vtable may also be
a factory. Where a function stores several vtables — a derived constructor with
its base constructors inlined — the class named is the one stored **last**,
which is the most-derived.

`tmpl__AV_...` is a C++ template kept in sanitised raw form. An earlier attempt
to demangle templates produced wrong-but-plausible names, which is worse than
ugly ones because everything downstream trusts them.
