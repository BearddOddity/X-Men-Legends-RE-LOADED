#!/usr/bin/env python3
"""Self-check for fix_stale_flags.py.

The rewrite edits generated C in place, so the failure mode is a build that
does not compile or, worse, one that compiles and branches differently than
intended. These cases pin the three things that would cause that: the
substitution happens in the condition, it does NOT happen in the branch body,
and running twice does not stack a second copy.

Case 1 is the real site from sub_001F09D0 that stopped the page-block table
from ever being populated.

    py -3 tools_data/test_fix_stale_flags.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_stale_flag_tests as det
import fix_stale_flags as fx

REAL_SITE = """\
void sub_001F09D0(void) {
loc_001F0AA9: ;
    ecx = MEM32(eax);
    eax = MEM32(0x5BC5D8);
    edx = MEM32(esi);
    (void)0; /* cmp ecx, eax - flags set for next jcc */
    ecx = esi;
    if (CMP_EQ(ecx, eax)) goto loc_001F0B03; /* je: equal / zero */
}
"""

# The tested register is never reassigned, so the re-read is already correct.
CLEAN = """\
void sub_00100000(void) {
loc_00100010: ;
    (void)0; /* cmp ecx, eax - flags set for next jcc */
    edx = esi;
    if (CMP_EQ(ecx, eax)) goto loc_00100020; /* je: equal / zero */
}
"""

# The body uses the clobbered register on purpose - it must survive untouched.
BODY_USES_REG = """\
void sub_00200000(void) {
loc_00200010: ;
    (void)0; /* test LO8(eax), LO8(eax) - flags set for next jcc */
    eax = edi;
    if (TEST_Z(LO8(eax), LO8(eax))) { esi = eax; return; } /* je: equal / zero */
}
"""


def run(src, only=None, apply=True):
    """Point the detector at a scratch tree and return the rewritten text."""
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "recomp_0014.c")
        open(path, "w", encoding="utf-8").write(src)
        old_gen = det.GEN
        det.GEN, fx.GEN = tmp, tmp
        try:
            by_file, cache = fx.plan(only)
            n = sum(len(v) for v in by_file.values())
            if apply and n:
                fx.apply(by_file, cache)
            return open(path, encoding="utf-8").read(), n
        finally:
            det.GEN, fx.GEN = old_gen, old_gen
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_real_site_is_rewritten():
    out, n = run(REAL_SITE)
    assert n == 1, n
    assert "uint32_t _sf6_ecx = ecx;" in out, out
    assert "if (CMP_EQ(_sf6_ecx, eax)) goto loc_001F0B03;" in out, out
    # the snapshot must be taken BEFORE the clobber, not after
    assert out.index("_sf6_ecx = ecx;") < out.index("ecx = esi;"), out


def test_uncloberred_site_untouched():
    out, n = run(CLEAN)
    assert n == 0, n
    assert out == CLEAN


def test_branch_body_keeps_live_register():
    """Substituting in the body would silently change what the branch does."""
    out, n = run(BODY_USES_REG)
    assert n == 1, n
    # the substitution goes inside the width wrapper, not around it
    assert "TEST_Z(LO8(_sf3_eax), LO8(_sf3_eax))" in out, out
    assert "{ esi = eax; return; }" in out, "body must keep the live eax"


def test_idempotent():
    once, n1 = run(REAL_SITE)
    twice, n2 = run(once)
    assert n1 == 1 and n2 == 0, (n1, n2)
    assert once == twice, "second pass must be a no-op"


def test_only_filter_matches_label_and_fileline():
    _, n = run(REAL_SITE, only="loc_001F0AA9")
    assert n == 1
    _, n = run(REAL_SITE, only="recomp_0014.c:6")
    assert n == 1
    _, n = run(REAL_SITE, only="loc_DEADBEEF")
    assert n == 0, "a non-matching filter must select nothing"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all cases hold")
