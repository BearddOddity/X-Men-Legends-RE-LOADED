"""Check trace_seeds inserts a probe, is idempotent, and skips absent functions."""
import importlib.util
import os
import sys
import tempfile

spec = importlib.util.spec_from_file_location(
    "bs", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bisect_seeds.py"))
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)

d = tempfile.mkdtemp()
gen = os.path.join(d, "src", "recomp", "gen")
os.makedirs(gen)
seed = os.path.join(gen, "recomp_seed.c")
open(seed, "w").write(
    "void sub_001FE670(void)\n{\n    eax = 0;\n}\n"
    "void sub_00112233(void)\n{\n    eax = 1;\n}\n")

bs.GAME = d
bs.trace_seeds([0x1FE670, 0xDEADBE])   # second one is not in the file
t = open(seed).read()
assert t.count('recomp_where("seedhit-001FE670"') == 1, t
assert "seedhit-00DEADBE" not in t, "probed a function that is not there"
assert t.count("/* PROBE */") == 2, t          # one open, one close on one line
assert "void sub_00112233(void)\n{\n    eax = 1;" in t, "untouched fn changed"

bs.trace_seeds([0x1FE670])                      # idempotent
t2 = open(seed).read()
assert t2 == t, "second pass modified the file"

print("tracer ok - inserts once, skips absent functions, idempotent")
