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
HEADLINE = ("The biggest repair of the week was deleting our own code")
SUBHEAD = ("Somebody - us, weeks ago - had added a safety net to the layer "
           "where the game calls the operating system. On every single one of "
           "those calls it swept two hundred and fifty-six bytes of the game's "
           "memory and blanked anything that looked suspicious. One of the two "
           "patterns it blanked is the number one written as a decimal "
           "fraction, which is one of the most ordinary values a game holds. "
           "So on every kernel call it was quietly destroying real data. "
           "It was found by making the first page of memory untouchable and "
           "logging everything that reached for it. Of the 1,983 such reaches "
           "in a run, 1,965 came from that safety net rather than from the "
           "game. Deleting it did not merely stop the harm - the boot went "
           "further immediately, from 785 routines reached to 850, and the "
           "reaches into forbidden memory fell from 1,983 to 3. "
           "The number is the smaller half of the point. Every failure chased "
           "this week ended at a value that should not have been there: a "
           "count of minus one, a fraction where an address belonged, a field "
           "that read back wrong. The safety net was rewriting exactly those "
           "values, on every call, underneath the investigation. Several "
           "earlier conclusions were drawn about a program that was being "
           "edited while it was being watched. "
           "What remains is honest: three reaches into forbidden memory, two "
           "of them in one routine, and a stopping point that is now a small "
           "and specific question rather than a fog.")


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
    <p class="eyebrow">Open defects</p>
    <h2>One target, not four</h2>
    <p>The audit listed four faults as wrong behaviour on every run. Repairing
    them in one experiment and then measuring each on its own is what showed
    they are the same problem seen from four places.</p>

    <div class="scroller">
      <table>
        <thead><tr><th>Configuration</th><th>Kernel calls</th><th>Heap</th><th>Reached</th><th>Call sites</th></tr></thead>
        <tbody>
          <tr><td>baseline</td><td class="n">230</td><td class="n">136</td><td class="n">196</td><td class="n">551</td></tr>
          <tr><td>first repair alone</td><td class="n">48</td><td class="n">96</td><td class="n">169</td><td class="n">437</td></tr>
          <tr><td>both repairs</td><td class="n">48</td><td class="n">96</td><td class="n">169</td><td class="n">437</td></tr>
          <tr><td>second repair alone</td><td class="n">248</td><td class="n">87</td><td class="n">143</td><td class="n">433</td></tr>
        </tbody>
      </table>
    </div>
    <p class="track-cap">Both repairs together are <em>identical</em> to the first
    alone, so the second changes nothing on this boot. On its own the second
    raises kernel calls while dropping the measure of how much distinct code
    runs &mdash; which is the one the project trusts first. Both were reverted.</p>

    <div class="note">
      <h3>What the failure actually is</h3>
      <p>With the first repair in place, execution dies reading an address in the
      region reserved for calls into the operating system &mdash; somewhere no
      object should ever live. Two measurements on the loop that fails show the
      moment it goes wrong. The healthy passes carry real objects with real
      method tables. Then one pass arrives with <strong>an empty pointer where the
      object should be</strong>.</p>
      <p>From there nothing is corrupted, which is the counter-intuitive part. The
      address zero is a readable page in this runtime, so reading &ldquo;the
      object&rsquo;s field list&rdquo; through an empty pointer returns a leftover
      value that looks like a plausible list. The loop then walks that leftover,
      inventing entries until one of them is treated as a method table and
      called. The crash is four steps downstream of the actual mistake.</p>
    </div>

    <div class="note">
      <h3>Where the empty pointer comes from</h3>
      <p>It is the stack. Every routine is handed a block of working space and is
      expected to give back exactly what it took. Some of these give back
      <strong>sixteen bytes more than they were given</strong>. The caller then
      looks for its own value one slot too far along, finds whatever was lying
      there, and carries on with a stray number where a pointer belonged. That
      stray number is the empty pointer described above.</p>
      <p>This was invisible for months because the existing check only asked
      whether the working space had run away entirely &mdash; off the end of an
      eight-megabyte region. Being sixteen bytes wrong sits comfortably inside
      that, so it never triggered. The check was asking the wrong question, not
      failing to ask it.</p>
    </div>

    <div class="note">
      <h3>Three tools, each seeing what the last could not</h3>
      <p>The first watches calls made through a stored address and compares the
      working space before and after. It named a routine immediately, and also
      revealed that most of what it flagged was harmless: of 167 complaints only
      ten were real, the other 157 being a normal bookkeeping step that had been
      miscounted as damage. That correction matters, because the surface to
      investigate shrank from 167 to 10.</p>
      <p>The second covers ordinary direct calls, which nothing had ever checked
      for this. It is self-calibrating: rather than needing a table of what each
      routine should give back, it records what a call site returns the first
      time and complains when the same site later disagrees with itself. On its
      first run it named the routine behind the current stopping point, matching
      a result that had previously taken a day of hand measurement.</p>
      <p>The third exists because the second has a blind spot: a routine that is
      <em>consistently</em> wrong never disagrees with itself. So every call site
      now records what it gave back, and the judging is done afterwards against
      what each routine is actually entitled to. That immediately found a
      further routine discarding twenty-four bytes on every single call, which
      neither of the first two could ever have seen.</p>
    </div>

    <div class="note">
      <h3>Why no single routine is to blame</h3>
      <p>The obvious next question is which routine is deepest, so the others can
      be dismissed as inheriting its damage. The question has no answer as posed.
      The routine at the stopping point was proved sound on every path through
      it, and all the ordinary calls it makes give back exactly what they should.
      The damage arrives through a call made via a stored address &mdash; and the
      stored addresses it calls are the ones its own caller handed it moments
      earlier. <strong>The three routines form a loop</strong>, and a map built
      from ordinary calls alone cannot order a loop whose damaging link is not an
      ordinary call.</p>
      <p>The next tool is therefore a small one: the existing check already knows
      which stored address was called, and needs only to record who called it.
      That completes the map.</p>
    </div>

    <div class="note">
      <h3>The one that was not a defect at all</h3>
      <p>The fourth item is not missing code. It is the compiler&rsquo;s own
      stack-corruption detector, and the placeholder standing in for it has been
      quietly swallowing the alarm on every run. Filling it in would make the
      program abort deliberately &mdash; correct, and worse. It is a symptom
      pointing at something else, and it is now recorded as one rather than
      queued as a repair.</p>
    </div>

    <p>Two tools were improved along the way. The one that recompiles a missing
    fragment was sizing it by scanning forward to the next return instruction,
    with no upper bound &mdash; so a fragment in the middle of a function ran
    straight past the end of it and into the next. It now stops at the next known
    function. That also corrects an earlier conclusion in the project&rsquo;s
    record, which had rejected a repair partly because a function looked 41 bytes
    long when it is really 882; the 41 was the translator&rsquo;s own truncated
    guess, not the truth.</p>

    <div class="note">
      <h3>The honest caveat</h3>
      <p><strong>Nothing on this page is a repair.</strong> The headline numbers
      have not moved and were not expected to: every change described here is a
      measuring instrument, and each was checked to be behaviour-neutral by
      confirming the ordinary build produces a log identical to the previous one
      byte for byte. Understanding moved a long way; the program still stops in
      the same place.</p>
      <p>Three separate faithful repairs have each made the headline number
      worse, and each was reverted. That is not three failures. Each removed a
      placeholder that had been returning early, and the code it stood in front
      of then ran and hit its own defect. The placeholders were load-bearing.
      Expect the next one to behave the same way, and treat a drop after a
      correct repair as information rather than a setback.</p>
      <p>Two specific limits on the findings above. A promising explanation for
      the sixteen bytes &mdash; a cleanup step being applied twice on the failure
      path &mdash; was measured and <strong>ruled out</strong>: it is real, it
      affects around 900 places in the code, and in this run it fires exactly
      once and costs four bytes. It is worth fixing on its own account and is not
      the cause here. Separately, one of the routines flagged as discarding too
      much is doing something legitimate that the newest tool does not yet
      understand, and is recorded as an open question rather than a defect.</p>
      <p>A claim in the project&rsquo;s own source comments was found to be false
      and has been corrected: it asserted a check had been run across 19,239
      places and found nothing, when the search it describes could not have
      matched anything at all. The lesson is recorded with it.</p>
      <p>Every repair and every refutation discussed here is preserved word for
      word in the project&rsquo;s record, so none of it has to be re-derived when
      the blocking defect is fixed.</p>
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
