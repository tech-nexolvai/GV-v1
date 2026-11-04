#!/usr/bin/env python3
"""A saved set pack's labels read again with today's code, at $0, for a replay (2026-11-04).

When the label reader changes (`dim_labels`, `PACK_VERSION`), a saved job's pack still holds the
labels the old code read. This copies the pack to a new folder and reads its labels again there:
`dims.json` and every fact the labels step makes from it (`scales.json`, `pairs.json`,
`notes.json`, `health.json`, `reading_coverage.json`, `reading.json`), exactly as the set pack does
(`set_pack.write_dims`). The saved pack is never changed; the copy keeps its saved AI answers
(`spot_reads/`, `readings.json`, `page_match.json`...) for a replay. No model and no network: the
drawn route's two OCR engines, the OCR route and `read_number` at pack time are code on this
machine, and every key, token and database address is taken out of the environment first. The
pack's code checks can then be run on the copy (`vendor_consistency.check_pack(<out>, <uploads>)`).

    .venv/bin/python scripts/rebuild_pack_dims.py <pack folder> <uploads folder> --out <new folder>

`<uploads folder>` holds the files the pack was made from, by the names in its `page_map.json` (a
chat's `uploads/`). `--workers N` reads N pages at once (default: the set pack's own number). The
copy holds a client's drawing values: `--out` inside a git working tree, or one that exists, is
refused. Exit codes: 0 done, 2 refused.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

REFUSED = 2
#: What is not copied: the hi-res page cache (made again on need) and the pages the old code read
#: (`dims_pool`'s kept pages: the new code reads every page again).
SKIPPED = ("hires", ".dims-pages")
#: Environment names that may hold a key, a token or an address of a paid service or a database.
_SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|_URL$", re.IGNORECASE)


class Refused(Exception):
    """The run would write where it must not, or has nothing to read."""


def outside_git(folder: Path) -> Path:
    """`folder`, resolved, when no git working tree holds it (a client's values stay out of any
    repository); else `Refused`."""
    resolved = folder.expanduser().resolve()
    probe = resolved
    while not probe.exists():
        probe = probe.parent
    found = subprocess.run(
        ["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode == 0 and found.stdout.strip() == "true":
        raise Refused(f"{resolved} is inside a git working tree")
    return resolved


def pack_files(pack: Path, uploads: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """The pack's upload names and page map pages, each upload found in `uploads`; else
    `Refused`."""
    try:
        page_map = json.loads((pack / "page_map.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as failed:
        raise Refused(f"{pack} has no readable page_map.json") from failed
    names = [str(name) for name in page_map.get("files") or []]
    if not names:
        raise Refused(f"{pack}'s page map names no files")
    missing = [name for name in names if not (uploads / name).is_file()]
    if missing:
        raise Refused(f"{uploads} does not hold {', '.join(missing)}")
    return names, [dict(page) for page in page_map.get("pages") or []]


def without_secrets(environ: dict[str, str]) -> list[str]:
    """Every key, token or address of a paid service or a database taken out of `environ` (the
    page workers inherit it): their names."""
    gone = [name for name in environ if _SECRET.search(name)]
    for name in gone:
        del environ[name]
    return gone


def copy_pack(pack: Path, out: Path) -> None:
    """The pack copied to `out`, without `SKIPPED`."""
    shutil.copytree(pack, out, ignore=shutil.ignore_patterns(*SKIPPED))


def label_counts(folder: Path) -> dict[str, int]:
    """Labels per side in a pack's `dims.json` (empty when it has none)."""
    try:
        dims = json.loads((folder / "dims.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    counts: dict[str, int] = {}
    for page in dims.get("pages") or []:
        for label in page.get("labels") or []:
            side = str(label.get("side") or "unknown")
            counts[side] = counts.get(side, 0) + 1
    return counts


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pack", type=Path)
    parser.add_argument("uploads", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=0)
    args = parser.parse_args(arguments)
    pack, uploads = args.pack.expanduser().resolve(), args.uploads.expanduser().resolve()
    try:
        out = outside_git(args.out)
        if out.exists():
            raise Refused(f"{out} exists")
        names, pages = pack_files(pack, uploads)
    except Refused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return REFUSED
    gone = without_secrets(os.environ)
    from app.review.assistant import dims_pool, set_pack

    if args.workers > 0:
        os.environ[dims_pool.WORKERS_SETTING] = str(args.workers)
    else:
        os.environ.setdefault(dims_pool.WORKERS_SETTING, str(dims_pool.workers()))
    copy_pack(pack, out)
    started = time.monotonic()
    set_pack.write_dims(out, uploads, names, pages)
    print(
        json.dumps(
            {
                "pack": str(pack),
                "out": str(out),
                "pack_version": set_pack.PACK_VERSION,
                "labels_before": label_counts(pack),
                "labels_after": label_counts(out),
                "seconds": round(time.monotonic() - started, 1),
                "environment_left_out": sorted(gone),
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
