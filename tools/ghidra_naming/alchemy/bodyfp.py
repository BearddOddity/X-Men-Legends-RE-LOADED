#!/usr/bin/env python3
"""bodyfp.py - normalise x86 function bodies into comparable fingerprints.

Both sides of the match run through this one module. The game and the Alchemy
2.5 DLLs are different builds of nearby source, so the fingerprint has to
survive the two skews that were measured between them:

  * data layout - the same member sits at this+0x10 in the game and this+0x14
    in 2.5, a uniform 4 byte difference
  * vtable slots - the same method is slot 21 in the game and slot 19 in 2.5,
    a per class difference

so displacements carry no reliable signal and are masked out. What is left -
the opcode sequence, the operand shapes, the call structure and the referenced
constants - did not move between the versions.

Two strengths:

  LOOSE  mnemonic + operand kinds. Used to pull candidates.
  STRICT LOOSE plus register names and small literal immediates. Used to score.
"""
import capstone

_MD = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
_MD.detail = True

WINDOW = 64          # instructions compared per function
SMALL_IMM = 0x1000   # below this an immediate is a literal, not an address


def _op_tokens(insn, strict):
    out = []
    for op in insn.operands:
        if op.type == capstone.x86.X86_OP_REG:
            out.append(insn.reg_name(op.reg) if strict else "r")
        elif op.type == capstone.x86.X86_OP_IMM:
            v = op.imm
            # An address-sized immediate is a relocation in disguise; only a
            # small literal (a count, a flag, an enum) means the same thing in
            # both builds.
            if strict and -SMALL_IMM < v < SMALL_IMM:
                out.append("i%d" % v)
            else:
                out.append("i")
        else:
            # Memory. The displacement is exactly what the two builds disagree
            # about, so it never enters the token.
            base = insn.reg_name(op.mem.base) if op.mem.base else "-"
            idx = insn.reg_name(op.mem.index) if op.mem.index else "-"
            if strict:
                out.append("m[%s+%s*%d]" % (base, idx, op.mem.scale))
            else:
                out.append("m")
    return ",".join(out)


def tokens(code, addr=0, window=WINDOW, strict=False):
    """Normalised token list for a function body."""
    toks = []
    for insn in _MD.disasm(code, addr):
        toks.append("%s %s" % (insn.mnemonic, _op_tokens(insn, strict)))
        if len(toks) >= window:
            break
    return toks


def call_targets(code, addr):
    """Absolute targets of direct calls, in order.

    The call graph is the corroboration signal: a body match is only believed
    when the things it calls also match.
    """
    out = []
    for insn in _MD.disasm(code, addr):
        if insn.mnemonic != "call":
            continue
        if not insn.operands:
            continue
        op = insn.operands[0]
        if op.type == capstone.x86.X86_OP_IMM:
            out.append(op.imm)
    return out


def disp_deltas(code, addr=0, window=WINDOW):
    """Differences between consecutive memory displacements.

    Masking displacements is what lets a match survive the version skew, but it
    also collapses a whole family of functions onto one fingerprint: every
    Alchemy destructor is the same release-a-member block repeated, and only
    the offsets say which class it belongs to.

    The absolute offsets are not comparable across versions - the base differs
    by a constant. The differences between them are, because a constant shift
    cancels. So this is the discriminator that separates two functions of
    identical shape without reintroducing the skew.
    """
    disps = []
    for insn in _MD.disasm(code, addr):
        for op in insn.operands:
            if op.type == capstone.x86.X86_OP_MEM and op.mem.base:
                disps.append(op.mem.disp)
        if len(disps) >= window:
            break
    return [b - a for a, b in zip(disps, disps[1:])]


def key(toks):
    return "\n".join(toks)
