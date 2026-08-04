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

from mcp.server.fastmcp import FastMCP

_HERE = os.path.dirname(os.path.abspath(__file__))          # tools/mcp_server
REPO = os.path.dirname(os.path.dirname(_HERE))              # repo root
GAME = os.path.join(REPO, "src", "game")
TOOLS = os.path.join(GAME, "tools_data")
GEN = os.path.join(GAME, "src", "recomp", "gen")
STDERR = os.path.join(GAME, "stderr.txt")

mcp = FastMCP("XboxRecomp")


def _py(script, *args, timeout=900, env_extra=None):
    """Run one of the project's own tools and return its output."""
    cmd = [sys.executable, os.path.join(TOOLS, script), *map(str, args)]
    env = None
    if env_extra:
        env = dict(os.environ)
        env.update(env_extra)
    p = subprocess.run(cmd, cwd=GAME, capture_output=True, text=True,
                       timeout=timeout, env=env)
    return {"ok": p.returncode == 0, "stdout": p.stdout, "stderr": p.stderr}


@mcp.tool()
def build() -> dict:
    """Compile the recompiled executable. Returns errors only, not the full log."""
    bat = os.path.join(GAME, "build_compile.bat")
    p = subprocess.run(["cmd", "/c", bat], cwd=GAME, capture_output=True,
                       text=True, timeout=1800)
    out = p.stdout + p.stderr
    errors = [l for l in out.splitlines()
              if "error C" in l or "error LNK" in l or l.startswith("FAILED")]
    linked = "Linking C executable" in out
    return {"linked": linked, "errors": errors[:20],
            "error_count": len(errors)}


@mcp.tool()
def run(times: int = 2, watch: str = "") -> dict:
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
    """
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


@mcp.tool()
def triage() -> dict:
    """Explain how the last run ended - crash site and registers, or hang details."""
    r = _py("triage_crash.py")
    return {"report": r["stdout"] or r["stderr"]}


@mcp.tool()
def progress(record: str = "", note: str = "") -> dict:
    """Read the progress history, or record a verified result.

    Pass `record` to add an entry. Numbers come from the current stderr.txt, so
    build and run first. Recording is refused if probes are still present.
    """
    if record:
        args = ["record", "-m", record] + (["--note", note] if note else [])
        return _py("progress.py", *args)
    return _py("progress.py")


@mcp.tool()
def function(name_or_addr: str) -> dict:
    """Return the lifted C for a function, and where it lives.

    Accepts `sub_001A3554` or `0x001A3554`. Pair this with ReVa's
    get-decompilation on the same address to diff lifted output against
    Ghidra's ground truth - that comparison is what found the dropped
    fall-through edges.
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
            if lines[j].rstrip("\n") == "}":
                return {"found": True, "file": fn, "line": i + 1,
                        "source": "".join(lines[i:j + 1])}
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
          fmt: str = "", args: str = "", limit: int = 20) -> dict:
    """Insert a debug probe via add_probe.py. Tag must be alphanumeric.

    Values are C expressions, e.g. `esi,MEM32(ebp + -16)`. The anchor must
    match exactly one line or nothing is written.
    """
    if not (after or before):
        return {"error": "give either `after` or `before` as the anchor"}
    a = ["--after", after] if after else ["--before", before]
    extra = (["--fmt", fmt] if fmt else []) + (["--args", args] if args else [])
    return _py("add_probe.py", os.path.join("src", "recomp", "gen", file),
               *a, "--tag", tag, *extra, "--limit", limit)


@mcp.tool()
def strip_probes() -> dict:
    """Remove all probes. Refuses any removal that would unbalance braces."""
    return _py("strip_probes.py", "--apply")


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


if __name__ == "__main__":
    mcp.run()
