#!/usr/bin/env python3
"""Self-check for find_icall_gaps.py's classifier.

The classifier decides which unresolved ICALL targets get offered for seeding,
and both kinds of mistake are expensive: a false LIKELY seeds junk (which the
project log records as having made things worse), a false UNLIKELY hides a real
function (which has cost days). Every case below is a REAL address from the
2026-08-05 sweep, with the verdict that turned out to be correct.

    py -3 tools_data/test_find_icall_gaps.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import find_icall_gaps as g


def test_run_once_guard_is_likely():
    """The shape that found all 13 real functions in the last sweep."""
    head = [("mov", "al, byte ptr [0x5bcad0]"), ("test", "al, al"),
            ("jne", "0x24605c")]
    verdict, reason = g.classify(0x00246030, head)
    assert verdict == "LIKELY", (verdict, reason)
    assert "static-initialiser" in reason, reason


def test_zero_run_is_unlikely():
    """`add byte ptr [eax], al` is 00 00 - padding, never a function."""
    verdict, _ = g.classify(0x000A72BC, [("add", "byte ptr [eax], al")])
    assert verdict == "UNLIKELY"


def test_junk_openers_are_unlikely():
    for mnem, ops in (("into", ""), ("pop", "ss"), ("push", "cs"),
                      ("int3", ""), ("cdq", ""), ("fnstsw", "ax"),
                      ("loopne", "0x211a8"), ("sbb", "al, 0xa1")):
        verdict, _ = g.classify(0x00176A58, [(mnem, ops)])
        assert verdict == "UNLIKELY", f"{mnem} {ops} should be UNLIKELY"


def test_wild_displacement_is_unlikely():
    """Huge displacements mean we are decoding data, not code."""
    verdict, _ = g.classify(0x00061710,
                            [("add", "byte ptr [ebp - 0x74b88b0a], al")])
    assert verdict == "UNLIKELY"


def test_boundary_test_overrides_a_plausible_prologue():
    """0x40000 decodes as a tidy `sub al, 1` but is mid-instruction.

    This is the case the prologue heuristic got WRONG on its own, and the
    reason the instruction-boundary test exists. Owner spans 0x3FACE-0x4009B
    and 0x40000 is not one of its instruction starts.
    """
    owner = (0x0003FACE, 0x0004009B, "sub_0003FACE", "recomp_0002.c")
    bounds = {0x0003FACE, 0x0003FFFE, 0x00040002}     # 0x40000 absent
    verdict, reason = g.classify(0x00040000, [("sub", "al, 1")], owner, bounds)
    assert verdict == "UNLIKELY", (verdict, reason)
    assert "instruction boundary" in reason or "NOT on an" in reason, reason


def test_boundary_test_rescues_an_odd_looking_opener():
    """`fnstsw ax` is a junk opener, but ON a boundary it is a real target.

    Observed at 0x002A6D7C, +0x590 into sub_002A67EC. The boundary test must
    win over the mnemonic blacklist, or genuine mid-function call targets get
    thrown away entirely.

    It classifies as FRAGMENT, not LIKELY. This test asserted LIKELY until
    2026-08-05, when acting on that verdict for a batch of 13 took the boot
    from 1452 kernel calls to 56. "Is this a real instruction?" and "is
    seeding it safe?" are different questions: a fragment duplicates its
    owner's tail and inherits a half-built frame, which is sometimes exactly
    right and sometimes catastrophic, and nothing in the disassembly says
    which. FRAGMENT means real-but-add-one-at-a-time.
    """
    owner = (0x002A67EC, 0x002A6DDD, "sub_002A67EC", "recomp_0018.c")
    bounds = {0x002A67EC, 0x002A6D7C}
    verdict, reason = g.classify(0x002A6D7C, [("fnstsw", "ax")], owner, bounds)
    assert verdict == "FRAGMENT", (verdict, reason)
    assert "one at a time" in reason, reason


def test_fragments_are_not_offered_for_bulk_add():
    """The regression guard: a boundary hit must never come back as LIKELY.

    --add only consumes LIKELY, so this is what keeps a batch of fragments
    from being seeded in one go again.
    """
    owner = (0x00014540, 0x0001459F, "sub_00014540", "recomp_0000.c")
    bounds = {0x00014540, 0x00014548}
    verdict, _ = g.classify(0x00014548, [("mov", "eax, dword ptr [edx]")],
                            owner, bounds)
    assert verdict != "LIKELY", "fragments must not be auto-added"


def test_owner_entry_is_not_missing():
    """If the target IS a known function's entry, it is not missing."""
    owner = (0x00246030, 0x00246090, "sub_00246030", "recomp_0017.c")
    verdict, reason = g.classify(0x00246030, [("mov", "al, byte ptr [0x5bcad0]")],
                                 owner, {0x00246030})
    assert verdict == "UNLIKELY", (verdict, reason)
    assert "already the entry" in reason, reason


def test_undecodable():
    verdict, _ = g.classify(0x00123456, None)
    assert verdict == "UNDECODABLE"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all classifier cases hold")
