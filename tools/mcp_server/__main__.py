#!/usr/bin/env python3
"""MCP server for the Xbox static-recompilation engine.

Why this exists
---------------
Ghidra became genuinely useful to an assistant the moment ReVa exposed it over
MCP - instead of guessing about the binary, you ask it. The recomp engine had
no equivalent, so every interaction went through shell commands and regexes
over stderr.txt. That cost real mistakes: a C string literal built through a
shell heredoc arrived with a mangled escape and broke the build (the exact trap
CLAUDE.md documents), and a probe limit silently truncated a log, hiding the
allocation failure being hunted.

This exposes the engine's own tools directly, so none of that is in the path.

Every tool returns structured data. Nothing here reimplements analysis - it
wraps the scripts in src/game/tools_data/ that already work, so there is one
source of truth for what a number means.

Run:
    py -3.12 tools/mcp_server/__main__.py

Register (absolute path on purpose - an MCP server inherits the client's
working directory, so the -m form would only work when launched from the repo
root; this module locates the repo from __file__):
    claude mcp add --scope user XboxRecomp -- py -3.12 "<repo>/tools/mcp_server/__main__.py"
"""
import json
import os
import re
import subprocess
import sys
import time

from mcp.server.fastmcp import FastMCP

_HERE = os.path.dirname(os.path.abspath(__file__))          # tools/mcp_server
REPO = os.path.dirname(os.path.dirname(_HERE))              # repo root
GAME = os.path.join(REPO, "src", "game")
TOOLS = os.path.join(GAME, "tools_data")
GEN = os.path.join(GAME, "src", "recomp", "gen")
STDERR = os.path.join(GAME, "stderr.txt")
AFK_STATE = os.path.join(GAME, ".afk-launch.json")

sys.path.insert(0, TOOLS)
import recomp_lock                                           # noqa: E402

mcp = FastMCP("XboxRecomp")


def _guarded(tool_name, fn, force=False):
    """Run `fn` under the build lock, failing fast rather than blocking.

    build_compile.bat, smoke_spread.py, add_probe.py, strip_probes.py and
    progress.py --record all touch gen/*.c or stderr.txt with no locking of
    their own - only walls.py/overnight.py/investigate.py/bisect_core.py take
    the lock, and each of those holds it for its ENTIRE hours-long run. An
    interactive MCP call has no business waiting hours behind one, so this
    fails immediately with a clear "busy" error instead of either blocking or
    (worse) racing and silently corrupting stderr.txt or an AFK run's
    snapshot.

    `force` forwards to recomp_lock, which applies it to the ACTIVITY
    HEURISTIC ONLY - a genuinely held lockfile still refuses, so this can
    never stomp a running AFK tool. That distinction is what makes exposing
    it safe.

    Why it needed exposing: the heuristic treats "stderr.txt or
    seed_list.json changed in the last 300s" as busy, and an interactive
    build-then-run-then-edit-then-rebuild loop trips it constantly on its own
    exhaust - three times in one sitting here, each time with the lock
    provably free and no process alive. Without an override the only options
    were to idle for five minutes or shell out around the tool, and shelling
    out is how the stale-binary confusion happened.
    """
    try:
        with recomp_lock.build_lock(tool_name, force=force):
            return fn()
    except SystemExit as exc:
        msg = str(exc)
        out = {"error": msg, "busy": True}
        if not force and "nothing holds the lock" in msg:
            out["hint"] = ("Nothing holds the lock - this is the activity "
                           "heuristic firing on recent tree writes, possibly "
                           "your own. Check afk_status(), and if no AFK run is "
                           "alive, retry with force=True.")
        return out


def _py(script, *args, timeout=900, env_extra=None):
    """Run one of the project's own tools and return its output."""
    cmd = [sys.executable, os.path.join(TOOLS, script), *map(str, args)]
    env = None
    if env_extra:
        env = dict(os.environ)
        env.update(env_extra)
    # stdin=DEVNULL matters: this server speaks MCP over stdin/stdout, and a
    # child that inherits those handles can block on the transport pipe or
    # corrupt the protocol stream. Seen for real - triage() hung for the full
    # 900s timeout while the same script ran instantly from a shell.
    p = subprocess.run(cmd, cwd=GAME, capture_output=True, text=True,
                       timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    return {"ok": p.returncode == 0, "stdout": p.stdout, "stderr": p.stderr}


def _pid_alive(pid):
    if sys.platform == "win32":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _launch_background(script, *args):
    """Start one of the hours-long unattended tools and return without waiting.

    These block on a build lock and run for hours - a synchronous MCP call
    would either time out or tie up the session for the whole run. Output goes
    to a log file next to the script; progress is read back through that
    tool's own *_report() once it finishes, never by waiting on this call.
    """
    log_path = os.path.join(GAME, script[:-3] + "_launch.log")
    cmd = [sys.executable, os.path.join(TOOLS, script), *map(str, args)]
    logf = open(log_path, "w", encoding="utf-8")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    p = subprocess.Popen(cmd, cwd=GAME, stdout=logf, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, **kwargs)
    state = {"script": script, "args": [str(a) for a in args], "pid": p.pid,
             "started": time.time(), "log": os.path.basename(log_path)}
    with open(AFK_STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    return {"started": True, "pid": p.pid, "log": state["log"]}


def _read_report(name):
    path = os.path.join(GAME, name)
    if not os.path.exists(path):
        return {"found": False}
    return {"found": True, "mtime": os.path.getmtime(path),
            "text": open(path, encoding="utf-8", errors="replace").read()}


@mcp.tool()
def build(force: bool = False) -> dict:
    """Compile the recompiled executable. Returns errors only, not the full log.

    Fails fast with `busy: true` if an AFK tool (afk_start) currently holds
    the build lock, rather than racing it.

    `force=True` bypasses only the ACTIVITY HEURISTIC ("the tree was touched
    in the last 300s"), which an interactive edit/build/run loop trips on its
    own exhaust. A genuinely held lockfile still refuses, so this cannot
    stomp a running AFK tool. Check `afk_status()` first; if the lock is free
    and no launch is alive, forcing is correct.
    """
    def _do():
        bat = os.path.join(GAME, "build_compile.bat")
        p = subprocess.run(["cmd", "/c", bat], cwd=GAME, capture_output=True,
                           text=True, timeout=1800, stdin=subprocess.DEVNULL)
        out = p.stdout + p.stderr
        errors = [l for l in out.splitlines()
                  if "error C" in l or "error LNK" in l or l.startswith("FAILED")]
        linked = "Linking C executable" in out
        return {"linked": linked, "errors": errors[:20],
                "error_count": len(errors)}
    return _guarded("mcp_build", _do, force)


@mcp.tool()
def run(times: int = 2, watch: str = "", force: bool = False) -> dict:
    """Run the game N times and report the tracked signals plus their spread.

    Determinism matters here: a build whose numbers move between runs cannot be
    compared against anything. `varies` names any signal that did.

    `watch` arms memory write-watches for the run - no rebuild needed. Format is
    `VA[:SIZE[:LABEL]]`, comma separated, e.g. `0:4:seh_head,0x5DD0E8:4:heap`.
    Each fires once, names the writing instruction, then disarms so the run
    continues. Use it for "what wrote here?", which grep cannot answer when the
    write goes through a register - that is how the ordinal-47 corruption was
    found after static search came up empty. Results appear in the log as
    `[WATCH:label]`; page granularity means a watch also catches writes to
    other addresses on the same 4 KB page and reports which.

    Fails fast with `busy: true` if an AFK tool currently holds the build
    lock - a run here would overwrite the stderr.txt an AFK tool is mid-read
    of, corrupting both measurements. `force=True` bypasses only the activity
    heuristic, never a real lock holder - see `build`.
    """
    def _do():
        r = _py("smoke_spread.py", times,
                env_extra={"RECOMP_WATCH": watch} if watch else None)
        text = r["stdout"]
        signals = {}
        for m in re.finditer(r"^\s{2}(\w+)\s+min=(\d+)\s+max=(\d+)", text, re.M):
            signals[m.group(1)] = {"min": int(m.group(2)), "max": int(m.group(3))}
        crash = re.search(r"^\s+(\d+)/(\d+)\s+(.*)$", text, re.M)
        return {
            "signals": signals,
            "varies": [k for k, v in signals.items() if v["min"] != v["max"]],
            "deterministic": "NON-DETERMINISTIC" not in text,
            "ending": crash.group(3).strip() if crash else None,
            "raw": text,
        }
    return _guarded("mcp_run", _do, force)


@mcp.tool()
def triage() -> dict:
    """Explain how the last run ended - crash site and registers, or hang details."""
    r = _py("triage_crash.py")
    return {"report": r["stdout"] or r["stderr"]}


@mcp.tool()
def progress(record: str = "", note: str = "", tail: int = 10, force: bool = False) -> dict:
    """Read the progress history, or record a verified result.

    Pass `record` to add an entry. Numbers come from the current stderr.txt, so
    build and run first. Recording is refused if probes are still present.

    `tail` limits how many entries print (default 10) - the full history can
    exceed 150K characters after months of entries and once failed this tool
    outright. The trailing best/now summary is still computed over the whole
    history, never just the shown slice. Pass `tail=0` for the complete
    history if you genuinely need it.

    Recording fails fast with `busy: true` if an AFK tool holds the build
    lock - it would otherwise record numbers from whatever gen/stderr.txt an
    unrelated background run happens to have left behind.
    """
    if record:
        args = ["record", "-m", record] + (["--note", note] if note else [])
        return _guarded("mcp_progress_record", lambda: _py("progress.py", *args), force)
    args = ["--tail", tail] if tail else []
    return _py("progress.py", *args)


@mcp.tool()
def function(name_or_addr: str, at_label: str = "", context: int = 0,
             labels_only: bool = False) -> dict:
    """Return the lifted C for a function, and where it lives.

    Accepts `sub_001A3554` or `0x001A3554`. Pair this with ReVa's
    get-decompilation on the same address to diff lifted output against
    Ghidra's ground truth - that comparison is what found the dropped
    fall-through edges.

    WHOLE FUNCTION BY DEFAULT, which is usually right: tracing control flow
    across labels is what makes a lifted function legible, and that needs all
    of it. Two narrower modes exist for the big ones - sub_001FFDA1 is ~250
    lines and sub_00011B35 more, and reading either in full to look at one
    branch is waste:

      `labels_only` - just the label names and their line numbers, i.e. the
          function's shape. Good for picking a probe site or seeing whether a
          label you care about exists at all.
      `at_label` + `context` - the lines around one label only.

    Both report `truncated: True` so a partial read is never mistaken for the
    whole function.
    """
    s = name_or_addr.strip()
    if s.lower().startswith("0x"):
        s = "sub_%08X" % int(s, 16)
    head = "void %s(void)\n" % s
    for fn in sorted(os.listdir(GEN)):
        if not fn.endswith(".c"):
            continue
        path = os.path.join(GEN, fn)
        lines = open(path, encoding="utf-8", errors="replace").readlines()
        try:
            i = lines.index(head)
        except ValueError:
            continue
        for j in range(i + 1, len(lines)):
            if lines[j].rstrip("\n") != "}":
                continue
            body = lines[i:j + 1]
            base = {"found": True, "file": fn, "line": i + 1,
                    "total_lines": len(body)}

            if labels_only:
                labs = [{"label": l.strip()[:-3], "line": i + 1 + n}
                        for n, l in enumerate(body)
                        if re.match(r"^loc_[0-9A-Fa-f]+: ;$", l.strip())]
                return dict(base, truncated=True, labels=labs)

            if at_label:
                want = at_label.strip().rstrip(":; ")
                hit = next((n for n, l in enumerate(body)
                            if l.strip() == "%s: ;" % want), None)
                if hit is None:
                    return dict(base, error="label %s not in %s" % (want, s),
                                hint="call with labels_only=True to list them")
                c = max(context, 1)
                a, b = max(0, hit - c), min(len(body), hit + c + 1)
                return dict(base, truncated=True, label=want,
                            line_from=i + 1 + a, line_to=i + b,
                            source="".join(body[a:b]))

            return dict(base, source="".join(body))
    return {"found": False,
            "hint": "not a lifted function - it may be an unresolved stub "
                    "(recomp_stubs_unresolved.c) or never discovered"}


@mcp.tool()
def search(pattern: str, limit: int = 40) -> dict:
    """Regex-search the generated code. Returns file, line and text.

    Use this instead of shell grep - no quoting or escaping in the path, which
    is how a C string literal got mangled through a heredoc before.
    """
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"error": "bad regex: %s" % e}
    hits, total = [], 0
    for fn in sorted(os.listdir(GEN)):
        if not fn.endswith(".c"):
            continue
        for n, line in enumerate(
                open(os.path.join(GEN, fn), encoding="utf-8",
                     errors="replace"), 1):
            if rx.search(line):
                total += 1
                if len(hits) < limit:
                    hits.append({"file": fn, "line": n, "text": line.rstrip()})
    return {"total": total, "shown": len(hits), "hits": hits}


@mcp.tool()
def probe(file: str, after: str = "", before: str = "", tag: str = "",
          fmt: str = "", args: str = "", limit: int = 20,
          force: bool = False) -> dict:
    """Insert a debug probe via add_probe.py. Tag must be alphanumeric.

    Values are C expressions, e.g. `esi,MEM32(ebp + -16)`. The anchor must
    match exactly one line or nothing is written.

    Fails fast with `busy: true` if an AFK tool holds the build lock -
    walls.py snapshots and restores gen/*.c across its whole run, and would
    silently discard a probe inserted mid-run on its next revert.
    """
    if not (after or before):
        return {"error": "give either `after` or `before` as the anchor"}
    a = ["--after", after] if after else ["--before", before]
    extra = (["--fmt", fmt] if fmt else []) + (["--args", args] if args else [])
    return _guarded("mcp_probe", lambda: _py(
        "add_probe.py", os.path.join("src", "recomp", "gen", file),
        *a, "--tag", tag, *extra, "--limit", limit), force)


def _summarise_strip(r):
    """Collapse strip_probes.py's per-line dump to per-file counts.

    The script echoes every removed line. One interactive session stripped
    233 probe lines and the tool printed all 233 back - the single largest
    tool result of that session, and none of it was read: the caller already
    knows what it inserted and only needs "did it all come out".

    Anything unexpected is still surfaced. Lines that are neither a file
    header nor a removed-probe bullet are passed through verbatim, so a
    brace-balance refusal or an error is never swallowed by the summary.
    """
    if not isinstance(r, dict) or not r.get("ok"):
        return r
    out = r.get("stdout") or ""
    files, other, total = [], [], None
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(\S+\.c): (\d+) probe line\(s\)$", s)
        if m:
            files.append({"file": m.group(1), "lines": int(m.group(2))})
            continue
        m = re.match(r"^removed (\d+) line\(s\)$", s)
        if m:
            total = int(m.group(1))
            continue
        if s.startswith("- "):          # the per-line echo - this is the bulk
            continue
        other.append(s)
    res = {"ok": True, "files": files}
    if total is not None:
        res["removed"] = total
    if other:
        res["notes"] = other           # never hide a refusal or an error
    return res


@mcp.tool()
def strip_probes(force: bool = False, verbose: bool = False) -> dict:
    """Remove all probes. Refuses any removal that would unbalance braces.

    Returns per-file counts, not the text of every removed line - see
    _summarise_strip. Pass `verbose=True` for the raw dump if you actually
    need to see what came out.

    Fails fast with `busy: true` if an AFK tool holds the build lock, for the
    same reason `probe()` does.
    """
    r = _guarded("mcp_strip_probes",
                 lambda: _py("strip_probes.py", "--apply"), force)
    return r if verbose else _summarise_strip(r)


@mcp.tool()
def log(pattern: str, last: int = 40) -> dict:
    """Grep the last run's stderr. Returns matching lines.

    Beware truncation: `add_probe.py --limit N` caps how many times a probe
    prints, and a low cap once hid the very allocation failure being hunted.
    """
    if not os.path.exists(STDERR):
        return {"error": "no stderr.txt - run first"}
    rx = re.compile(pattern)
    hits = [l.rstrip() for l in
            open(STDERR, encoding="utf-8", errors="replace") if rx.search(l)]
    return {"total": len(hits), "lines": hits[-last:]}


@mcp.tool()
def afk_start(tool: str, hours: float = 4.0, plan: str = "confirmed",
              then: str = "", watch: str = "", stages: str = "",
              dry_run: bool = False) -> dict:
    """Launch an unattended AFK investigator in the background. Returns at once.

    `tool`: `walls` (grind the current wall list with proven patterns),
    `overnight` (greedily accumulate a candidate list, `plan` selects it),
    or `investigate` (four-stage where/when/what/how on the current stall).

    These run for hours - this call does not wait for them. Poll
    `afk_status()`, and once it reports the process is no longer alive, read
    the matching report tool: `walls_report()`, `overnight_report()`, or
    `investigation_report()`.

    Project rule #11: this is a PC port, never an emulator. `walls` applies
    bypasses (clamp/skip) to see past a stall for measurement only - every one
    is stamped SCAFFOLDING in the generated code and in `walls_report()`, and
    is not a fix. `overnight` and `investigate` do not have this caveat:
    `overnight` seeds real missing functions, `investigate` never edits
    anything. Do not report a walls.py bypass as "fixed" - report it as "got
    past", and the underlying defect stays open.

    To be notified on completion instead of polling by hand: this call
    returning is NOT completion, so a notification tied to it would fire
    hours early. Set up a watcher instead - e.g. (session-scoped, dies with
    this session; for a durable nightly kickoff use the scheduled-tasks MCP,
    not this):

        CronCreate(cron="*/10 * * * *", prompt=
          "Call afk_status via the XboxRecomp MCP. If launch.alive is false, "
          "read the matching report, PushNotification a one-line summary, "
          "then CronDelete this job. Otherwise do nothing.")

    A durable nightly run (survives session restarts) belongs in
    mcp__scheduled-tasks__create_scheduled_task instead - write its prompt to
    call afk_start, THEN loop polling afk_status() until the process exits,
    THEN read the report and summarize, so the scheduled task's own
    completion (and its notifyOnCompletion) lines up with the real work
    finishing, not with the launch.
    """
    if os.path.exists(AFK_STATE):
        prev = json.load(open(AFK_STATE, encoding="utf-8"))
        if _pid_alive(prev["pid"]):
            return {"error": f"{prev['script']} (pid {prev['pid']}) is still "
                              f"running - it would just queue behind its own "
                              f"internal lock, but this launcher only tracks "
                              f"one PID at a time, so starting another now "
                              f"loses visibility into the first. Wait for it "
                              f"or check afk_status() first.",
                    "busy": True}
    if tool == "walls":
        args = ["--hours", hours] + (["--dry-run"] if dry_run else [])
        return _launch_background("walls.py", *args)
    if tool == "overnight":
        args = ["--plan", plan, "--hours", hours]
        for t in (then.split(",") if then else []):
            args += ["--then", t]
        if dry_run:
            args.append("--dry-run")
        return _launch_background("overnight.py", *args)
    if tool == "investigate":
        args = ["--hours", hours]
        if watch:
            args += ["--watch", watch]
        if stages:
            args += ["--stages", stages]
        if dry_run:
            args.append("--dry-run")
        return _launch_background("investigate.py", *args)
    return {"error": "tool must be one of: walls, overnight, investigate"}


@mcp.tool()
def afk_status() -> dict:
    """Is the last afk_start()-launched process still running, and who else
    holds the build lock. Check this before starting another AFK run - two
    tools building or running the port at once corrupts both measurements.
    """
    lock = _py("recomp_lock.py", "--status")
    launch = None
    if os.path.exists(AFK_STATE):
        launch = json.load(open(AFK_STATE, encoding="utf-8"))
        launch["alive"] = _pid_alive(launch["pid"])
        launch["minutes_running"] = round((time.time() - launch["started"]) / 60, 1)
    return {"launch": launch, "lock": (lock["stdout"] or lock["stderr"]).strip()}


@mcp.tool()
def afk_stop(force: bool = False) -> dict:
    """Stop the last afk_start()-launched process. There was no way to do
    this before except finding the PID by hand and killing it outside the
    session.

    `force=False` (default) tries `taskkill /PID /T` without `/F` first.
    Verified empirically: this ALWAYS FAILS for these tools, because they run
    detached with no window for Windows to send a close message to - `taskkill`
    reports "can only be terminated forcefully" and nothing dies. It is tried
    anyway because it is free and harmless; do not expect it to work. Pass
    `force=True` to actually stop the process.

    IMPORTANT: a forceful kill does NOT run Python's `finally` blocks, so it
    can leave gen/*.c mid-experiment (a SCAFFOLDING bypass applied but not
    reverted) and the lockfile held by a now-dead PID. `recomp_lock` reclaims
    a dead PID's lock automatically on the next check, so the lock itself
    self-heals - but the tree does not. After a forced stop, check
    `walls_report()`/`git status` on gen/, and rebuild from the tool's own
    `.walls-snapshot` (or an explicit `snapshot.py` checkpoint) if in doubt.
    """
    if not os.path.exists(AFK_STATE):
        return {"stopped": False, "reason": "no launch on record"}
    launch = json.load(open(AFK_STATE, encoding="utf-8"))
    pid = launch["pid"]
    if not _pid_alive(pid):
        return {"stopped": False, "reason": f"{launch['script']} (pid {pid}) "
                                             f"is already not running"}
    if sys.platform != "win32":
        os.kill(pid, 9 if force else 15)
        return {"stopped": True, "pid": pid, "forced": force}
    args = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
    p = subprocess.run(args, capture_output=True, text=True)
    return {"stopped": p.returncode == 0, "pid": pid, "forced": force,
            "output": (p.stdout + p.stderr).strip(),
            "note": None if force else
            "graceful request sent - re-check afk_status() in a few seconds; "
            "call afk_stop(force=True) if it is still alive"}


@mcp.tool()
def walls_report() -> dict:
    """Read the latest walls.py report (walls_report.md). Does not run it."""
    return _read_report("walls_report.md")


@mcp.tool()
def overnight_report() -> dict:
    """Read the latest overnight.py report (overnight_report.md). Does not run it."""
    return _read_report("overnight_report.md")


@mcp.tool()
def investigation_report() -> dict:
    """Read the latest investigate.py report (investigation_report.md). Does not run it."""
    return _read_report("investigation_report.md")


@mcp.tool()
def faithful(target: str = "", sweep: int = -1) -> dict:
    """Compare generated C against the original x86 - the only check on this
    project that has never given a wrong answer. Pass `target`
    (`sub_XXXXXXXX` or `0xADDR`) to check one function, or `sweep` (0 for
    every function, or a count) to rank many by findings. Read-only.
    """
    if target:
        return _py("faithful.py", target)
    if sweep >= 0:
        return _py("faithful.py", "--sweep", sweep)
    return {"error": "pass either target or sweep"}


@mcp.tool()
def ledger(action: str = "list", claim: str = "", verdict: str = "",
          evidence: str = "", tags: str = "") -> dict:
    """The record of what has already been tried and what it proved.

    `action=check` (needs `claim`) before starting any new investigation
    thread - it flags similar claims already on record, and REFUTED ones
    should stop you before you repeat a dead idea. `action=add` (needs
    `claim`, `verdict` in confirmed/refuted, `evidence`) records a result.
    `action=list` (default) or `action=report` (writes ledger_report.md) for
    an overview.
    """
    if action == "check":
        if not claim:
            return {"error": "check needs claim"}
        return _py("ledger.py", "check", claim)
    if action == "add":
        if not (claim and verdict and evidence):
            return {"error": "add needs claim, verdict and evidence"}
        args = ["add", "--claim", claim, "--verdict", verdict,
                "--evidence", evidence]
        if tags:
            args += ["--tags", tags]
        return _py("ledger.py", *args)
    if action == "report":
        return _py("ledger.py", "report")
    return _py("ledger.py", "list")


@mcp.tool()
def deepdive(target: str, no_faithful: bool = False,
             evidence: bool = False) -> dict:
    """Everything already known about one function, in one call.

    Gathers the lifted C's location, faithful.py's verdict, EVERY ledger
    entry naming the function, any hand-proven guards from manual_edits.json,
    its walls.json record if it is a known wall, and its callers - the six
    lookups a deep dive otherwise needs, correlated by hand.

    Call this BEFORE starting new analysis on a function. The ledger section
    is the point: a deep dive on sub_001F7930 began re-deriving a finding
    that ledger #16 already had, confirmed and better. Refuted entries are
    printed first and loudest for that reason.

    Read-only and takes NO build lock, so it is safe to run while an AFK tool
    (afk_start) is mid-run - verified against a held lock. It does NOT touch
    Ghidra, deliberately: Ghidra holds an exclusive project lock and driving
    it headless would fight the running instance, so the output ends with the
    exact ReVa call to make for the Ghidra half. Pair the two - diffing lifted
    C against Ghidra's decompilation is the comparison this project's notes
    call the one that has never given a wrong answer.

    `no_faithful` skips the capstone-dependent check.

    `evidence` controls the ledger section. OFF by default: each entry comes
    back as claim + verdict + tags + date, with the evidence replaced by a
    length and a pointer to `ledger(action="list")`. Evidence blocks on this
    project run to 500+ words each and deepdive returns EVERY entry naming
    the function - one call on sub_001EC9C0 returned three of them, ~1500
    words, when what was needed was three claims and three verdicts. Since
    the docstring above tells you to call this before any new analysis, it is
    also the most frequent call there is. Pass `evidence=True` when a claim
    looks relevant and you want to read it properly.
    """
    args = [target] + (["--no-faithful"] if no_faithful else []) + ["--json"]
    r = _py("deepdive.py", *args)
    if not r["ok"]:
        return {"error": (r["stderr"] or r["stdout"]).strip()}
    try:
        d = json.loads(r["stdout"])
    except ValueError:
        return {"error": "deepdive.py did not return JSON",
                "raw": r["stdout"][:2000]}

    if not evidence and isinstance(d.get("ledger"), list):
        trimmed = 0
        for e in d["ledger"]:
            ev = e.get("evidence")
            if isinstance(ev, str) and ev:
                trimmed += len(ev)
                e["evidence"] = "<%d chars - rerun with evidence=True>" % len(ev)
        if trimmed:
            d["evidence_omitted_chars"] = trimmed
    return d


@mcp.tool()
def walls_knowledge() -> dict:
    """walls.py's accumulated knowledge (walls.json), structured - no need to
    grep the file by hand. Each wall: kind, site, how many patterns were
    tried, whether it's bypassed or exhausted, and its faithful.py verdict if
    checked (see afk_start's walls.py docs - a wall whose function has a real
    faithful.py finding got a bypass applied to a SYMPTOM, not the cause).
    """
    path = os.path.join(GAME, "walls.json")
    if not os.path.exists(path):
        return {"found": False}
    kb = json.load(open(path, encoding="utf-8"))
    walls = []
    for key, v in kb.get("walls", {}).items():
        fr = v.get("faithful") or {}
        walls.append({
            "key": key, "kind": v.get("kind"), "site": v.get("site"),
            "seen": v.get("seen"), "steps": v.get("steps"),
            "tried": v.get("tried", []), "bypassed": bool(v.get("bypassed")),
            "exhausted": bool(v.get("exhausted")), "note": v.get("note"),
            "faithful_defect": bool(fr.get("missing_labels") or fr.get("stale_flags")),
        })
    return {"found": True, "walls": walls, "promoted": kb.get("promoted", []),
            "sweep_count": len(kb.get("sweeps", [])),
            "error_categories": list((kb.get("errors") or {}).keys())}


@mcp.tool()
def patterns() -> dict:
    """List walls.py's candidate-pattern library: name, one-line summary from
    its docstring, which wall kinds it's PROVEN for vs offered as a
    CANDIDATE, and whether it's been promoted from candidate to proven (from
    walls.json). Answers "what could walls.py even try here" without reading
    walls.py source.
    """
    import walls as w
    promoted = set()
    path = os.path.join(GAME, "walls.json")
    if os.path.exists(path):
        promoted = set(json.load(open(path, encoding="utf-8")).get("promoted", []))
    out = []
    for name, fn, proven_for, candidate_for in w.PATTERNS:
        doc = (fn.__doc__ or "").strip().splitlines()
        out.append({
            "name": name, "summary": doc[0] if doc else "",
            "proven_for": list(proven_for), "candidate_for": list(candidate_for),
            "promoted_for": sorted(k.split("@", 1)[1] for k in promoted
                                   if k.startswith(f"{name}@")),
        })
    return {"patterns": out}


@mcp.tool()
def manual_edits(function: str = "") -> dict:
    """The 139 hand-proven guards in manual_edits.json - the source `walls.py`
    patterns get mined from (see heap-range-guard's docstring for an example
    of that mining).

    Without `function`, returns a summary: total count and a per-function
    tally, not the full text - the file is large and most callers want to
    know WHERE the hand fixes are before reading one. Pass `function`
    (`sub_XXXXXXXX`) to get that function's full edit(s) verbatim.
    """
    path = os.path.join(TOOLS, "manual_edits.json")
    if not os.path.exists(path):
        return {"found": False}
    edits = json.load(open(path, encoding="utf-8"))
    if function:
        matches = [e for e in edits if e.get("function") == function]
        return {"found": bool(matches), "count": len(matches), "edits": matches}
    tally = {}
    for e in edits:
        tally[e.get("function", "?")] = tally.get(e.get("function", "?"), 0) + 1
    return {"total": len(edits),
            "by_function": sorted(tally.items(), key=lambda kv: -kv[1])}


@mcp.tool()
def bisect_journal(tail: int = 15, harness: str = "") -> dict:
    """Per-experiment history from overnight.py/bisect_core.py
    (bisect_journal.jsonl) - what was tried, kept, or dropped, and why.

    Summarized: each entry's candidate SUBSET can be dozens of function
    addresses, which would blow up the response the same way progress.py's
    full history once did - so this reports subset_size, not the raw list.
    `harness` filters to one source (e.g. "walls", "overnight"); `tail`
    limits how many recent entries print (0 for all).
    """
    path = os.path.join(GAME, "bisect_journal.jsonl")
    if not os.path.exists(path):
        return {"found": False}
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    if harness:
        rows = [r for r in rows if r.get("harness") == harness]
    total = len(rows)
    shown = rows[-tail:] if tail else rows
    out = [{"ts": r.get("ts"), "harness": r.get("harness"), "n": r.get("n"),
           "verdict": r.get("verdict"),
           "subset_size": len(r.get("subset", []) or []) if isinstance(
               r.get("subset"), list) else None,
           "signals": r.get("signals")} for r in shown]
    return {"total": total, "shown": len(out), "entries": out}


if __name__ == "__main__":
    mcp.run()
