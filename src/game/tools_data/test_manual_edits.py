#!/usr/bin/env python3
"""Self-check for the three re-application rules that fixed the guard loss.

Each assert here corresponds to a bug that silently destroyed hand-written
guards on a regeneration, so a failure means that class of loss is back.

    py -3 tools_data/test_manual_edits.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manual_edits as m


def test_normalise_spans_the_abi_call_rewrite():
    """A direct call re-spelled by the generator is the same statement.

    This is what dropped all 18 D3D-null wraps: the stored `wrapped` run holds
    the bare-call spelling, the tree holds the ABI-wrapper spelling, and
    _match_run compares line by line.
    """
    old = "    PUSH32(esp, 0); sub_00119900(); /* call 0x00119900 */"
    new = "    PUSH32(esp, 0); RECOMP_ABI_CALL(sub_00119900); /* call 0x00119900 */"
    assert m._normalise(old) == m._normalise(new)
    # Different callee must NOT collapse together.
    other = "    PUSH32(esp, 0); RECOMP_ABI_CALL(sub_00119901); /* call */"
    assert m._normalise(new) != m._normalise(other)


def test_match_run_tolerates_the_rewrite():
    lines = [
        "void sub_001198B0(void)",
        "{",
        "    PUSH32(esp, eax);",
        "    PUSH32(esp, 0); RECOMP_ABI_CALL(sub_00119900); /* call 0x00119900 */",
        "    esp = esp + 8;",
        "}",
    ]
    run = [
        "    PUSH32(esp, eax);",
        "    PUSH32(esp, 0); sub_00119900(); /* call 0x00119900 */",
        "    esp = esp + 8;",
    ]
    assert m._match_run(lines, 2, len(lines), run) == 5
    assert m._match_run(lines, 3, len(lines), run) is None


def test_quota_inserts_only_the_deficit():
    """Identical guards: 4 recorded, 3 present -> insert exactly 1.

    Positional idempotency got this wrong and inserted 2, leaving 5.
    """
    block = ["    /* Manual guard (not in original x86): identical */",
             "    if (!ok) goto out;"]
    lines = ["void sub_001E8E20(void)", "{"]
    for _ in range(3):
        lines += block + ["    step();"]
    lines += ["}"]
    edits = [{"kind": "insert_before", "function": "sub_001E8E20",
              "block": block, "file": "x.c"} for _ in range(4)]
    quota = m._insert_quota(lines, edits)
    assert quota[("sub_001E8E20", tuple(block))] == 1, quota

    # Nothing missing -> nothing to insert. This is the idempotency guarantee.
    assert m._insert_quota(lines, edits[:3])[
        ("sub_001E8E20", tuple(block))] == 0


def test_moved_stub_idempotency_ignores_the_prose():
    """A re-worded moved-to-shim comment is still the same applied edit."""
    lines = ["/* sub_0035ADD0: moved to src/d3d8_shim.c - generated body "
             "removed so the hand-written definition links */"]
    e = {"block": ["/* sub_0035ADD0: moved to src/d3d8_shim.c "
                   "(native PC implementation) */"]}
    assert m._moved_already(lines, e)
    other = {"block": ["/* sub_0035AE60: moved to src/d3d8_shim.c "
                       "(native PC implementation) */"]}
    assert not m._moved_already(lines, other)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all rules hold")
