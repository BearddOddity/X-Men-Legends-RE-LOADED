"""
Reusable fixup for a systematic lifter bug found while debugging X-Men
Legends: `call dword ptr [esp+N]` (an indirect call whose target is read
relative to esp) was translated as

    PUSH32(esp, 0); RECOMP_ICALL_SAFE(MEM32(esp + N), _icall_esp);

Real x86 computes the call's memory operand BEFORE pushing the return
address; the dummy `PUSH32(esp, 0)` above models that push but runs
*before* `MEM32(esp + N)` is evaluated, so the operand is read 4 bytes
too deep - it picks up whatever was pushed by the previous statement
(often 0, or the caller's own dummy return-address slot) instead of the
real call target. Concretely: sub_00209650's `call [esp+8]` (real x86:
push edi; call [esp+8], target correctly = the caller's pushed function
pointer) was instead reading the caller's dummy return-address slot
(0x00000000) after the lift, causing the ICALL to silently fail and
skip the callee entirely.

Verified (2026-07-29 session): confirmed via raw capstone disassembly
of sub_00209650 (VA 0x00209650) against the generated C - real x86 reads
[esp+8] with esp as it stood right after `push edi`, i.e. BEFORE the
call instruction's own implicit return-address push. Root-caused and
fixed at the source in tools/recomp/lifter.py's _lift_call(); this
script reapplies the equivalent patch to already-generated
src/recomp/gen/*.c files so a full pipeline regeneration isn't required.

The fix captures the target into a temp *before* the dummy push:

    uint32_t _icall_target = MEM32(esp + N);
    PUSH32(esp, 0); RECOMP_ICALL_SAFE(_icall_target, _icall_esp);

Run this after any regeneration of src/recomp/gen/*.c via the
disasm/func_id/recomp pipeline, in case the lifter.py fix regresses or
the pipeline is re-run from an older lifter.py.

Usage: run from src/game/ (or point glob at wherever recomp_*.c live).
    py -3 fix_icall_esp_operand.py
"""
import re
import glob

# Matches: <indent>PUSH32(esp, 0); RECOMP_ICALL_SAFE(<target with esp in it>, _icall_esp); /* indirect call */
PATTERN = re.compile(
    r"^(?P<indent>[ \t]*)PUSH32\(esp, 0\); RECOMP_ICALL_SAFE\((?P<target>[^,]*\besp\b[^,]*), _icall_esp\); /\* indirect call \*/$",
    re.MULTILINE,
)


def fix_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    def repl(m):
        indent = m.group("indent")
        target = m.group("target")
        return (
            f"{indent}uint32_t _icall_target = {target}; "
            f"PUSH32(esp, 0); RECOMP_ICALL_SAFE(_icall_target, _icall_esp); /* indirect call */"
        )

    new_content, n = PATTERN.subn(repl, content)
    if n:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_content)
    return n


if __name__ == "__main__":
    grand_total = 0
    for f in sorted(glob.glob("src/recomp/gen/recomp_*.c")):
        n = fix_file(f)
        if n:
            print(f, "fixed", n)
            grand_total += n
    print("GRAND TOTAL:", grand_total)
