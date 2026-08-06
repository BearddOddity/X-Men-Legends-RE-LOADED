#!/usr/bin/env python3
"""Self-check for the shared bisect loop and the signal gate.

These two pieces now decide what every automated tool keeps or throws away, so
a mistake here silently discards good work or keeps a regression. Everything
below runs against a fake harness - no builds, no runs, nothing touched.

    py -3 tools_data/test_bisect_core.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signals
import bisect_core as bc

# bc.bisect() journals every evaluation. Left alone, running these tests
# appends dozens of fake verdicts to the real bisect_journal.jsonl and
# corrupts the record an unattended run is supposed to leave behind.
bc.journal = lambda *args, **kwargs: None

GOOD = {"kernel_calls": 1452, "failed_icalls": 30, "heap_allocs": 8, "safe_stub": 5}


class Fake(bc.Harness):
    """Any subset containing `culprit` measures worse. Nothing is built."""
    name = "fake"

    def __init__(self, items, culprit=None, build_fails=(), hang=False):
        self._items, self.culprit = items, culprit
        self.build_fails, self.hang = set(build_fails), hang
        self.applied, self.restores, self.current = [], 0, []

    def items(self):        return list(self._items)
    def snapshot(self):     return "tok"
    def restore(self, tok): self.restores += 1; self.current = []

    def apply(self, subset):
        self.applied.append(list(subset))
        self.current = list(subset)
        return not (self.build_fails & set(subset))

    def measure(self):
        sig = dict(GOOD)
        sig["hung"] = self.hang
        if self.culprit in self.current:
            sig["kernel_calls"] = 56          # the real 2026-08-05 regression
        return sig


def run(h, cands=None):
    base = dict(GOOD, hung=False)
    return bc.bisect(h, cands if cands is not None else h.items(), base, "tok")


def test_all_clean_keeps_everything_in_one_step():
    h = Fake(list("abcdefgh"))
    safe = run(h)
    assert safe == list("abcdefgh"), safe
    assert len(h.applied) == 1, "a clean list must not be split at all"


def test_isolates_the_single_culprit():
    h = Fake(list("abcdefgh"), culprit="e")
    safe = run(h)
    assert "e" not in safe, safe
    assert sorted(safe) == list("abcdfgh"), safe


def test_log2_not_linear():
    """The whole point: 8 candidates must not cost 8+ builds to clear."""
    h = Fake(list("abcdefgh"), culprit="e")
    run(h)
    assert len(h.applied) <= 9, f"{len(h.applied)} builds is near-linear"


def test_restores_between_branches():
    """Without a restore, the right branch inherits the left branch's edits."""
    h = Fake(list("abcd"), culprit="a")
    run(h)
    assert h.restores > 0, "bisect must restore between branches"


def test_build_failure_counts_as_worse_not_as_pass():
    """A subset that will not build must be rejected, never silently kept."""
    h = Fake(list("abcd"), build_fails={"c"})
    safe = run(h)
    assert "c" not in safe, safe


def test_gate_directions():
    base = dict(GOOD)
    assert not signals.worse_than(dict(base), base)
    assert signals.worse_than(dict(base, kernel_calls=56), base)      # fell
    assert not signals.worse_than(dict(base, kernel_calls=9000), base)
    assert signals.worse_than(dict(base, failed_icalls=99), base)     # rose
    assert signals.worse_than(dict(base, heap_allocs=0), base)        # fell


def test_safe_stub_never_gates():
    """It is watchdog time-boxed and varies run to run; gating on it is noise."""
    base = dict(GOOD)
    assert not signals.worse_than(dict(base, safe_stub=0), base)
    assert not signals.worse_than(dict(base, safe_stub=999999), base)


def test_hang_is_visible_in_the_summary():
    """A hung run must never be mistaken for a clean one when read by a human."""
    assert "HUNG" in signals.fmt(dict(GOOD, hung=True))
    assert "HUNG" not in signals.fmt(dict(GOOD, hung=False))


def test_stale_log_is_visible_and_never_silent():
    """The 2026-08-06 trap: a log older than the tree still returns numbers.

    Reading them straight out produced a confident 1452 -> 62 "regression"
    that had never actually been measured. fmt() must say so out loud.
    """
    assert "STALE" in signals.fmt(dict(GOOD, stale=True,
                                       stale_because=["seed_list.json"]))
    assert "STALE" not in signals.fmt(dict(GOOD, stale=False))


def test_parse_counts_real_log_shapes():
    text = ("[KERNEL] #1 foo\n[KERNEL] #2 bar\n"
            "Failed to resolve VA 0x123\n[HEAP] #1 alloc\n")
    sig = signals.parse(text)
    assert sig["kernel_calls"] == 2 and sig["failed_icalls"] == 1
    assert sig["heap_allocs"] == 1 and not sig["hung"]
    assert signals.parse("[WATCHDOG] No progress")["hung"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all cases hold")
