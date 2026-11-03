"""A helper's box recorded in a shrunk page picture's pixels, put back in PDF points ($0, no AI).

Every box the check records is in PDF points of the page as shown (origin top-left). A helper reads
the set pack's page pictures (`set_pack`: 100 dpi), which the `view` tool shrinks to fit what the
model reads (`sandbox._fit`: at most 1568 px a side for a helper, 2576 for the lead). A box read off
that picture is in its pixels: on a 36 x 24 in sheet a helper's pixel is about 1.65 points, so the
box lands at about 0.6 of where the value is, often on blank paper (Set A, 2026-11-04: a schedule's
"34" recorded at 985-1040 x 315-335 where the page prints it at 1667-1689 x 542-564).

The `view` tool now says how its pixels map to points (`sandbox._points_note`); this module fixes a
box already recorded in pixels, and only where code proves it:

1. **Proved by the box's own value.** A helper's side box (one code did not place on a label) whose
   spot prints none of the side's numbers and none of its words, while the same box in a shrunk
   picture's frame (each frame the page could have been shown in) holds one of its numbers,
   printed as text: the box moves there (to that phrase's own box when exactly one matches).
2. **The same helper's frame.** Another box of the same helper on the same page proved the frame
   (1.), and this box is on blank paper, or its side names numbers and the box prints none of
   them, while the box in that frame holds printed text: it moves there too.

Anything else is left as recorded. A number of two or more characters must match (a lone "5" is
everywhere on a schedule); words only ever keep a box where it is. Pure code: no I/O beyond reading a page
picture's size. Verification: `tests/review/assistant/test_picture_frame.py`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "FIELD",
    "Moved",
    "fix_boxes",
    "frames",
    "sizes_for_pack",
]

#: The finding's record of a box code moved out of a picture's pixels.
FIELD: Final = "box_frame"
#: The page pictures' dpi (`set_pack.RENDER_DPI`).
_DPI: Final = 100
#: The longest side each reader of a page picture sees it at (`prompt_budget`, `sandbox`).
_EDGES: Final = (1568, 2576)
#: How far round a spot a printed phrase may sit and still be the spot's (points).
_SLACK_PT: Final = 2.0
#: The sides and their boxes.
_SIDES: Final = (("vendor", "vendor_box"), ("architect", "arch_box"))

type Box = tuple[float, float, float, float]
type PageSize = Callable[[str, int], tuple[float, float] | None]


@dataclass(frozen=True, slots=True)
class Moved:
    """One box code moved: the finding, the side, the old and new box, how."""

    finding_id: str
    side: str
    old: Box
    new: Box
    how: str


def frames(size_pt: tuple[float, float]) -> list[float]:
    """Points per pixel of each shrunk picture the page could have been read in, largest shrink
    first; none when the picture is shown at its own size."""
    fit = __import__("app.review.assistant.sandbox", fromlist=["_fit"])._fit
    width_px = max(1, round(size_pt[0] * _DPI / 72))
    height_px = max(1, round(size_pt[1] * _DPI / 72))
    out: list[float] = []
    for edge in _EDGES:
        shown = fit(width_px, height_px, max_edge=edge)
        if shown[0] >= width_px:
            continue
        factor = (width_px / shown[0]) * 72 / _DPI
        if all(abs(factor - f) > 0.01 for f in out):
            out.append(factor)
    return out


def _numbers(text: object) -> set[str]:
    """The numbers of two or more characters in `text` (`34`, `6.7`, `121`)."""
    return {n for n in re.findall(r"\d+(?:\.\d+)?", str(text or "")) if len(n) >= 2}


def _words(text: object) -> set[str]:
    """The words of four or more letters in `text`, singular (`DOORS` is `DOOR`)."""
    return {w.removesuffix("S") for w in re.findall(r"[A-Z]{4,}", str(text or "").upper())}


def _words_there(phrases: Sequence[Mapping[str, Any]], box: Box, claim: object) -> bool:
    """The box prints one of the claim's words (`WATER DISPENSER` for "water dispenser by GV"):
    the box is where the claim is, whatever its numbers."""
    wanted = _words(claim)
    return bool(wanted) and any(_words(text) & wanted for text, _ in _texts(phrases, box))


def _meets(a: Sequence[float], b: Sequence[float]) -> bool:
    s = _SLACK_PT
    return a[0] < b[2] + s and b[0] < a[2] + s and a[1] < b[3] + s and b[1] < a[3] + s


def _texts(
    phrases: Sequence[Mapping[str, Any]], box: Box
) -> list[tuple[str, tuple[float, float, float, float]]]:
    out = []
    for phrase in phrases:
        b = phrase.get("box")
        if isinstance(b, Sequence) and len(b) == 4 and _meets([float(v) for v in b], box):
            out.append(
                (
                    str(phrase.get("text") or ""),
                    (float(b[0]), float(b[1]), float(b[2]), float(b[3])),
                )
            )
    return out


def _hits(phrases: Sequence[Mapping[str, Any]], box: Box, wanted: set[str]) -> list[Box]:
    return [b for text, b in _texts(phrases, box) if _numbers(text) & wanted]


def _box_of(raw: object) -> tuple[str | None, int, Box] | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        box = (float(raw["x0"]), float(raw["y0"]), float(raw["x1"]), float(raw["y1"]))
        page = int(raw["page"])
    except (KeyError, TypeError, ValueError):
        return None
    file = raw.get("file")
    return (str(file) if file else None), page, box


def _scaled(box: Box, factor: float, size: tuple[float, float]) -> Box | None:
    new = (box[0] * factor, box[1] * factor, box[2] * factor, box[3] * factor)
    if new[2] > size[0] + 1 or new[3] > size[1] + 1:
        return None  # off the page: not this frame
    return new


def _helper(finding: Mapping[str, Any]) -> bool:
    return str(finding.get("created_by") or "").startswith("helper")


def _placed(finding: Mapping[str, Any], side: str) -> bool:
    """Code placed this side's box on a label it numbered (`dims`): points, never pixels."""
    dims = finding.get("dims")
    label = dims.get(side) if isinstance(dims, Mapping) else None
    return isinstance(label, Mapping) and bool(label.get("id"))


def _set(finding: dict[str, Any], key: str, file: str | None, page: int, box: Box) -> None:
    finding[key] = {
        **(finding.get(key) or {}),
        "page": page,
        "x0": round(box[0], 2),
        "y0": round(box[1], 2),
        "x1": round(box[2], 2),
        "y1": round(box[3], 2),
        **({"file": file} if file else {}),
    }


def fix_boxes(
    findings: Sequence[dict[str, Any]],
    phrases: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    page_size: PageSize | None,
    main_file: str | None = None,
) -> list[Moved]:
    """Move each helper box code proves was recorded in a shrunk picture's pixels (see the module),
    in place; returns what moved (each finding also carries `FIELD`)."""
    if page_size is None:
        return []
    moved: list[Moved] = []
    proven: dict[tuple[str, str, int], float] = {}
    later: list[tuple[dict[str, Any], str, str, str, int, Box]] = []
    for finding in findings:
        if not _helper(finding):
            continue
        for side, key in _SIDES:
            where = _box_of(finding.get(key))
            if where is None or _placed(finding, side):
                continue
            name, page, box = where
            file = name or main_file or ""
            text = phrases.get((file, page)) or []
            size = page_size(file, page)
            if not text or size is None:
                continue
            wanted = _numbers(finding.get(side))
            if (wanted and _hits(text, box, wanted)) or _words_there(text, box, finding.get(side)):
                continue  # the box prints its value (or its words): points
            found = None
            for factor in frames(size):
                scaled = _scaled(box, factor, size)
                hits = _hits(text, scaled, wanted) if scaled is not None and wanted else []
                if scaled is not None and hits:
                    found = (factor, hits[0] if len(hits) == 1 else scaled)
                    break
            if found is None:
                later.append((finding, side, key, file, page, box))
                continue
            factor, new = found
            proven[(str(finding.get("created_by")), file, page)] = factor
            how = (
                f"the box was in a shrunk page picture's pixels (x {factor:.2f} to points): code "
                f"reads the {side}'s {', '.join(sorted(wanted))} printed there"
            )
            _set(finding, key, name, page, new)
            finding[FIELD] = {**(finding.get(FIELD) or {}), side: how}
            moved.append(Moved(str(finding.get("id")), side, box, new, how))
    for finding, side, key, file, page, box in later:
        same = proven.get((str(finding.get("created_by")), file, page))
        size = page_size(file, page)
        text = phrases.get((file, page)) or []
        if same is None or size is None:
            continue  # no frame proved for this helper on this page
        if not _numbers(finding.get(side)) and _texts(text, box):
            continue  # words only, on printed text: nothing says the box is off
        scaled = _scaled(box, same, size)
        if scaled is None or not _texts(text, scaled):
            continue
        how = (
            f"the box was in a shrunk page picture's pixels (x {same:.2f} to points), as this "
            "helper's other box on the page proves; it printed none of the side's numbers"
        )
        recorded = _box_of(finding.get(key))
        _set(finding, key, recorded[0] if recorded else None, page, scaled)
        finding[FIELD] = {**(finding.get(FIELD) or {}), side: how}
        moved.append(Moved(str(finding.get("id")), side, box, scaled, how))
    return moved


def sizes_for_pack(pack: Path | None) -> PageSize | None:
    """Each page's size in points, from the set pack's page picture (`pages/<name>/p-NNN.png` at
    100 dpi): its header only."""
    if pack is None:
        return None
    folder = __import__("app.review.assistant.sandbox", fromlist=["page_folder"]).page_folder
    cache: dict[tuple[str, int], tuple[float, float] | None] = {}

    def size(file: str, page: int) -> tuple[float, float] | None:
        key = (file, page)
        if key not in cache:
            path = pack / "pages" / folder(file) / f"p-{page:03d}.png"
            try:
                from PIL import Image

                with Image.open(path) as picture:
                    w, h = picture.size
                cache[key] = (w * 72 / _DPI, h * 72 / _DPI)
            except (OSError, ValueError):
                cache[key] = None
        return cache[key]

    return size
