"""Row keys by geometry: which numbered box on a picture shows the keyed line (#1321).

**Why.** A row key that says "box 2" is true only on the one numbered picture it was keyed on
(`picture_sha256`): a picture drawn another way (the item pictures of the drawing-context plan)
numbers the boxes differently, so every box-number case is "not asked" there and a change of
picture can never be judged. A geometry key names the line itself: the page (its content hash),
the line's height and its two end ticks, in page points (`keys.RowAt`), with a tolerance the key
states. A reader's chosen box is then the keyed line when that box's line lies within the tolerance
of the key's, on whatever picture the reader was shown.

**The box map (`pariksha-row-boxes/1`).** A stored run records which picture a question showed
and which box number each reader chose, but not where each numbered box was on the page. The box
map says it, per picture and page (one picture can belong to two pages: two drawing sets holding
the same drawing give byte-identical pictures, so a picture is looked up with its page):

    {"format": "pariksha-row-boxes/1",
     "pictures": [{"picture_sha256": "<64 hex>", "page_sha256": "<64 hex>",
                   "boxes": [{"y": 412.5, "x0": 100.0, "x1": 400.0}, ...],   # box 1 first
                   "how": "<how the boxes were proven>",
                   "px_per_pt": 1.3889}]}                                       # optional

`px_per_pt` (optional) is how many of the stored picture's pixels one page point spans, for a page
drawn upright from its top-left corner; only the review page uses it, to draw the keyed line.

**Proven, never estimated.** `prove_picture_boxes` takes the page as the product's own code builds
it (`workflow.slot_reader.SlotPage`), draws the numbered picture again with the product's own
picture code (`_numbered_rows_png`) and keeps the boxes only when the drawing is byte for byte the
stored picture (the same SHA-256): then box n *is* code's n-th candidate row, exactly. A picture
stored before #1108 was drawn in an earlier colour set (`EARLIER_PALETTES`), and is proven with it.
A picture that cannot be drawn again exactly gets no boxes, with the reason, and its questions are
"not asked" for a geometry key: never guessed from the pixels.

**Never chosen between.** On a picture where two boxes lie within the tolerance of the keyed line,
the case is not asked, with both box numbers named; on one where no box does, the right answer is
"not among the boxes" (and the pair is then split, as the product sends it to a person).

**Converting a box-number key** (`to_geometry_key`): each `box` answer becomes its box's line on
its own picture, refused when the picture has no proven boxes, when the box number is beyond them,
or when another box on that picture lies within the tolerance (the key would then name two lines).
"No countertop" drops its picture (it holds on any picture of the page); "not among the boxes"
keeps it (whether a line is boxed depends on the picture).

Local only: box maps name client pages by their hashes; they are kept beside the keys under
`~/gv-local/`, never in git.

Source: issue #1321 · Verification: `tests/eval/test_pariksha_row_geometry.py`
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from eval.pariksha.keys import (
    AnswerKey,
    KeyCase,
    KeyFormatError,
    RowAt,
    RowTruth,
    dump_key,
    load_key,
)

if TYPE_CHECKING:
    from workflow.slot_reader import SlotPage

__all__ = [
    "EARLIER_PALETTES",
    "ROW_BOXES_FORMAT",
    "PictureBoxes",
    "RowBoxes",
    "RowBoxesError",
    "dump_row_boxes",
    "load_row_boxes",
    "matching_boxes",
    "on_picture",
    "prove_picture_boxes",
    "read_row_boxes",
    "to_geometry_key",
    "write_row_boxes",
]

ROW_BOXES_FORMAT: Final = "pariksha-row-boxes/1"
#: Colour sets the numbered row picture was drawn in before today's (`ROW_BOX_COLOURS`): the
#: picture code is otherwise the same, so a picture stored then is drawn again exactly with these.
EARLIER_PALETTES: Final[Mapping[str, tuple[bytes, ...]]] = {
    "the colours used before #1108": (
        bytes((220, 20, 60)),
        bytes((0, 100, 220)),
        bytes((0, 135, 70)),
        bytes((145, 70, 190)),
        bytes((220, 125, 0)),
        bytes((0, 135, 150)),
    ),
}
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_HEADER: Final = frozenset({"format", "pictures"})
_PICTURE: Final = frozenset({"picture_sha256", "page_sha256", "boxes", "how", "px_per_pt"})
_PICTURE_REQUIRED: Final = _PICTURE - {"px_per_pt"}


class RowBoxesError(ValueError):
    """The box map cannot be used as asked; the message says which part and why."""


@dataclass(frozen=True, slots=True)
class PictureBoxes:
    """Where each numbered box of one stored picture is on its page, box 1 first."""

    picture_sha256: str
    page_sha256: str
    boxes: tuple[RowAt, ...]
    how: str
    """How the boxes were proven (for a person), e.g. drawn again byte for byte."""
    px_per_pt: Decimal | None = None
    """The stored picture's pixels per page point, for drawing on it (review page only)."""

    def same_boxes(self, other: PictureBoxes) -> bool:
        return (self.page_sha256, self.boxes) == (other.page_sha256, other.boxes)


@dataclass(frozen=True, slots=True)
class RowBoxes:
    """The box map: each proven picture's boxes, by the picture's and its page's hashes."""

    pictures: Mapping[tuple[str, str], PictureBoxes]

    def get(self, picture_sha256: str, page_sha256: str) -> PictureBoxes | None:
        """The picture's boxes on this page; `None` when the map does not know that pair."""
        return self.pictures.get((picture_sha256, page_sha256))

    def for_page(self, page_sha256: str) -> tuple[PictureBoxes, ...]:
        return tuple(
            picture for picture in self.pictures.values() if picture.page_sha256 == page_sha256
        )

    def merged(self, other: RowBoxes) -> RowBoxes:
        """Both maps in one; one picture with other boxes in each is refused, never chosen."""
        return _collect((*self.pictures.values(), *other.pictures.values()))

    @classmethod
    def of(cls, pictures: Iterable[PictureBoxes]) -> RowBoxes:
        return _collect(pictures)


def _collect(pictures: Iterable[PictureBoxes]) -> RowBoxes:
    found: dict[tuple[str, str], PictureBoxes] = {}
    for picture in pictures:
        pair = (picture.picture_sha256, picture.page_sha256)
        known = found.get(pair)
        if known is not None and not known.same_boxes(picture):
            raise RowBoxesError(
                f"picture {picture.picture_sha256[:12]} on page {picture.page_sha256[:12]} is "
                f"named twice with other boxes ({len(known.boxes)} and {len(picture.boxes)} "
                "boxes); a picture's numbering is one fact, so a person settles which is right"
            )
        found.setdefault(pair, picture)
    return RowBoxes(pictures=found)


def _sha(value: object, field: str, where: str) -> str:
    if not isinstance(value, str) or not _SHA256.match(value):
        raise RowBoxesError(f"{where}: {field} must be 64 lower-case hex characters")
    return value


def _picture(data: object, index: int) -> PictureBoxes:
    where = f"picture {index + 1}"
    if not isinstance(data, Mapping):
        raise RowBoxesError(f"{where} is not an object")
    extra = sorted(set(data) - _PICTURE)
    missing = sorted(_PICTURE_REQUIRED - set(data))
    if extra or missing:
        raise RowBoxesError(
            f"{where}: unknown field(s) {', '.join(extra) or 'none'}, missing "
            f"{', '.join(missing) or 'none'}"
        )
    raw_boxes = data["boxes"]
    if not isinstance(raw_boxes, list) or not raw_boxes:
        raise RowBoxesError(f"{where}: boxes must be a non-empty list, box 1 first")
    try:
        boxes = tuple(
            RowAt.read(box, f"{where} box {number}")
            for number, box in enumerate(raw_boxes, start=1)
        )
    except KeyFormatError as error:
        raise RowBoxesError(str(error)) from error
    how = data["how"]
    if not isinstance(how, str) or not how.strip():
        raise RowBoxesError(f"{where}: how must say how the boxes were proven")
    scale = data.get("px_per_pt")
    px_per_pt: Decimal | None = None
    if scale is not None:
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not scale > 0:
            raise RowBoxesError(f"{where}: px_per_pt must be a positive number, not {scale!r}")
        px_per_pt = Decimal(str(scale))
    return PictureBoxes(
        picture_sha256=_sha(data["picture_sha256"], "picture_sha256", where),
        page_sha256=_sha(data["page_sha256"], "page_sha256", where),
        boxes=boxes,
        how=how.strip(),
        px_per_pt=px_per_pt,
    )


def load_row_boxes(data: object) -> RowBoxes:
    """Read a box map strictly: unknown fields, a bad hash or line, or a picture named twice with
    other boxes are refused."""
    if not isinstance(data, Mapping):
        raise RowBoxesError("a box map is one JSON object")
    if data.get("format") != ROW_BOXES_FORMAT:
        raise RowBoxesError(f"format must be {ROW_BOXES_FORMAT!r}, not {data.get('format')!r}")
    extra = sorted(set(data) - _HEADER)
    if extra:
        raise RowBoxesError(f"unknown field(s) in the box map: {', '.join(extra)}")
    pictures = data.get("pictures")
    if not isinstance(pictures, list):
        raise RowBoxesError("pictures must be a list")
    return _collect(_picture(item, index) for index, item in enumerate(pictures))


def read_row_boxes(paths: Sequence[Path]) -> RowBoxes:
    """One or more box map files, merged (a conflict between them is refused)."""
    merged = RowBoxes(pictures={})
    for path in paths:
        try:
            data = json.loads(Path(path).expanduser().read_text())
        except FileNotFoundError as error:
            raise RowBoxesError(f"no box map at {path}") from error
        except json.JSONDecodeError as error:
            raise RowBoxesError(f"{path} is not JSON: {error}") from error
        merged = merged.merged(load_row_boxes(data))
    return merged


def dump_row_boxes(boxes: RowBoxes) -> dict[str, Any]:
    pictures: list[dict[str, object]] = []
    for picture in sorted(boxes.pictures.values(), key=lambda p: (p.page_sha256, p.picture_sha256)):
        document: dict[str, object] = {
            "picture_sha256": picture.picture_sha256,
            "page_sha256": picture.page_sha256,
            "boxes": [box.document() for box in picture.boxes],
            "how": picture.how,
        }
        if picture.px_per_pt is not None:
            document["px_per_pt"] = float(picture.px_per_pt)
        pictures.append(document)
    return {"format": ROW_BOXES_FORMAT, "pictures": pictures}


def write_row_boxes(boxes: RowBoxes, path: Path) -> None:
    """Write the map once (an existing file is refused); the caller checks it is outside git."""
    text = json.dumps(dump_row_boxes(boxes), indent=1) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        handle.write(text)


# --- matching --------------------------------------------------------------------------------


def matching_boxes(line: RowAt, picture: PictureBoxes, tolerance: Decimal) -> tuple[int, ...]:
    """The numbers (from 1) of the picture's boxes whose line lies within `tolerance` of `line`."""
    return tuple(
        number
        for number, box in enumerate(picture.boxes, start=1)
        if line.distance(box) <= tolerance
    )


def on_picture(line: RowAt, picture: PictureBoxes, tolerance: Decimal) -> RowTruth | str:
    """What the keyed line means on this picture: its box, "not among the boxes", or why not
    knowable (two boxes within the tolerance: never chosen between)."""
    found = matching_boxes(line, picture, tolerance)
    if len(found) == 1:
        return RowTruth(box=found[0], kind="row", at=None)
    if not found:
        return RowTruth(box=None, kind="not_among_boxes")
    named = ", ".join(str(number) for number in found[:-1]) + f" and {found[-1]}"
    return (
        f"the keyed line matches boxes {named} of picture {picture.picture_sha256[:12]} within "
        f"{tolerance} pt; which one the key means is not knowable"
    )


# --- converting a box-number key -------------------------------------------------------------


def _converted(case: KeyCase, boxes: RowBoxes, tolerance: Decimal) -> KeyCase:
    truth = case.truth
    if not isinstance(truth, RowTruth):
        raise RowBoxesError(f"{case.question}: only row cases are converted")
    if truth.kind == "no_countertop":
        return replace(case, picture_sha256=None)
    if truth.kind != "row" or truth.at is not None:
        return case
    assert truth.box is not None and case.picture_sha256 is not None
    picture = boxes.get(case.picture_sha256, case.question.page_sha256)
    if picture is None:
        raise RowBoxesError(
            f"{case.question}: no box geometry for its picture {case.picture_sha256[:12]} on its "
            "page; build the box map for that picture first (scripts/pariksha_row_boxes.py build)"
        )
    if truth.box > len(picture.boxes):
        raise RowBoxesError(
            f"{case.question}: box {truth.box} is beyond the {len(picture.boxes)} boxes of "
            f"picture {case.picture_sha256[:12]}"
        )
    line = picture.boxes[truth.box - 1]
    found = matching_boxes(line, picture, tolerance)
    if found != (truth.box,):
        raise RowBoxesError(
            f"{case.question}: box {truth.box}'s line is within the {tolerance} pt tolerance of "
            f"boxes {', '.join(map(str, found))} on its own picture; a tighter tolerance is needed "
            "to name it alone"
        )
    note = f"box {truth.box} of picture {case.picture_sha256[:12]} ({picture.how})"
    return replace(
        case,
        truth=RowTruth(box=None, kind="row", at=line),
        picture_sha256=None,
        source=f"{case.source}; {note}" if case.source else note,
    )


def to_geometry_key(
    key: AnswerKey, boxes: RowBoxes, *, tolerance: Decimal, name: str, version: str
) -> AnswerKey:
    """The box-number key as a geometry key, read back through the strict loader."""
    if key.agent != "rekha":
        raise RowBoxesError(f"only row keys are converted, not a {key.agent} key")
    converted = tuple(_converted(case, boxes, tolerance) for case in key.cases)
    geometry = any(isinstance(c.truth, RowTruth) and c.truth.at is not None for c in converted)
    document = dump_key(
        replace(
            key,
            name=name,
            version=version,
            cases=converted,
            row_tolerance_pt=tolerance if geometry else None,
        )
    )
    loaded = load_key(document)
    assert isinstance(loaded, AnswerKey)
    return loaded


# --- proving a stored picture's boxes --------------------------------------------------------


def _drawn(page: SlotPage, palette: tuple[bytes, ...] | None) -> tuple[bytes, tuple[object, ...]]:
    from unittest import mock

    from workflow import slot_reader

    if palette is None:
        png, candidates = slot_reader._numbered_rows_png(page)
    else:
        with mock.patch.object(slot_reader, "_ROW_COLOURS", palette):
            png, candidates = slot_reader._numbered_rows_png(page)
    return png, tuple(candidates)


def _px_per_pt(page: SlotPage, png: bytes) -> Decimal | None:
    """The stored picture's pixels per page point, for a page drawn upright from the corner
    pdfplumber measures from; `None` otherwise (the review page then draws no mark)."""
    transform = page.transform
    if transform is None or transform.rotation != 0:
        return None
    if transform.crop_box[:2] != transform.media_box[:2]:
        return None
    width = int.from_bytes(png[16:20], "big")  # the PNG header's width
    rendered = page.rendered
    scale = Decimal(width) / Decimal(rendered.width_px) * Decimal(rendered.dpi) / Decimal(72)
    return scale.quantize(Decimal("0.0001"))


def prove_picture_boxes(
    page: SlotPage, *, picture_sha256: str, page_sha256: str
) -> PictureBoxes | str:
    """The stored picture's boxes, proven by drawing it again byte for byte, or why not.

    `page` is the page as the product built it for the reading (its render, its row candidates
    with the run's architect drawings set aside). The numbered picture is drawn again with the
    product's own picture code, in today's colours and then each earlier colour set; only a
    drawing with the stored picture's exact SHA-256 gives the boxes: box n is then code's n-th
    candidate row."""
    attempts = (("today's colours", None), *EARLIER_PALETTES.items())
    for name, palette in attempts:
        png, candidates = _drawn(page, palette)
        if hashlib.sha256(png).hexdigest() != picture_sha256:
            continue
        if not candidates:
            return "the picture shows no numbered box"
        boxes = tuple(
            RowAt.of(candidate.y, candidate.x0, candidate.x1)  # type: ignore[attr-defined]
            for candidate in candidates
        )
        return PictureBoxes(
            picture_sha256=picture_sha256,
            page_sha256=page_sha256,
            boxes=boxes,
            how=(
                "code's row candidates; the product's picture code draws the stored picture "
                f"again byte for byte ({name})"
            ),
            px_per_pt=_px_per_pt(page, png),
        )
    return (
        f"picture {picture_sha256[:12]} was not drawn again byte for byte by today's picture code "
        f"(tried today's colours and {len(EARLIER_PALETTES)} earlier set(s)); its boxes are not "
        "proven, so a geometry case on it is not asked"
    )
