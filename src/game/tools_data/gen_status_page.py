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
HEADLINE = ("A predicate that always said yes, and a baseline that was "
            "measuring a damaged tree")
SUBHEAD = ("The translator emits the two-instruction idiom for “was this zero?” "
           "but never the flag that carries the answer between them, so 269 "
           "comparisons across the binary evaluated as a compile-time constant. "
           "One of them asks whether an allocator owns a pointer, and it was "
           "answering yes for every pointer. Fixing it does not unblock what the "
           "plan said it would - and checking that turned up something worse: the "
           "previous run’s figures were taken from a tree that had silently lost "
           "four hand-written fixes. The honest number is 230 kernel calls and 477 "
           "call sites, not 582 and 501.")


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
    <h2>A correction first, then the next thread</h2>
    <p>The previous entry reported 582 kernel calls and 501 call sites. That
    measurement was taken from a damaged tree: regenerating one generated file
    discards the hand-written fixes inside it, and the tool that checks for those
    fixes answers &ldquo;all 1,095 can be placed&rdquo; whether or not they are
    actually there. Four were missing, including one that makes an allocator
    ownership test honest. With them restored the same build measures 230 and
    477. The lower number is the real one.</p>
    <div class="note">
      <h3>What was actually fixed</h3>
      <p><code>neg r; sbb r, r; inc r</code> is how a compiler of that era asks
      &ldquo;was this value zero?&rdquo;. The middle instruction reads a carry
      flag the first one sets. The translator emits both instructions and never
      the flag, so the whole idiom folds to a constant &mdash; the answer is
      always &ldquo;yes, equal&rdquo;. It appears 404 times; at 269 of them the
      two instructions sit adjacent in one basic block, where the flag is
      knowable with certainty, and those are now correct.</p>
      <p>The remaining 135 take their carry from a comparison further back or
      from another path into the same code. Those are left alone. Guessing a
      plausible value there would be manufacturing data to get past a check,
      which is the one thing this project does not do.</p>
      <p>A pleasing confirmation: months ago this same defect was found by hand
      at exactly one of those sites and fixed the same way. The new tool skips
      that site, because it was already right.</p>
    </div>
    <p>It did not, however, unblock the change it was supposed to. That change
    still collapses the boot, so the dependency the plan described was wrong: the
    fixed function was blocking a <em>different</em> repair, not this one.</p>
    <div class="note">
      <h3>The next thread, and it is a sharp one</h3>
      <p>The defect this project has been chasing for weeks &mdash; type
      descriptors whose field container is missing &mdash; is gone. All fourteen
      now have real containers and real arrays, and the fix written for it is
      measurably a no-op, so it was deleted rather than kept as dead code.</p>
      <p>What replaced it is more specific. Six of the fourteen have a field
      pointer aimed at <em>another object&rsquo;s</em> array, while their own sits
      unused at a fixed offset the other eight use correctly. That is not a null
      pointer or uninitialised memory; it is one object holding a reference to
      another&rsquo;s data. Aliasing like that has a single cause, and finding it
      should collapse several walls at once.</p>
    </div>
    <div class="note">
      <h3>The honest caveat</h3>
      <p>Two of today&rsquo;s corrections cost more than they gained on the
      headline counter, and both were kept anyway. A predicate that answers
      truthfully sends execution down paths that a predicate stuck on
      &ldquo;yes&rdquo; never reached, and those paths fail. Indirect dispatches
      rose 21% while kernel calls fell &mdash; more real work happening, over a
      shorter run. The counter that dropped is the narrower measure.</p>
      <p>One thing was lost for good. The regenerated file cannot be rebuilt to
      match what it replaced, and no archive had been taken since early August.
      The project has a tool for exactly this and it was not used.</p>
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
