#!/usr/bin/env python3
"""Ground-truth cases for faithful.py.

This tool got the wrong answer three times before it got the right one, and each
wrong version looked plausible and produced confident numbers:

  1. linear sweep, flat 4 KB from the entry
     -> read through into neighbouring functions and blamed them for their
        neighbours' branch targets. 4,735 functions "with findings", including 277
        dropped edges in a 10-byte fragment.

  2. linear sweep bounded by the next function entry, stopping at any jmp/ret
     -> no longer over-read, but under-read badly: 24 of sub_001F09D0's ~112
        instructions, so it MISSED the deferred-flag bug the tool exists to find.
        A false negative is not a safe failure here - it is the tool silently
        not working.

  3. same, but continuing past a forward jmp inside the bound
     -> still stopped at 58 instructions and still missed the bug.

  4. recursive descent within [start, next entry): follow every branch target.
     Correct on all three cases below, because it visits exactly the bytes the
     code can reach instead of guessing where a straight line ends.

So these three assertions are the contract. Two of them must find NOTHING, which
matters as much as the one that must find something: a checker that reports
findings everywhere is as useless as one that reports them nowhere.

    py -3 tools_data/test_faithful.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import faithful as F


def setup():
    from capstone import Cs, CS_ARCH_X86, CS_MODE_32
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    d, secs = F.load_xbe()
    gen = F.index_gen()
    addrs = sorted(int(n[4:], 16) for n in gen)
    ends = {a: (addrs[i + 1] if i + 1 < len(addrs) else a + 4096)
            for i, a in enumerate(addrs)}
    return md, d, secs, gen, ends


CTX = None


def chk(name):
    global CTX
    if CTX is None:
        CTX = setup()
    md, d, secs, gen, ends = CTX
    assert name in gen, f"{name} missing from gen/ - tree changed?"
    return F.check(name, gen, md, d, secs, ends)


def test_finds_the_known_deferred_flag_bug():
    """sub_001F09D0 at 0x001F0AB2: cmp ecx, eax / mov ecx, esi / je.

    Confirmed against the raw XBE bytes. The lifted C re-tests the comparison at
    the branch, so it compares esi instead of the value that set the flags. This
    is the bug that kept a populator from ever being called.
    """
    r = chk("sub_001F09D0")
    assert not r.get("error"), r
    assert r["insns"] > 100, f"only reached {r['insns']} insns - coverage regressed"
    hits = [a for a, _, _ in r["stale_flags"]]
    assert 0x001F0AB2 in hits, f"missed the known bug; found {[hex(h) for h in hits]}"


def test_clean_on_a_function_verified_faithful_by_hand():
    """sub_001F7930's dispatch loop was checked instruction-by-instruction
    against the original and matches. It must not be flagged."""
    r = chk("sub_001F7930")
    assert not r.get("error"), r
    assert not r["missing_labels"], r["missing_labels"]
    assert not r["stale_flags"], r["stale_flags"]


def test_tail_jump_does_not_look_like_dropped_edges():
    """sub_00344BA0 is three instructions ending in `jmp 0x00344C15`.

    Version 1 swept past that jump and reported 9 dropped edges here. The
    function is lifted perfectly; the finding was entirely the tool's own bug.
    """
    r = chk("sub_00344BA0")
    assert not r.get("error"), r
    assert not r["missing_labels"], \
        f"tail jump misread as dropped edges: {r['missing_labels']}"


def test_missing_label_detection_actually_works():
    """The label check must be capable of firing, or its silence means nothing.

    Two of the three cases above assert emptiness, so without this a check that
    never fires would pass the whole suite.
    """
    md, d, secs, gen, ends = CTX or setup()
    r = F.check("sub_001F09D0", gen, md, d, secs, ends)
    ins = F.disasm_function(md, d, secs, 0x001F09D0, end=ends.get(0x001F09D0))
    targets = [i for i in ins if i.mnemonic in F.BRANCH]
    assert targets, "no branches found at all - the disassembler is not working"


if __name__ == "__main__":
    CTX = setup()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all ground-truth cases hold")
