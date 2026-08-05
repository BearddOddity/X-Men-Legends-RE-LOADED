#!/usr/bin/env python3
"""Self-check for the x87 lifting rules.

Small on purpose. It covers the two things that were actually wrong before
and that no build error would catch:

  1. mnemonic decomposition - the p/r/i suffix grammar
  2. stack depth - how many pushes and pops each form performs

Depth is the one that matters. Every st(n) reference is relative to TOP, so
a single missing pop silently shifts every later register access in the
function. Value bugs are local; depth bugs cascade.

Run: py -3 -m tools.recomp.test_fpu
"""
from .disasm import Instruction, Operand
from .lifter import _fpu_decompose, _st_index, _st_expr


def _st(n):
    return Operand(type="reg", reg="st(%d)" % n)


def _mem(size, disp=0x400000):
    return Operand(type="mem", mem_disp=disp, mem_size=size)


def test_decompose():
    # (mnemonic, operator, integer source, reversed, pops)
    cases = {
        "fadd":    ("+", False, False, False),
        "faddp":   ("+", False, False, True),
        "fsub":    ("-", False, False, False),
        "fsubr":   ("-", False, True,  False),
        "fsubp":   ("-", False, False, True),
        "fsubrp":  ("-", False, True,  True),
        "fmul":    ("*", False, False, False),
        "fmulp":   ("*", False, False, True),
        "fdiv":    ("/", False, False, False),
        "fdivr":   ("/", False, True,  False),
        "fdivp":   ("/", False, False, True),
        "fdivrp":  ("/", False, True,  True),
        "fiadd":   ("+", True,  False, False),
        "fisub":   ("-", True,  False, False),
        "fisubr":  ("-", True,  True,  False),
        "fidiv":   ("/", True,  False, False),
        "fidivr":  ("/", True,  True,  False),
        "fimul":   ("*", True,  False, False),
    }
    for m, want in cases.items():
        got = _fpu_decompose(m)
        assert got == want, "%s: got %r want %r" % (m, got, want)

    # Must NOT be treated as arithmetic.
    for m in ("fld", "fstp", "fcomp", "fnstsw", "fxch", "fld1", "fldcw",
              "fabs", "fchs", "fsqrt", "fild", "fistp", "fldz"):
        assert _fpu_decompose(m) is None, "%s wrongly parsed as arithmetic" % m


def test_st_index():
    assert _st_index(_st(0)) == 0
    assert _st_index(_st(3)) == 3
    assert _st_index(_mem(4)) is None
    assert _st_index(Operand(type="reg", reg="eax")) is None
    assert _st_index(None) is None
    assert _st_expr(0) == "fp_top()"
    assert _st_expr(2) == "fp_st(2)"


class _Lifter:
    """Minimal stand-in - _lift_fpu only touches its arguments."""
    pass


def _lift(m, ops, op_str=""):
    from .lifter import Lifter
    insn = Instruction(address=0x1000, size=2, mnemonic=m, op_str=op_str,
                       bytes_hex="0000", operands=list(ops))
    return Lifter._lift_fpu(_Lifter(), insn, m, list(ops))[0]


def _depth(line):
    """Net stack movement of one emitted line: +1 per push, -1 per pop."""
    return line.count("fp_push(") - line.count("fp_pop()")


def test_depth():
    # (mnemonic, operands, expected net depth change)
    cases = [
        ("fld",    [_mem(4)],           +1),
        ("fld",    [_mem(8)],           +1),
        ("fld",    [_st(2)],            +1),
        ("fild",   [_mem(4)],           +1),
        ("fldz",   [],                  +1),
        ("fld1",   [],                  +1),

        ("fst",    [_mem(4)],            0),   # was -1: fst must NOT pop
        ("fst",    [_st(1)],             0),
        ("fstp",   [_mem(4)],           -1),
        ("fstp",   [_mem(8)],           -1),
        ("fstp",   [_st(0)],            -1),   # was 0: fstp always pops
        ("fstp",   [_st(3)],            -1),

        ("fist",   [_mem(4)],            0),
        ("fistp",  [_mem(4)],           -1),   # was 0

        ("fadd",   [_mem(4)],            0),   # was -1: memory form never pops
        ("fadd",   [_st(0), _st(2)],     0),
        ("faddp",  [_st(1), _st(0)],    -1),
        ("faddp",  [],                  -1),
        ("fmul",   [_mem(8)],            0),   # was -1
        ("fdivp",  [_st(1), _st(0)],    -1),
        ("fsubr",  [_mem(4)],            0),

        ("fcom",   [_st(1)],             0),
        ("fcomp",  [_st(1)],            -1),   # was 0: fcomp pops once
        ("fcomp",  [_mem(4)],           -1),
        ("fcompp", [],                  -2),   # was 0: fcompp pops twice
        ("fucompp", [],                 -2),

        ("fxch",   [_st(1)],             0),
        ("fnstsw", [],                   0),
        ("fabs",   [],                   0),
        ("fchs",   [],                   0),
    ]
    for m, ops, want in cases:
        line = _lift(m, ops)
        got = _depth(line)
        assert got == want, "%s %r: depth %+d, want %+d\n    %s" % (
            m, [o.type for o in ops], got, want, line)


def test_reverse_and_dest():
    # fsubr st(0), st(2)  ->  st(0) = st(2) - st(0)
    line = _lift("fsubr", [_st(0), _st(2)])
    assert "fp_top() = fp_st(2) - fp_top();" in line, line
    # fsub st(0), st(2)   ->  st(0) = st(0) - st(2)
    line = _lift("fsub", [_st(0), _st(2)])
    assert "fp_top() = fp_top() - fp_st(2);" in line, line
    # fsubp st(1), st(0)  ->  st(1) = st(1) - st(0), then pop
    line = _lift("fsubp", [_st(1), _st(0)])
    assert "fp_st(1) = fp_st(1) - fp_top(); fp_pop();" in line, line
    # fdivrp st(1), st(0) ->  st(1) = st(0) - ... reversed division
    line = _lift("fdivrp", [_st(1), _st(0)])
    assert "fp_st(1) = fp_top() / fp_st(1); fp_pop();" in line, line


def test_no_undefined_push():
    """fld st(n) must read into a temp, not inline fp_st(n) into fp_push.

    fp_push predecrements g_fp_top; the macro's subscript and its value
    expression are unsequenced, so an inlined read is undefined behaviour.
    """
    line = _lift("fld", [_st(2)])
    assert "double _t = fp_st(2); fp_push(_t);" in line, line
    assert "fp_push(fp_st(" not in line, line


def test_status_word():
    """fnstsw must build the bits that 'test ah, 0x41' actually reads."""
    line = _lift("fnstsw", [], "ax")
    assert "g_eax" in line, line
    # C0 (ah bit 0) when st(0) < src; C3 (ah bit 6) when equal.
    assert "0x0100u" in line and "0x4000u" in line, line


def _lift_sse(m, ops, op_str=""):
    from .lifter import Lifter
    insn = Instruction(address=0x1000, size=3, mnemonic=m, op_str=op_str,
                       bytes_hex="000000", operands=list(ops))
    return Lifter._lift_sse(_Lifter(), insn, m, list(ops))[0]


def _mm(n):
    return Operand(type="reg", reg="mm%d" % n)


def _imm(v):
    return Operand(type="imm", imm=v)


def test_mmx():
    """MMX must emit real calls, not comments. It used to emit comments."""
    line = _lift_sse("paddw", [_mm(0), _mm(1)])
    assert line.startswith("mm0 = mmx_paddw(mm0, mm1);"), line

    # Memory source is a 64-bit load, not MEM32.
    line = _lift_sse("por", [_mm(2), _mem(8, 0x430000)])
    assert "mmx_por(mm2, MEM64(" in line, line

    # Shift counts arrive as immediates.
    line = _lift_sse("psrlq", [_mm(2), _imm(0x20)])
    assert "mmx_psrlq(mm2, 32ULL)" in line, line

    # pextrw writes a general register, so it is not a mmx_ assignment.
    line = _lift_sse("pextrw", [Operand(type="reg", reg="eax"), _mm(3), _imm(2)])
    assert "mmx_pextrw(mm3, 2)" in line, line

    # movq in all three directions.
    assert _lift_sse("movq", [_mm(0), _mm(1)]).startswith("mm0 = mm1;")
    assert "MEM64(" in _lift_sse("movq", [_mm(0), _mem(8, 0x430000)])
    assert "MEM64(" in _lift_sse("movq", [_mem(8, 0x430000), _mm(0)])

    # No MMX op may still be emitted as a bare comment.
    for m in ("paddw", "psubsw", "pmaddwd", "paddd", "packuswb",
              "punpcklbw", "pmullw", "psraw", "pxor"):
        line = _lift_sse(m, [_mm(0), _mm(1)])
        assert not line.lstrip().startswith("/*"), "%s still a no-op: %s" % (m, line)

    # 3DNow! is unreachable on a Pentium III and must say so, not pretend.
    line = _lift_sse("pfmul", [_mm(0), _mm(1)])
    assert "3DNow" in line, line

    # shufps must be honest about needing a wider xmm model.
    line = _lift_sse("shufps", [Operand(type="reg", reg="xmm0"),
                                Operand(type="reg", reg="xmm1"), _imm(0x93)])
    assert "4-LANE" in line, line


def demo():
    test_decompose()
    test_st_index()
    test_depth()
    test_reverse_and_dest()
    test_no_undefined_push()
    test_status_word()
    test_mmx()
    print("x87 and MMX lifting self-check: all passed")


if __name__ == "__main__":
    demo()
