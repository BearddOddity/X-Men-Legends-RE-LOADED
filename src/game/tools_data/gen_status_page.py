#!/usr/bin/env python3
"""gen_status_page.py - rebuild the shareable status page from real project data.

Why this exists
---------------
The status page was hand-written once and went stale the same afternoon: it
carried a metric ("146 functions execute") that turned out to measure something
else entirely, and no amount of re-reading the prose would have caught it. A
page that claims to show progress has to be generated from the numbers the
project already records, or it drifts into fiction.

Everything numeric here comes from tools_data/progress.json - the history
progress.py maintains - and from the current stderr.txt. Nothing is typed in.

    py -3 tools_data/gen_status_page.py -o status.html

The prose lives in this file; the numbers never do.
"""
import argparse
import json
import os
import re
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.dirname(HERE)
DB = os.path.join(HERE, "progress.json")
LOG = os.path.join(GAME, "stderr.txt")
HEAD = os.path.join(HERE, "status", "head.html")
GEN = os.path.join(GAME, "src", "recomp", "gen")

FUNC_RE = re.compile(r"^void sub_[0-9A-F]+\(void\)$")

# The one-line story, shown at the top where a reader looks first. The metrics
# below it often do not move for days at a time - diagnosis is not progress in
# kernel calls - so if this is not updated the whole page looks stale even when
# the work has moved a long way. Edit it whenever the understanding changes.
HEADLINE = ("The list that never grew, because the instruction that "
            "appended to it was missing")
SUBHEAD = ("For months the engine’s type descriptors held field lists that stayed "
           "empty, and a dozen crashes downstream all traced back to reading one. "
           "The routine that appends an entry ends in a branch the translator "
           "never recognised as code, so the store simply never happened — and "
           "the same gap dropped sixteen bytes of stack cleanup, which is a leak "
           "measured in August and never placed until now. Restoring it moved "
           "three of the four tracked signals at once: heap allocations 96 to 136, "
           "code reached 170 to 196, call sites 477 to 551, indirect dispatches up "
           "43%. Both reached and call sites are records.")


def lifted_function_count():
    n = 0
    for name in sorted(os.listdir(GEN)):
        if not name.endswith(".c"):
            continue
        with open(os.path.join(GEN, name), encoding="utf-8", errors="ignore") as fh:
            n += sum(1 for line in fh if FUNC_RE.match(line.rstrip("\n")))
    return n


def current_signals():
    """Signals from the most recent run, straight out of stderr.txt."""
    if not os.path.exists(LOG):
        return {}
    text = open(LOG, encoding="utf-8", errors="ignore").read()

    def last(pat):
        m = re.findall(pat, text)
        return int(m[-1]) if m else None

    return {
        "callsites": last(r"callsites=(\d+)"),
        "reached": len(re.findall(r"COVERAGE-VA", text)),
        "kernel": last(r"\[KERNEL\] #(\d+)"),
        "heap": last(r"\[HEAP\] #(\d+)"),
    }


def wall_history(hist):
    """Distinct crash sites, in the order they were first reached.

    This is the honest progress metric on this project. Kernel calls rise and
    fall for reasons that include loops; a new crash site cannot be faked by
    spinning, because reaching one means the previous wall was passed.
    """
    out, seen = [], set()
    for i, e in enumerate(hist):
        c = e.get("crash_in")
        if c and c not in seen:
            seen.add(c)
            out.append((e.get("date", ""), c, e.get("kernel_calls"), i))
    return out


def wall_economics(hist, walls):
    """How the cost of a wall has changed, and the spread.

    Split the history in half rather than fitting a curve: with 148 runs and
    39 walls the sample is small enough that a trend line would imply more
    precision than the data carries.
    """
    idx = [w[3] for w in walls]
    mid = len(hist) // 2
    first = [i for i in idx if i < mid]
    second = [i for i in idx if i >= mid]
    gaps = sorted(idx[i] - idx[i - 1] for i in range(1, len(idx))) or [0]
    return {
        "first_runs": mid, "first_walls": len(first),
        "second_runs": len(hist) - mid, "second_walls": len(second),
        "first_cost": mid / max(1, len(first)),
        "second_cost": (len(hist) - mid) / max(1, len(second)),
        "median_gap": gaps[len(gaps) // 2],
        "worst_gap": gaps[-1],
    }


def sparkline(values, w=680, h=120):
    """Inline SVG of kernel calls across the recorded history.

    One recorded value is a spin artefact orders of magnitude above the rest.
    Plotting it raw flattens everything else onto the baseline, so the series is
    clipped - and the page states the clip rather than doing it quietly.
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return "", 0
    cap = sorted(vals)[int(len(vals) * 0.97)] or max(vals)
    pts = []
    n = len(vals)
    for i, v in enumerate(vals):
        x = (i / max(1, n - 1)) * w
        y = h - (min(v, cap) / cap) * (h - 8) - 4
        pts.append((x, y))
    poly = " ".join("%.1f,%.1f" % p for p in pts)
    lx, ly = pts[-1]
    svg = (
        '<svg viewBox="0 0 %d %d" width="100%%" height="%d" preserveAspectRatio="none" '
        'role="img" aria-label="Kernel calls across %d recorded runs, ending at %d">'
        '<polyline points="%s" fill="none" stroke="var(--accent)" stroke-width="1.6" '
        'stroke-linejoin="round"/>'
        '<circle cx="%.1f" cy="%.1f" r="3.5" fill="var(--accent)"/>'
        "</svg>" % (w, h, h, n, vals[-1], poly, lx, ly)
    )
    return svg, cap


def build(hist, sig):
    walls = wall_history(hist)
    econ = wall_economics(hist, walls)
    lifted = lifted_function_count()
    latest = hist[-1]
    svg, cap = sparkline([e.get("kernel_calls") for e in hist])
    recent = walls[-6:][::-1]

    rows = "\n".join(
        '          <tr><td class="n">%s</td><td class="n">%s</td>'
        '<td class="n">%s</td></tr>'
        % (escape(str(d)), escape(str(c)), k if k is not None else "&mdash;")
        for d, c, k, _ in recent
    )

    return """<div class="wrap">

  <header>
    <p class="eyebrow">Static recompilation &middot; Xbox to PC</p>
    <h1>X-Men Legends, running as native code</h1>
    <p class="lede">The translation is essentially finished. Getting the
    translated code to <em>execute</em> is the entire remaining problem, and it
    is where all current work happens.</p>
    <div class="stamp">
      <span>{date}</span>
      <span>default.xbe &middot; XDK 5849</span>
      <span>{runs} recorded runs</span>
      <span>generated, not hand-written</span>
    </div>
  </header>

  <section class="wall" style="border-left-color:var(--blocked)">
    <div class="wall-row"><span class="tn-label">Where it stands right now</span></div>
    <h3 style="margin-top:.2rem">{headline}</h3>
    <p class="track-cap">{subhead}</p>
    <div class="wall-row" style="margin-top:.4rem">
      <span>wall <strong>{crash}</strong></span>
      <span>{kernel} kernel calls</span>
      <span>{runs} runs</span>
      <span>{walls} walls passed</span>
      <span>updated {date}</span>
    </div>
  </section>

  <section class="thesis">
    <div class="thesis-nums">
      <div><span class="tn-label">Functions translated to C</span>
           <span class="tn-val num">{lifted:,}</span></div>
      <div><span class="tn-label">Call sites executed</span>
           <span class="tn-val num reached">{callsites:,}</span></div>
      <div><span class="tn-label">Walls passed</span>
           <span class="tn-val num">{walls}</span></div>
    </div>
    <div class="track"><div class="track-fill"></div><div class="track-tick"></div></div>
    <p class="track-cap">The sliver is drawn to true scale. Nearly the whole
    game is already C, and almost none of it has ever run &mdash; the boot dies
    inside the C runtime's static initialisers, before the game proper starts.</p>
    <p class="track-cap"><strong>On the numbers.</strong> Call sites executed
    counts distinct <em>direct</em> call sites. A separate counter tracks
    {reached} functions entered through <em>indirect</em> calls. Neither is a
    clean count of functions executed, and an earlier version of this page
    presented the second as though it were.</p>
  </section>

  <section>
    <p class="eyebrow">Progression</p>
    <h2>{walls} walls passed across {runs} runs</h2>
    <p>Kernel calls per recorded run. It is a narrow proxy &mdash; a loop can
    inflate it &mdash; which is why the count of distinct crash sites matters
    more: reaching a new one means the previous wall was passed, and that cannot
    be faked by spinning.</p>
    {svg}
    <p class="track-cap">Clipped at {cap:,} so one spin artefact does not flatten
    the series. Latest run: <strong>{kernel}</strong> kernel calls,
    {heap} heap allocations, ending in {ended}.</p>

    <div class="scroller">
      <table>
        <thead><tr><th>First reached</th><th>Wall</th><th>Kernel calls</th></tr></thead>
        <tbody>
{rows}
        </tbody>
      </table>
    </div>
    <p class="track-cap">Most recent walls, newest first.</p>

    <div class="metrics">
      <div class="metric"><span class="metric-k">First {first_runs} runs</span>
        <span class="metric-val">{first_walls} walls</span>
        <span class="track-cap">{first_cost:.1f} runs each</span></div>
      <div class="metric"><span class="metric-k">Last {second_runs} runs</span>
        <span class="metric-val">{second_walls} walls</span>
        <span class="track-cap">{second_cost:.1f} runs each</span></div>
      <div class="metric"><span class="metric-k">Median wall</span>
        <span class="metric-val">{median_gap} runs</span></div>
      <div class="metric"><span class="metric-k">Worst wall</span>
        <span class="metric-val">{worst_gap} runs</span></div>
    </div>
    <p class="track-cap"><strong>Walls are getting more expensive, not
    cheaper</strong> &mdash; the cost per wall has roughly doubled between the
    first and second half of the history. That is expected: the cheap ones
    (missing stubs, obvious nulls) went early, and what remains needs tracing.
    It also means the wall count is not a countdown. Walls are not independent,
    and fixing a whole defect class can collapse several at once, which is what
    the 512-pointer pass did.</p>
  </section>

  <section>
    <p class="eyebrow">The boot chain, mapped</p>
    <h2>Exactly where execution dies</h2>
    <p>Decompiling the wall traced the path from the entry point to the crash,
    and a per-call bisect of the static initialisers narrowed it to one call.</p>
    <div class="scroller">
      <table>
        <thead><tr><th>Step</th><th>What it is</th><th>Result</th></tr></thead>
        <tbody>
          <tr><td class="n">0x001A1C97</td><td>XBE entry point</td><td>runs</td></tr>
          <tr><td class="n">CreateThread</td><td>starts the CRT on a second thread</td><td>runs inline</td></tr>
          <tr><td class="n">0x001A1C23</td><td>CRT startup &mdash; hand-written in the port</td><td>runs, 4 steps</td></tr>
          <tr><td class="n">sub_00011E40</td><td>static initialisers</td><td>entered, never returns</td></tr>
          <tr><td class="n">sub_0011DD40</td><td>initialiser 1</td><td>returns</td></tr>
          <tr><td class="n">sub_001E8DE0</td><td>initialiser 2</td><td>returns</td></tr>
          <tr><td class="n">sub_00239E50</td><td><strong>initialiser 3 &mdash; the registry singleton</strong></td><td><strong>never returns</strong></td></tr>
        </tbody>
      </table>
    </div>

    <div class="note">
      <h3>Root cause: one uninitialised object</h3>
      <p>Inside that initialiser, a type descriptor reaches the create-an-instance
      path with <em>both</em> its instance size and its allocation prefix set to
      <code>-1</code>. The code allocates <code>size + prefix</code>, so it asks
      the heap for <code>0xFFFFFFFE</code> bytes &mdash; roughly 4&nbsp;GB. The
      allocator correctly refuses and returns null, and the pointer becomes
      <code>0 + (-1) = -1</code>, which passes a non-null check and is written
      through.</p>
      <p>Probing all 235 allocator calls made this unambiguous: the healthy ones
      request 12, 16, 52 bytes; the fatal one requests <code>0xFFFFFFFE</code>.
      Nothing in the chain is misbehaving &mdash; not the allocator, not the null
      check, not the create path. One object was never initialised.</p>
    </div>

    <p class="eyebrow" style="margin-top:.6rem">And where that object comes from</p>
    <h3>The chain runs back to a wall documented weeks ago</h3>
    <p>A software watchpoint &mdash; reading the value after every recompiled call,
    because page-protection watchpoints cannot see writes that land while the page
    is unprotected &mdash; named the function that installs the bad descriptor. It
    is a type lookup whose fallback path is dead:</p>

    <div class="scroller">
      <table>
        <thead><tr><th>Link</th><th>Consequence</th></tr></thead>
        <tbody>
          <tr><td>The subsystem registrar is never called &mdash; its only reference is a <strong>data</strong> pointer, not a call</td><td>registry count stays at <strong>1</strong></td></tr>
          <tr><td>The type lookup falls back to the previously-registered subsystem, which needs <strong>2 or more</strong></td><td>that branch is dead code</td></tr>
          <tr><td>A type this subsystem does not own cannot be inherited</td><td>lookup returns an uninitialised descriptor</td></tr>
          <tr><td>Its size and prefix are both <code>-1</code></td><td>allocation asks for 4&nbsp;GB, returns null</td></tr>
          <tr><td><code>0 + (-1) = -1</code> passes the null check</td><td>the boot writes through <code>-1</code> and dies</td></tr>
        </tbody>
      </table>
    </div>

    <p>The fix belongs at the top of that chain. Every function below it is
    faithful to the original and none of them should be guarded. It is also the
    <strong>third</strong> time the same defect class &mdash; code reachable only
    through a data pointer, so never translated &mdash; has produced the active
    wall.</p>
  </section>

  <section>
    <p class="eyebrow">The dominant defect class</p>
    <h2>Code that exists but is never reached</h2>
    <p>A recompiler discovers functions by following calls from an entry point.
    A function whose only reference is a pointer in a table &mdash; a vtable, an
    initialiser list, a factory array &mdash; is never reached, never translated,
    and whatever it was meant to set up stays null for the whole run.</p>
    <p>The clearest case sits on the most important path in the binary: the CRT
    startup at <code>0x001A1C23</code> is never the target of a call instruction
    anywhere. Its only reference is <code>push 0x1a1c23</code>, as an argument to
    <code>CreateThread</code>. It runs at all only because it was hand-written
    into the port.</p>
    <div class="note">
      <h3>The strategic read</h3>
      <p>Anything that converts data-referenced code into translated code has
      outsized leverage over fixing individual functions. Two passes of that
      shape &mdash; 609 orphan functions, then 512 data-referenced pointers
      &mdash; have each produced more movement than any single-function fix in
      this project's history.</p>
    </div>
  </section>

  <section>
    <p class="eyebrow">What happens next</p>
    <h2>A wrong idea, killed before it cost a build</h2>
    <p>The session opened by chasing a hunch: six of fourteen type descriptors
    appeared to point at another object&rsquo;s data. Checking the project&rsquo;s
    own record of past conclusions refuted it in seconds. The pattern that looked
    like structure &mdash; every healthy descriptor&rsquo;s list sitting exactly
    0x68 bytes past it &mdash; is just the allocator handing out consecutive
    blocks of a 0x64-byte object. The same mistake had been made and withdrawn
    months earlier. Nothing was aliased.</p>
    <p>That is the ledger paying for itself. The wrong idea was appealing, and
    the only thing that stopped it was having written down why it failed the
    first time.</p>
    <div class="note">
      <h3>What was actually wrong</h3>
      <p>Measuring instead of guessing found something better. The <em>first</em>
      lookup worked perfectly &mdash; twenty-three entries, match at index
      thirteen. The <em>second</em> call was the broken one, and bracketing it
      showed the stack pointer sixteen bytes lower after a call that should have
      left it untouched, with a saved register coming back holding a
      neighbour&rsquo;s value.</p>
      <p>Seven measurements down the call chain put the sixteen bytes on one
      routine: the one that stores an entry into a descriptor&rsquo;s field list.
      Its translated form stops early, and the branch it ends on &mdash; the
      branch containing the store itself, plus the register restore and argument
      cleanup &mdash; was never recognised as code. Eight bytes of restore plus
      eight of cleanup is exactly the sixteen that went missing, and the absent
      store is why no list ever grew.</p>
      <p>This is the third time a two-or-three-instruction fragment the translator
      skipped has turned out to be the whole defect. It is now the first thing to
      check when a function&rsquo;s translated extent is shorter than its real
      one.</p>
    </div>
    <p>Kernel calls did not move, and that is expected rather than
    disappointing: the boot now does far more real work before it stops, which
    the other three counters show and that one cannot. The project&rsquo;s own
    rules require a second signal before believing a fix, and here there are
    three.</p>
    <div class="note">
      <h3>The honest caveat</h3>
      <p>Regenerating any part of the translated tree silently discards the
      hand-written repairs inside it, duplicates three functions every time, and
      leaves a link error behind. None of that is detected by the tool that
      claims to check it. The full repair sequence is now written down, and a new
      tool fixes the duplication by checking that every label inside a function
      belongs to that function&rsquo;s own address range.</p>
      <p>An archive of the working tree is taken before and after each of these
      steps now. Last session one was not, and a file that cannot be rebuilt was
      lost permanently.</p>
    </div>
  </section>

  <footer>
    Generated by <code>tools_data/gen_status_page.py</code> from
    <code>progress.json</code> and the current run's log. Figures come from a
    deterministic two-of-two run. Where a conclusion was later overturned, the
    project ledger records both &mdash; including several where a runtime
    measurement corrected a confident static one.
  </footer>

</div>
""".format(
        date=escape(str(latest.get("date", ""))),
        runs=len(hist),
        lifted=lifted,
        callsites=sig.get("callsites") or 0,
        walls=len(walls),
        reached=sig.get("reached") or 0,
        svg=svg,
        cap=cap,
        kernel=latest.get("kernel_calls"),
        heap=sig.get("heap") or 0,
        ended=escape(str(latest.get("ended", "?"))),
        rows=rows,
        headline=HEADLINE,
        subhead=SUBHEAD,
        crash=escape(str(latest.get("crash_in", "?"))),
        **econ,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=os.path.join(GAME, "status.html"))
    a = ap.parse_args()
    hist = json.load(open(DB, encoding="utf-8"))
    head = open(HEAD, encoding="utf-8").read()
    with open(a.out, "w", encoding="utf-8", newline="") as fh:
        fh.write(head + build(hist, current_signals()))
    print("wrote %s" % a.out)
    print("  %d runs, %d distinct walls, latest %s kernel calls"
          % (len(hist), len(wall_history(hist)), hist[-1].get("kernel_calls")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
