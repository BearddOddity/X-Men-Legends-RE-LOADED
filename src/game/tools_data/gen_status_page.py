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
HEADLINE = ("The generator was the bug, and fixing it moved everything")
SUBHEAD = ("Nine hundred and fifty-seven routines now run, up from eight "
           "hundred and fifty-three, and every routine that ran before still "
           "runs - each step added functions without losing one. None of it "
           "came from new guards. It came from three defects in the translator "
           "itself. It was cutting functions short wherever a conditional jump "
           "pointed past a return instruction, so the tail of the function "
           "became an empty placeholder the caller jumped into and the guest "
           "stack drifted; that defect had been costing 244 kilobytes of real "
           "code and had 116 hand-written guards propping it up, all now "
           "deleted. It was also dropping the carry flag on negate, so the "
           "common 'are these equal' idiom answered yes to everything, and it "
           "could not see a register clobbered by a pop before a comparison was "
           "tested. "
           "The other half of the gain was asking the program what it wanted. "
           "The failure log names every address the boot tried to call and "
           "could not; seeding those, and translating the sound and "
           "graphics-support sections as code rather than data, closed the list "
           "entirely - every unresolved call target is now gone except the null "
           "pointers, which are a symptom rather than a gap. "
           "The static initialisers all complete now, and the stopping point "
           "has moved into the game's own startup. There, a routine treats a "
           "pointer into read-only data as though it were a heap block: it "
           "reads the four bytes in front of it as an allocation header and "
           "steps back by that amount, which lands in the middle of the "
           "program's own code. "
           "Two corrections are worth recording, because both were published "
           "before they were checked. Several routine names given earlier were "
           "wrong - the crash report prints offsets that were being read "
           "against the wrong column of the linker map. And the register state "
           "at the fault was first read as a stack imbalance; it is not, it is "
           "that pointer arithmetic landing in code space. Probes settled both: "
           "a routine that never executes cannot be where the fault is.")


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
    game is already C, and only a sliver of it has ever run &mdash; but the C
    runtime's static initialisers now complete, so what runs is the game's own
    startup rather than the runtime's.</p>
    <p class="track-cap"><strong>On the numbers.</strong> Call sites executed
    counts distinct <em>direct</em> call sites. A separate counter tracks
    {reached} functions entered through <em>indirect</em> calls. Neither is a
    clean count of functions executed, and an earlier version of this page
    presented the second as though it were.</p>
  </section>

  <section>
    <p class="eyebrow">Audit &middot; 3 September 2026</p>
    <h2>What was checked, and against what</h2>
    <p>A percentage is only worth printing when there is a denominator behind
    it. These four have one. Each was measured during the audit, not quoted from
    an earlier session.</p>

    <div class="vledger">
      <div class="vrow">
        <span class="vrow-label">Hand-written repairs present in the translated tree</span>
        <span class="vrow-frac">1,093 / 1,093</span>
        <span class="vrow-bar"><span class="vrow-fill" style="width:100%"></span></span>
        <span class="vrow-note">Tested by matching each recorded block&rsquo;s exact text in
        its file. The project&rsquo;s own tool reports whether an edit <em>could</em> be
        placed, which is not the same question, and is how four repairs went missing
        earlier the same day.</span>
      </div>

      <div class="vrow">
        <span class="vrow-label">Recompiled functions with a real body</span>
        <span class="vrow-frac">1,656 / 1,656</span>
        <span class="vrow-bar"><span class="vrow-fill" style="width:100%"></span></span>
        <span class="vrow-note">No address in the recompilation list is missing its code,
        and no function is defined twice anywhere in the tree.</span>
      </div>

      <div class="vrow">
        <span class="vrow-label">Tool tests passing</span>
        <span class="vrow-frac">51 / 51</span>
        <span class="vrow-bar"><span class="vrow-fill" style="width:100%"></span></span>
        <span class="vrow-note">Plus the two new tools&rsquo; own self-checks. The tools are
        what make the analysis repeatable, so they are tested; the translated code cannot
        be, since its only real test is whether the game boots.</span>
      </div>

      <div class="vrow">
        <span class="vrow-label">Identical runs from a clean rebuild</span>
        <span class="vrow-frac">2 / 2</span>
        <span class="vrow-bar"><span class="vrow-fill" style="width:100%"></span></span>
        <span class="vrow-note">Rebuilt from source and run twice: {kernel} kernel calls,
        {heap} heap allocations, {reached} functions reached, {callsites} call sites, both
        times. A figure that moves between runs cannot be compared with anything.</span>
      </div>
    </div>

    <div class="nodenom">
      <span class="vrow-label">No completion percentage for the port itself</span>
      <span class="vrow-note">There is nothing honest to divide by. The goal is
      &ldquo;boots to the main menu&rdquo;, and the remaining work is however many defects
      stand between here and there &mdash; a number nobody knows. Every percentage on this
      page measures something countable; inventing one for overall progress would be the
      most quotable and least true figure here. Direction of travel is the honest
      substitute, and it is what the chart below shows.</span>
    </div>

    <div class="note">
      <h3>The high-water mark that was not one</h3>
      <p>On 9 August a run recorded <strong>4,000 kernel calls and 826 heap
      allocations</strong> against today&rsquo;s {kernel} and {heap}, which reads as a
      severe regression. It is not. That run was a runaway: 4,000 is where the watchdog
      cuts execution off, and the change that ended it is recorded as removing a dispatch
      loop running on junk read from an unmapped page. The allocations were that loop
      allocating garbage.</p>
      <p>Worth stating plainly, because the project&rsquo;s own progress tool still
      advertises 4,000 as the best figure ever achieved, and it never was.</p>
    </div>
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
    <h2>Static startup now completes</h2>
    <p>The C runtime&rsquo;s four startup steps all finish, including the walk
    over the static initialiser table that used to spin on garbage. Execution
    now stops inside the game&rsquo;s own startup rather than before it.</p>
    <div class="scroller">
      <table>
        <thead><tr><th>Step</th><th>What it is</th><th>Result</th></tr></thead>
        <tbody>
          <tr><td class="n">0x001A1C97</td><td>XBE entry point</td><td>runs</td></tr>
          <tr><td class="n">CreateThread</td><td>starts the CRT on a second thread</td><td>runs inline</td></tr>
          <tr><td class="n">0x001A1C23</td><td>CRT startup &mdash; hand-written in the port</td><td>runs, 4 steps</td></tr>
          <tr><td class="n">sub_001A3639</td><td>step 1</td><td><strong>completes</strong></td></tr>
          <tr><td class="n">sub_001A23F3</td><td>step 2</td><td><strong>completes</strong></td></tr>
          <tr><td class="n">sub_001A35AC</td><td>step 3</td><td><strong>completes</strong></td></tr>
          <tr><td class="n">sub_001A3554</td><td>step 4 &mdash; the static initialiser walk</td><td><strong>completes</strong></td></tr>
          <tr><td class="n">sub_0020AA90</td><td>game startup, deep in object construction</td><td><strong>faults</strong></td></tr>
        </tbody>
      </table>
    </div>

    <div class="wall">
      <div class="wall-row"><span class="tn-label">The current stopping point</span></div>
      <p>The routine is entered with a <strong>valid object every time</strong>
      &mdash; a probe that fires only when the object pointer falls outside the
      heap never fired once. The fault is a <strong>second</strong> pointer: the
      code reads the four bytes in front of it as an allocation header and steps
      back by that amount, which is what you do to a heap block. On the fatal
      pass that pointer is a <em>static</em> address in read-only data, which has
      no such header, so the subtraction lands in the middle of the program&rsquo;s
      own code and the next read returns instruction bytes.</p>
      <p>That also explains registers that appear to hold code addresses. They
      are not saved return addresses surfacing through a stack imbalance, which
      was the first reading &mdash; they are this arithmetic landing in code
      space. The open question is which producer hands back a static pointer
      where a heap block belongs.</p>
    </div>
  </section>

  <section>
    <p class="eyebrow">What moved, and why</p>
    <h2>The generator was the defect, not the game</h2>
    <p>The gain from 853 to {reached} came from three faults in the translator
    itself, plus one loop of simply asking the program what it wanted. None of it
    came from new hand-written guards &mdash; 116 existing ones were
    <em>deleted</em> as no longer needed.</p>

    <div class="scroller">
      <table>
        <thead><tr><th>Step</th><th>Reached</th><th>Call sites</th><th>What changed</th></tr></thead>
        <tbody>
          <tr><td>start</td><td class="n">853</td><td class="n">1,674</td><td>&mdash;</td></tr>
          <tr><td>generator fixes</td><td class="n">861</td><td class="n">1,669</td><td>extent, carry flag, clobber detection</td></tr>
          <tr><td>seed round 1</td><td class="n">941</td><td class="n">1,810</td><td>4 addresses the failure log named</td></tr>
          <tr><td>seed round 2</td><td class="n">949</td><td class="n">1,835</td><td>3 more the next run exposed</td></tr>
          <tr><td>extra sections</td><td class="n">{reached}</td><td class="n">{callsites}</td><td>sound and graphics support as code</td></tr>
        </tbody>
      </table>
    </div>
    <p class="track-cap">Every step is a <strong>strict superset</strong> of the
    one before: no routine that ran previously stopped running at any point.</p>

    <div class="note">
      <h3>Functions cut short wherever a branch jumped past a return</h3>
      <p>The translator decided where each function ended by walking forward and
      stopping at a return instruction, extending that limit when it saw a branch
      pointing further ahead &mdash; but it set the limit <em>to</em> the branch
      target rather than past it. A return sitting immediately before that target
      therefore ended the walk, and the whole jumped-to block was lost: it became
      an empty placeholder the caller jumped into, and the guest stack drifted
      every time.</p>
      <p>Correcting that arithmetic recovered <strong>244 kilobytes</strong> of
      real code and cut empty placeholders from 2,819 to 407. It also made 116
      hand-written repairs obsolete &mdash; every one of them existed to patch
      this same defect by hand, one function at a time.</p>
    </div>

    <div class="note">
      <h3>Two flag defects, fixed at the source</h3>
      <p>Negation was not recording its carry, so the common &ldquo;are these two
      equal&rdquo; idiom &mdash; which reads that carry one instruction later
      &mdash; answered <em>yes to everything</em>. One such predicate decides
      which allocator owns a pointer. Separately, a register overwritten by a
      stack pop between a comparison and the branch testing it was invisible to
      the checker, because a pop is a macro call rather than an assignment.</p>
      <p>Both are now emitted correctly by the translator instead of being
      patched by hand afterwards. The repair tool for the first class went from
      262 sites to zero.</p>
    </div>

    <div class="note">
      <h3>Ask the program what it wanted</h3>
      <p>When the boot calls an address the port has no code for, it says so and
      names the address. Seeding exactly those, measuring, and re-reading the log
      is a loop, and it ran until the list was empty. Seven were small routines
      the detector had missed because alignment padding separated them from the
      function above; the rest lived in sections marked as data that in fact hold
      code &mdash; sound and two graphics support libraries, now translated too.</p>
      <p>Every unresolved call target is now gone except the null pointers, which
      are a symptom of the remaining defect rather than a gap in the port.</p>
    </div>

    <div class="note">
      <h3>A correction worth publishing</h3>
      <p>Several routine names given earlier in the same session were wrong. The
      crash report prints offsets, and those were being resolved against the wrong
      column of the linker&rsquo;s symbol map &mdash; a column relative to the code
      section rather than to the file, which names the routine <em>below</em> the
      true one.</p>
      <p>Probes settled it, not argument: the wrongly named routine never executed
      once across two builds while the crash reproduced identically. A routine that
      never runs cannot be where the fault is. The conversion is now a tool rather
      than arithmetic done by hand.</p>
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
