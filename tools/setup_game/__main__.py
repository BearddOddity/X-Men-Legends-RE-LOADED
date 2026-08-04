#!/usr/bin/env python3
"""Bring your own copy: extract, identify and verify the user's game disc.

We ship no game data. The user points this at a disc image (or an already
extracted folder) they legally own; it verifies the dump is one the
recompiler was actually developed against, then stages the files locally so
the recompilation can run on their machine.

    py -3 -m tools.setup_game "X-Men Legends.iso"
    py -3 -m tools.setup_game path/to/extracted/folder
    py -3 -m tools.setup_game game.iso --dest src/game/game

Exit codes:  0 ready   1 usage/extraction failure   2 unrecognised dump
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.xbe_parser.xbe_parser import XBEParser          # noqa: E402
from tools.setup_game.manifest import SUPPORTED, KNOWN_TITLE_IDS  # noqa: E402

ISO_SUFFIXES = {".iso", ".xiso"}
DEFAULT_DEST = REPO / "src" / "game" / "game"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_extractor() -> str | None:
    """extract-xiso, from PATH or EXTRACT_XISO."""
    env = os.environ.get("EXTRACT_XISO")
    if env and Path(env).is_file():
        return env
    return shutil.which("extract-xiso") or shutil.which("extract-xiso.exe")


def extract_iso(iso: Path, workdir: Path) -> Path:
    """Extract an ISO to workdir and return the directory holding default.xbe."""
    tool = find_extractor()
    if not tool:
        sys.exit(
            "Need extract-xiso to read a disc image.\n"
            "  Get it:  https://github.com/XboxDev/extract-xiso\n"
            "  Then put it on PATH, or set EXTRACT_XISO=/path/to/extract-xiso\n"
            "\n"
            "Already extracted the disc yourself? Point this at the folder\n"
            "containing default.xbe instead of at the .iso."
        )
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"extracting {iso.name} ...")
    # -x extract, -d destination. Args passed as a list; nothing goes via a shell.
    r = subprocess.run([tool, "-x", str(iso), "-d", str(workdir)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"extract-xiso failed ({r.returncode}):\n{r.stderr or r.stdout}")
    return workdir


def locate_xbe(root: Path) -> Path:
    """Find default.xbe at the root or one level down (extractors vary)."""
    direct = root / "default.xbe"
    if direct.is_file():
        return direct
    hits = sorted(root.glob("*/default.xbe")) or sorted(root.rglob("default.xbe"))
    if not hits:
        sys.exit(f"no default.xbe found under {root}")
    return hits[0]


def describe(xbe: Path) -> dict:
    cert = XBEParser(str(xbe)).parse().certificate
    return {
        "title": cert.title_name,
        "title_id": cert.title_id,
        "region": cert.game_region,
        "disc": cert.disc_number,
        "version": cert.version,
    }


def report_unrecognised(digest: str, info: dict) -> None:
    print("\nThis dump is not one the recompiler has been verified against.\n")
    print(f"  found    {info['title']!r}  title_id=0x{info['title_id']:08X}  "
          f"region=0x{info['region']:X}")
    print(f"  sha256   {digest}")
    known = KNOWN_TITLE_IDS.get(info["title_id"])
    if known:
        print(f"\n  That is {known}, but not a build we have verified - most "
              f"likely a different\n  region or revision. It will recompile, "
              f"and then misbehave in ways that\n  look like lifter bugs.")
    print("\n  verified dumps:")
    for d, m in SUPPORTED.items():
        print(f"    {m['label']}\n      sha256 {d}")
    print("\n  Re-run with --force to proceed anyway (you are on your own).")


def stage(src_dir: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for item in src_dir.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
        n += 1
    print(f"staged {n} item(s) -> {dest}")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="setup_game",
        description="Verify and stage a user-supplied Xbox game disc.")
    ap.add_argument("source", help="disc image (.iso/.xiso) or extracted folder")
    ap.add_argument("--dest", default=str(DEFAULT_DEST),
                    help=f"where to stage game files (default: {DEFAULT_DEST})")
    ap.add_argument("--force", action="store_true",
                    help="stage even if the dump is unrecognised")
    ap.add_argument("--verify-only", action="store_true",
                    help="identify and hash the dump, copy nothing")
    args = ap.parse_args()

    source = Path(args.source).expanduser()
    if not source.exists():
        sys.exit(f"no such path: {source}")

    if source.is_file():
        if source.suffix.lower() not in ISO_SUFFIXES:
            sys.exit(f"expected a folder or {'/'.join(sorted(ISO_SUFFIXES))}, "
                     f"got {source.suffix or 'a file with no extension'}")
        game_dir = extract_iso(source, Path(args.dest).parent / "game_extracted")
    else:
        game_dir = source

    xbe = locate_xbe(game_dir)
    game_dir = xbe.parent
    digest = sha256(xbe)
    info = describe(xbe)

    print(f"\n  {info['title']}")
    print(f"  title_id 0x{info['title_id']:08X}   region 0x{info['region']:X}"
          f"   disc {info['disc']}   version 0x{info['version']:X}")
    print(f"  sha256   {digest}")

    match = SUPPORTED.get(digest)
    if match:
        print(f"  VERIFIED: {match['label']}  (XDK {match['xdk']})")
    else:
        report_unrecognised(digest, info)
        if not args.force:
            return 2
        print("\n  --force given; continuing with an unverified dump.")

    if args.verify_only:
        return 0

    stage(game_dir, Path(args.dest))
    print("\nready. Next: recompile from your copy -")
    print("  py -3 -m tools.recomp game/default.xbe --all --split 1000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
