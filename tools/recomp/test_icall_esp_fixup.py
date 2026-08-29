#!/usr/bin/env python3
"""Check where _fixup_icall_esp_save puts the _icall_esp capture.

RECOMP_ICALL_SAFE restores g_esp = _icall_esp when a target cannot be resolved.
The capture therefore has to sit BELOW the prologue's callee-saved register
pushes (or the rollback throws the saves away and the epilogue's POP32s read
the wrong slots - ledger #145) and ABOVE the argument pushes (or a failed call
leaks its arguments and the following POP32 takes the register from the wrong
slot - docs/PAGE_ZERO_CENSUS.md).

Both mistakes have been shipped in this tree, in opposite directions, so the
boundary is worth a test.

    py -3 tools/recomp/test_icall_esp_fixup.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_fixup():
    """Pull the function out of translator.py without importing the package.

    translator.py does relative imports at module level, so importing it
    standalone fails; the function itself has no dependencies beyond re.
    """
    src = open(os.path.join(HERE, "translator.py"), encoding="utf-8").read()
    start = src.index("def _fixup_icall_esp_save")
    m = re.search(r"^(def |class |@)", src[start + 10:], re.M)
    end = start + 10 + m.start() if m else len(src)
    ns = {}
    exec(src[start:end], ns)
    return ns["_fixup_icall_esp_save"]


def _capture_index(out):
    for i, line in enumerate(out):
        if "_icall_esp = g_esp" in line:
            return i
    raise AssertionError("no _icall_esp capture was inserted")


def main():
    fix = _load_fixup()

    # Prologue saves then a genuine argument. The capture belongs after the
    # four saves and before edx. This is sub_001EA770's shape (ledger #145).
    out = fix([
        "    PUSH32(esp, ebx);", "    PUSH32(esp, ebp);",
        "    PUSH32(esp, esi);", "    PUSH32(esp, edi);",
        "    eax = MEM32(ecx);", "    PUSH32(esp, edx);",
        "    PUSH32(esp, 0); RECOMP_ICALL_SAFE(MEM32(eax + 0x58), _icall_esp);",
        "    POP32(esp, edi);", "    POP32(esp, esi);",
        "    POP32(esp, ebp);", "    POP32(esp, ebx);",
    ])
    i = _capture_index(out)
    assert "PUSH32(esp, edx)" in out[i + 1], out
    assert all("PUSH32(esp, %s)" % r not in out[i + 1] for r in ("ebx", "ebp")), out

    # A register saved in the prologue and later passed as an argument. The
    # epilogue pops it either way, so the pop cannot classify the second push;
    # only "is this the register's first push" can. sub_00209650's shape.
    out = fix([
        "    PUSH32(esp, edi);", "    edi = eax;", "    edx = MEM32(eax);",
        "    PUSH32(esp, edi);", "    ecx = eax;",
        "    PUSH32(esp, 0); RECOMP_ICALL_SAFE(MEM32(edx + 0xFC), _icall_esp);",
        "    POP32(esp, edi);",
    ])
    i = _capture_index(out)
    assert i > 1, "capture must not precede the prologue save of edi:\n%s" % out
    assert "PUSH32(esp, edi)" in out[i + 1], out

    # A register pushed as an argument that the function never saves at all is
    # an argument, so the capture goes above it.
    out = fix([
        "    edx = MEM32(eax);", "    PUSH32(esp, esi);", "    ecx = eax;",
        "    PUSH32(esp, 0); RECOMP_ICALL_SAFE(MEM32(edx + 0x50), _icall_esp);",
    ])
    i = _capture_index(out)
    assert "PUSH32(esp, esi)" in out[i + 1], out

    print("ok - 3 cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
