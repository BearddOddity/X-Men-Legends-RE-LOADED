#!/usr/bin/env python3
"""oracle.py - ask the real game a question.

xemu running the retail disc is the only source of ground truth this project
has. Every question put to it today was written as a throwaway .gdb file, which
made each query cost several minutes of typing; that friction is why the oracle
got used to kill one hypothesis at a time instead of as a reflex. This wraps the
whole round trip so a question costs one command.

Usage
-----
    # what does this function see when it is entered?
    oracle.py --at 0x209670 --dump edi,[edi],[0x5BC5D8] --hits 20

    # only the calls that carry -1
    oracle.py --at 0x1FBA90 --dump [esp+4] --when '*(unsigned int*)($esp+4) == 0xFFFFFFFF'

    # what does the original actually do here?
    oracle.py --disas 0x2235d0:14

    # stack depth at each frame of a chain, which is the measurement that
    # names a stack-imbalance culprit without needing a hypothesis
    oracle.py --depth 0x2D690,0x6BF80,0x209650,0x2225F0,0x2235D0

Dump expression shorthand
-------------------------
Registers are written bare and memory in brackets, because that is how the
addresses actually get discussed:

    edi             -> $edi
    [edi]           -> *(unsigned int*)($edi)
    [edi+0xA4]      -> *(unsigned int*)($edi+0xA4)
    [esp+4]         -> *(unsigned int*)($esp+4)
    [0x5BC5D8]      -> *(unsigned int*)(0x5BC5D8)

Anything containing a '$' or a '*' is passed to gdb untouched, so the full
expression language is still available when the shorthand is not enough.

Comparing against our build
---------------------------
Compare structure, never addresses. The real heap sits at 0x0080xxxx and ours at
0x0109xxxx; that difference is expected and is not a defect. Call order, stack
depth, and zero versus non-zero are the comparisons that mean something.
"""
import argparse
import os
import re
import subprocess
import sys
import time

XEMU_DIR = "/home/oddity/tools/xemu"
APPIMAGE = os.path.join(XEMU_DIR, "xemu.AppImage")
CONFIG = os.path.join(XEMU_DIR, "xemu.toml")
ISO = os.path.join(XEMU_DIR, "data/xmen.iso")
PORT = 1234

MEM = re.compile(r"^\[\s*([^\]]+?)\s*\]$")
REGS = {"eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp", "eip"}


def gdb_expr(tok):
    """Turn the bracket shorthand into a gdb expression."""
    tok = tok.strip()
    if "$" in tok or "*" in tok:
        return tok                      # already a gdb expression
    m = MEM.match(tok)
    if m:
        inner = m.group(1)
        for r in REGS:                  # edi+0xA4 -> $edi + 0xA4
            inner = re.sub(r"\b%s\b" % r, "$" + r, inner)
        return "*(unsigned int*)(%s)" % inner
    if tok in REGS:
        return "$" + tok
    return tok


def xemu_running():
    return subprocess.run(["pgrep", "-f", "xemu.AppImage"],
                          capture_output=True).returncode == 0


def start_xemu(wait=25):
    """Launch xemu frozen at reset with the gdb stub open."""
    if xemu_running():
        return True
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":10"))
    subprocess.Popen(
        ["setsid", APPIMAGE, "-config_path", CONFIG, "-dvd_path", ISO,
         "-s", "-S"],
        cwd=XEMU_DIR, env=env,
        stdout=open("/tmp/xemu.log", "w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    for _ in range(wait):
        time.sleep(1)
        if xemu_running():
            time.sleep(3)               # let the stub bind
            return True
    return False


def _budget(args):
    """Stop the whole run once enough has been seen.

    A script that only ends at the gdb timeout holds its caller open for the
    full duration and then dies by signal, losing the output that was already
    collected. Quitting on a budget makes every query terminate on its own.
    """
    return ("  set $total = $total + 1\n"
            "  if $total >= %d\n    detach\n    quit\n  end" % args.budget)


def build_script(args):
    out = ["set pagination off", "set confirm off", "set architecture i386",
           "target remote localhost:%d" % PORT, "set $total = 0", ""]

    for i, spec in enumerate(args.at):
        addr, _, label = spec.partition(":")
        label = label or addr
        dumps = args.dump[i] if i < len(args.dump) else ""
        when = args.when[i] if i < len(args.when) else ""
        exprs = [e for e in dumps.split(",") if e.strip()]

        fmt = "  ".join("%s=0x%%08x" % e.strip() for e in exprs)
        vals = ", ".join(gdb_expr(e) for e in exprs)

        out.append("set $n%d = 0" % i)
        out.append("break *%s" % addr)
        out.append("commands")
        out.append("  set $n%d = $n%d + 1" % (i, i))
        cond = "$n%d <= %d" % (i, args.hits)
        if when:
            cond = "(%s) && (%s)" % (cond, when)
        out.append("  if %s" % cond)
        if exprs:
            out.append('    printf "[%s #%%d] %s\\n", $n%d, %s'
                       % (label, fmt, i, vals))
        else:
            out.append('    printf "[%s #%%d] hit\\n", $n%d' % (label, i))
        out.append("  end")
        out.append(_budget(args))
        out.append("  continue")
        out.append("end")
        out.append("")

    for spec in args.disas:
        addr, _, count = spec.partition(":")
        out.append("break *%s" % addr)
        out.append("commands")
        out.append('  printf "\\n=== disassembly at %s ===\\n"' % addr)
        out.append("  x/%si %s" % (count or "16", addr))
        out.append("  detach")
        out.append("  quit")
        out.append("end")
        out.append("")

    # --depth: one breakpoint per function, printing only the stack pointer.
    # A frame whose depth disagrees with our build's is the culprit, and this
    # needs no hypothesis about which function is at fault.
    for addr in args.depth:
        out.append("break *%s" % addr)
        out.append("commands")
        out.append('  printf "[depth %s] esp=0x%%08x  ebp=0x%%08x\\n", $esp, $ebp'
                   % addr)
        out.append(_budget(args))
        out.append("  continue")
        out.append("end")
        out.append("")

    out.append("continue")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", action="append", default=[],
                    help="ADDR[:LABEL] to break on; repeatable")
    ap.add_argument("--dump", action="append", default=[],
                    help="comma-separated expressions for the matching --at")
    ap.add_argument("--when", action="append", default=[],
                    help="gdb condition for the matching --at")
    ap.add_argument("--disas", action="append", default=[],
                    help="ADDR[:COUNT] - disassemble there, then detach")
    ap.add_argument("--depth", default="",
                    help="comma-separated addresses; print esp at each entry")
    ap.add_argument("--hits", type=int, default=12,
                    help="stop printing a site after this many hits")
    ap.add_argument("--budget", type=int, default=60,
                    help="total breakpoint hits before the run ends itself")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--raw", action="store_true", help="show all gdb output")
    ap.add_argument("--script", action="store_true",
                    help="print the generated gdb script and exit")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    a.depth = [d for d in a.depth.split(",") if d.strip()]
    if not (a.at or a.disas or a.depth):
        ap.error("nothing to ask - give --at, --disas or --depth")

    script = build_script(a)
    if a.script:
        sys.stdout.write(script)
        return 0

    if not start_xemu():
        sys.exit("xemu did not come up - check /tmp/xemu.log")

    path = "/tmp/oracle_gen.gdb"
    with open(path, "w") as fh:
        fh.write(script)

    r = subprocess.run(["timeout", str(a.timeout), "gdb-multiarch",
                        "-q", "-batch", "-x", path],
                       capture_output=True, text=True)
    text = r.stdout + r.stderr
    if a.raw:
        print(text)
        return 0

    lines = [l for l in text.split("\n")
             if l.startswith("[") or l.startswith("===") or "   0x" in l]
    if not lines:
        print("no output - the breakpoints were never reached.")
        print("that is itself an answer: the real game does not execute there.")
        return 1
    print("\n".join(lines))
    return 0


def selftest():
    """The shorthand translation is the only real logic here, so check it."""
    cases = [
        ("edi", "$edi"),
        ("[edi]", "*(unsigned int*)($edi)"),
        ("[edi+0xA4]", "*(unsigned int*)($edi+0xA4)"),
        ("[esp+4]", "*(unsigned int*)($esp+4)"),
        ("[0x5BC5D8]", "*(unsigned int*)(0x5BC5D8)"),
        ("$eax", "$eax"),
        ("*(unsigned char*)$esi", "*(unsigned char*)$esi"),
    ]
    bad = 0
    for src, want in cases:
        got = gdb_expr(src)
        ok = got == want
        bad += not ok
        print("%s %-24s -> %s" % ("ok " if ok else "FAIL", src, got))

    class A:
        at = ["0x209670:probe"]
        dump = ["edi,[edi]"]
        when = []
        disas = []
        depth = []
        hits = 5
        budget = 60
    s = build_script(A)
    for need in ("break *0x209670", "$n0 <= 5", "*(unsigned int*)($edi)",
                 "target remote localhost:1234"):
        ok = need in s
        bad += not ok
        print("%s script contains %r" % ("ok " if ok else "FAIL", need))

    print("\nall cases hold" if not bad else "\n%d FAILURE(S)" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
