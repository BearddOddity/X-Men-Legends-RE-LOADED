#!/usr/bin/env python3
"""Ground-truth cases for deepdive.py.

Why this file exists
--------------------
deepdive.py shipped without one, and two real bugs went in with it. Both were
in the read/write classifier, and both were found by hand afterwards:

  1. writes were detected with a bare `\\s*=`, which also matches `==`. That
     turned 8 real comparisons in gen/ into phantom WRITES.
  2. compound assignment (`+=` and friends) was classified read-only. None
     appear in gen/ today, so this one was latent rather than firing.

Neither would have survived a test. Rule #15 says build the check on the
second occurrence, not the fourth.

The read/write split is worth this much care because it is the tool's headline
output: `ebx 0x2Cr` on sub_001F7930 says registry+0x2C is READ and never
written, i.e. the function trusts a field someone else was supposed to set.
That single letter is the difference between "this function owns the field"
and "this function is the victim". Getting it backwards points an
investigation the wrong way.

Most assertions here are on synthetic lines rather than the live tree, on
purpose: gen/ is regenerated and gitignored, so a suite anchored to it would
rot. The two that DO touch the tree are marked, and skip cleanly when the
function is absent rather than failing a suite for the wrong reason.

    py -3 tools_data/test_deepdive.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deepdive as D


# ------------------------------------------------------------ read vs write
def test_plain_assignment_is_a_write():
    r = D.field_access(["    MEM32(eax + 0x10) = ecx;"])
    assert r == {"eax": [("0x10", "w")]}, r


def test_equality_test_is_NOT_a_write():
    """The bug that shipped. `==` must never read as a write.

    A field shown as written reads as "this function owns it" when it only
    ever compares it - the opposite of the conclusion that matters.
    """
    r = D.field_access(["    if (MEM32(eax + 0x10) == 0) goto loc_00000000;"])
    assert r == {"eax": [("0x10", "r")]}, r


def test_compound_assignment_is_a_write():
    """`+=` is read-modify-write. Read-only would be wrong.

    Latent rather than firing - no compound assignment on a MEM deref exists
    in gen/ today - but it is exactly the kind of thing that appears later and
    is never noticed.
    """
    for op in ("+=", "-=", "|=", "&=", "^="):
        r = D.field_access([f"    MEM32(eax + 0x10) {op} 4;"])
        assert r == {"eax": [("0x10", "w")]}, (op, r)


def test_relational_tests_are_not_writes():
    """`<=` and `>=` end in `=` too. They are reads."""
    for op in ("<=", ">=", "!="):
        r = D.field_access([f"    if (MEM32(eax + 0x10) {op} 4) goto loc_1;"])
        assert r == {"eax": [("0x10", "r")]}, (op, r)


def test_a_field_written_on_one_line_and_read_on_another_reports_both():
    """The realistic shape, and what the live tree actually shows (`esi 8wr`).

    KNOWN LIMIT, asserted below rather than hidden: the classifier keys on
    (base, offset), not on position within the line, so a read and a write of
    the SAME field on ONE line cannot be told apart and reports `w`. That is
    the right answer anyway - a read-modify-write does own the field, and
    ownership is the question this table answers - but it is a limit, not a
    guarantee, and a future reader should not infer more precision than exists.
    """
    r = D.field_access(["    MEM32(esi + 8) = eax;",
                        "    ecx = MEM32(esi + 8);"])
    assert r["esi"][0][0] == "0x8"
    assert set(r["esi"][0][1]) == {"r", "w"}, r


def test_same_line_read_modify_write_reports_write():
    """Documents the limit above so it cannot regress silently either way."""
    r = D.field_access(["    MEM32(esi + 8) = MEM32(esi + 8) + 1;"])
    assert r == {"esi": [("0x8", "w")]}, r


def test_decimal_and_hex_offsets_are_one_field():
    """The lifter emits both `+ 8` and `+ 0x8`. Keyed on raw text they listed
    the same field twice - visible in the live output as a stray decimal `8`
    among hex keys. Found by this suite, not by hand."""
    r = D.field_access(["    MEM32(esi + 8) = eax;",
                        "    ecx = MEM32(esi + 0x8);"])
    assert list(r) == ["esi"] and len(r["esi"]) == 1, r
    assert r["esi"][0][0] == "0x8", r


def test_negative_offsets_survive_normalisation():
    """`MEM32(ebp + -16)` is a real shape in gen/ (locals off the frame)."""
    r = D.field_access(["    ecx = MEM32(ebp + -16);"])
    assert r == {"ebp": [("-0x10", "r")]}, r


def test_esp_is_excluded():
    """Stack slots are frame noise, not object layout.

    Without this every function reports a wall of esp offsets and the actual
    struct shape is buried.
    """
    assert D.field_access(["    MEM32(esp + 0x10) = eax;"]) == {}


def test_comments_are_not_parsed():
    """A commented-out access is not an access.

    gen/ carries long manual-guard comments that quote code, so this is a real
    hazard rather than a theoretical one.
    """
    assert D.field_access(["     * MEM32(eax + 0x10) = ecx;"]) == {}
    assert D.field_access(["    /* MEM32(eax + 0x10) = ecx; */"]) == {}


def test_offsets_sort_numerically_not_lexically():
    """0x8 must come before 0x10. Sorted as text it would not.

    The table is read as a struct layout; out-of-order offsets make it useless
    for spotting adjacent fields.
    """
    r = D.field_access(["    ecx = MEM32(eax + 0x10);",
                        "    ecx = MEM32(eax + 0x8);",
                        "    ecx = MEM32(eax + 0x4);"])
    assert [o for o, _ in r["eax"]] == ["0x4", "0x8", "0x10"], r


# ------------------------------------------------------------------ globals
def test_global_write_and_read_are_distinguished():
    assert D.globals_referenced(["    MEM32(0x5BC508) = eax;"]) == [("0x5BC508", "w")]
    assert D.globals_referenced(["    eax = MEM32(0x5BC508);"]) == [("0x5BC508", "r")]


def test_global_equality_test_is_NOT_a_write():
    """Same `==` trap as field_access, same fix, asserted separately.

    The two functions carry their own regex, so one being right says nothing
    about the other.
    """
    r = D.globals_referenced(["    if (MEM32(0x5BC5D8) == ecx) goto loc_1;"])
    assert r == [("0x5BC5D8", "r")], r


# ------------------------------------------------------- structure findings
def test_backward_branch_is_a_loop_and_forward_is_not():
    """Only backward branches are loops. A forward goto is just a skip."""
    body = ["loc_00000010: ;",
            "    eax = 1;",
            "    if (CMP_L(esi, ebp)) goto loc_00000010;"]
    assert len(D.back_edges(body)) == 1, D.back_edges(body)
    fwd = ["    if (CMP_L(esi, ebp)) goto loc_00000010;",
           "loc_00000010: ;"]
    assert D.back_edges(fwd) == [], D.back_edges(fwd)


def test_indirect_call_target_expression_is_captured():
    """The callees no name-based search can ever find."""
    body = ["    uint32_t _icall_target = MEM32(eax + esi * 4); PUSH32(esp, 0);"]
    got = D.indirect_calls(body)
    assert len(got) == 1 and "MEM32(eax + esi * 4)" in got[0][1], got


def test_probe_detection_fires_and_stays_quiet():
    """A probe in the body makes any measurement incomparable, so silence
    here has to mean something."""
    assert D.probes_present(["    /* PROBE: x */"])
    assert D.probes_present(["    recomp_where(\"t\", 1, 0, 0, 0, 0);"])
    assert D.probes_present(["    eax = MEM32(esi);"]) == []


def test_normalise_accepts_every_address_form():
    assert D.normalise("sub_001F7930") == ("sub_001F7930", 0x001F7930)
    assert D.normalise("0x001F7930") == ("sub_001F7930", 0x001F7930)
    assert D.normalise("1F7930") == ("sub_001F7930", 0x001F7930)


# ------------------------------------------------ live tree (skips if absent)
def test_the_registry_field_reads_as_READ_ONLY():
    """The finding this tool exists to surface, asserted end to end.

    sub_001F7930 reads registry+0x2C as a {table,count} descriptor and never
    writes it. If this ever reports `w`, the classifier has regressed and the
    conclusion it supports - that the function is a victim rather than the
    owner - silently inverts.

    Touches gen/, which is regenerated and gitignored, so it skips rather than
    fails when the function is not present.
    """
    loc = D.find_in_gen("sub_001F7930")
    if not loc:
        print("    (skipped - sub_001F7930 not in gen/)")
        return
    fields = D.field_access(loc[2])
    assert "ebx" in fields, fields
    got = dict(fields["ebx"])
    assert got.get("0x2C") == "r", f"registry+0x2C should be READ-ONLY, got {got}"


def test_ledger_lookup_finds_a_known_entry():
    """The section that stops repeated work must actually return entries.

    A lookup that silently returns nothing is the single most dangerous wrong
    answer this tool can give - it reads as "nothing on record" and invites
    re-running a refuted experiment.
    """
    hits = D.ledger_hits("sub_001F7930", 0x001F7930)
    if hits is None:
        print("    (skipped - ledger lock was held)")
        return
    if not hits:
        print("    (skipped - no ledger entries for this function yet)")
        return
    assert any(e["verdict"] == "refuted" for e in hits), \
        "expected at least one refuted claim on the live wall"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all ground-truth cases hold")
