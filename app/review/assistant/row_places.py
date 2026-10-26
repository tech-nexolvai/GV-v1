"""Pieces of a row of sizes paired by where each starts along the row: code, $0, no AI (2026-10-27).

A run of counter is often dimensioned as a row of sizes on one line, end to end (a chain), with the
whole run's size printed beside it. Two pieces of such a row can print the same value and still be
different pieces: a by-eye check of a real upload found the vendor's range gap (made-up values
here: a 24" piece starting 34" along the side counter) paired by the AI with the reference's other
24", starting 10" along. Both say 24", so the tick was true by luck, but the marked spot was the
wrong piece.

**The rule.** Where an item called the same pairs a vendor label and a reference label that print
the same value, and each is a piece of a row of sizes code can measure (`rows_of`), code records
each piece's start: the printed sizes before it along its row, added up exactly. Then:

- the two rows must have the same total (the whole row's printed sizes added up; a vendor's
  field-cut piece code proved at a row's end, `field_cut.page_field_cuts`, is left out first);
- the rows are read the same way round when more of the vendor's joints fall where the
  reference's do read that way than read the other way (a reversed unit is drawn mirrored); a tie
  means the way round is not known, and nothing is decided;
- the vendor's piece and the AI's reference piece starting at the same place (within
  `PLACE_TOLERANCE`) is recorded, nothing else changes;
- otherwise, a reference piece of the same row (or of a row beside it over the same stretch, with
  the same total) that starts where the vendor's does and has its length is the pair: code points
  the item at it, records its pick, and the item stays the same;
- with none, the item is Not compared with the reason ("the vendor's 24\" starts at 34\" along its
  row of sizes; the reference's 24\" starts at 10\"").

**Never a guess.** A label code has no line for, a row that is not one chain (two sizes start at
one joint, another size runs into it), rows whose totals differ, or a way round that is not known:
nothing is recorded and the item is as before. This only ever moves a Right to another piece of the
same row, or to Not compared: never red.

**The rows** (`rows_of`). A row is the label's line and the sizes touching it end to end on that
line (`dim_ends.CHAIN_ROW_PT` across, `dim_ends.CHAIN_GAP_PT` along), one size at each joint, and
no other size on the line running into it; also each part of it between the two ends of a size
printed beside it (the run's overall) whose value is the sum of the pieces between its ends. A
label's line is its span (`dim_ends.Page.spans`), or the scale fit's line for it (`scale_fit`)
when that line fills a gap in the row exactly, its neighbours' views have one fit, and it is drawn
to its value at that fit (a dimension line split by its own text into two views).

Pure: the pages are passed in. Verification: `tests/review/assistant/test_row_places.py`
(made-up rows).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Final

from app.review.assistant import dim_ends, scale_fit
from app.review.assistant.ledger import format_inches

__all__ = [
    "FIELD",
    "PLACE_TOLERANCE",
    "Row",
    "elsewhere_reason",
    "judge",
    "lines_of",
    "repoint",
    "rows_of",
    "side_key",
]

#: The finding's record: {"outcome": "same_place" | "repointed" | "elsewhere", "vendor": key,
#: "architect": key, "was": key (repointed), "vendor_start", "reference_start", "why"}.
FIELD: Final = "row_place"
#: Two pieces start at the same place within this (inches), and are the same length within
#: `SAME_SIZE`; two rows have the same total within `SAME_SIZE`.
PLACE_TOLERANCE: Final = Fraction(1, 4)
SAME_SIZE: Final = Fraction(1, 16)
#: A row's pieces lie on one line within this across (points) and touch within `JOIN_PT` along.
ROW_PT: Final = dim_ends.CHAIN_ROW_PT
JOIN_PT: Final = dim_ends.CHAIN_GAP_PT
#: An overall's end and a row's joint, or two rows' ends, are one place within this (points).
END_PT: Final = dim_ends.SAME_PT
#: The most pieces a row is read with.
MAX_PIECES: Final = dim_ends.CHAIN_MAX_PARTS

type Span = dim_ends.Span
type Pages = Callable[[str, int], dim_ends.Page | None]


@dataclass(frozen=True, slots=True)
class Row:
    """Pieces of a row of sizes, end to end along one line, in drawn order (low end first)."""

    pieces: tuple[Span, ...]

    @property
    def total(self) -> Fraction:
        return sum((piece.inches for piece in self.pieces), Fraction(0))

    @property
    def a(self) -> float:
        return self.pieces[0].a

    @property
    def b(self) -> float:
        return self.pieces[-1].b

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(piece.id for piece in self.pieces)

    def start(self, piece_id: str) -> Fraction:
        """The printed sizes before the piece along the row, added up (from the low end)."""
        before = Fraction(0)
        for piece in self.pieces:
            if piece.id == piece_id:
                return before
            before += piece.inches
        raise KeyError(piece_id)

    def joints(self) -> list[Fraction]:
        """Where each piece meets the next, from the low end."""
        out: list[Fraction] = []
        place = Fraction(0)
        for piece in self.pieces[:-1]:
            place += piece.inches
            out.append(place)
        return out


# ---- the lines -----------------------------------------------------------------------------------


def _agrees(drawn: scale_fit.Drawn, in_per_pt: float) -> bool:
    """`scale_fit`'s check of a value against its drawn length at a fit."""
    length = drawn.length * in_per_pt
    allowed = max(
        scale_fit.AGREE_IN,
        scale_fit.AGREE_SHARE * float(drawn.inches),
        scale_fit.AGREE_PT * in_per_pt,
    )
    return abs(length - float(drawn.inches)) <= allowed


def _fills_gap(page: dim_ends.Page, spans: Mapping[str, Span], drawn: scale_fit.Drawn) -> bool:
    """The scale fit's line for a label without a span fills a gap in a row exactly: a piece with a
    span touches it at one end or both (one at each end at most), each such neighbour's value
    agrees with its drawn length at its view's fit, those fits are one scale, and this line is drawn
    to its value at it."""
    fit = page.fit
    side = fit.sides.get(drawn.id)
    if side is None:
        return False
    on_line = [
        s for s in spans.values() if s.axis == drawn.axis and abs(s.row - drawn.row) <= ROW_PT
    ]
    before = [s for s in on_line if abs(s.b - drawn.a) <= JOIN_PT]
    after = [s for s in on_line if abs(s.a - drawn.b) <= JOIN_PT]
    if len(before) > 1 or len(after) > 1 or not (before or after):
        return False
    if any(min(s.b, drawn.b) - max(s.a, drawn.a) > JOIN_PT for s in on_line):
        return False  # another size on the line runs into it
    scales: list[float] = []
    for neighbour in before + after:
        check = fit.check(neighbour.id)
        view = fit.views.get(neighbour.id)
        if check is None or not check.agrees or view is None or fit.sides.get(neighbour.id) != side:
            return False
        found = fit.fit_at(side, view, drawn.axis)
        if found is None:
            return False
        scales.append(found[0].in_per_pt)
    if any(abs(s / scales[0] - 1) > scale_fit.AGREE for s in scales[1:]):
        return False
    return _agrees(drawn, scales[0])


def lines_of(page: dim_ends.Page) -> dict[str, Span]:
    """The page's labels with a line code trusts: their spans, and the scale fit's lines that fill
    a gap in a row exactly (`_fills_gap`)."""
    spans: dict[str, Span] = dict(page.spans)
    fit = page.fit
    if fit is None:
        return spans
    added: dict[str, Span] = {}
    for key, drawn in fit.spans.items():
        if key in spans or not drawn.inches or not _fills_gap(page, spans, drawn):
            continue
        added[key] = dim_ends.Span(key, drawn.axis, drawn.row, drawn.a, drawn.b, drawn.inches)
    spans.update(added)
    return spans


# ---- the rows ------------------------------------------------------------------------------------


def _chain(spans: Mapping[str, Span], piece: Span) -> tuple[Span, ...] | None:
    """The piece and the sizes touching it end to end on its line, in drawn order; `None` when a
    joint has two sizes, another size on the line runs into the chain, or it has one piece."""
    line = [s for s in spans.values() if s.axis == piece.axis and abs(s.row - piece.row) <= ROW_PT]
    parts = [piece]
    for forward in (True, False):
        current = piece
        while len(parts) <= MAX_PIECES:
            joined = [
                other
                for other in line
                if all(other.id != part.id for part in parts)
                and abs((other.a - current.b) if forward else (current.a - other.b)) <= JOIN_PT
            ]
            if not joined:
                break
            if len(joined) > 1:
                return None  # two sizes start at one joint: not one row
            current = joined[0]
            parts.append(current)
    if not 2 <= len(parts) <= MAX_PIECES:
        return None
    ordered = tuple(sorted(parts, key=lambda part: part.a))
    start, end = ordered[0].a, ordered[-1].b
    for other in line:
        if any(other.id == part.id for part in ordered):
            continue
        if min(other.b, end) - max(other.a, start) > JOIN_PT:
            return None  # another size on the line runs into the row
    return ordered


def _strip_field_cuts(pieces: tuple[Span, ...], cuts: Any) -> tuple[Span, ...]:
    """A vendor's field-cut piece code proved (`field_cut.page_field_cuts`) left out at the row's
    ends: GV's settled extra stone, not a piece of the counter."""
    labels = getattr(cuts, "labels", None) or frozenset()
    out = list(pieces)
    while out and out[0].id in labels:
        out.pop(0)
    while out and out[-1].id in labels:
        out.pop()
    return tuple(out)


def rows_of(spans: Mapping[str, Span], piece: Span, cuts: Any = None) -> list[Row]:
    """The rows of sizes the piece is a piece of (see the module): its chain, and each part of the
    chain between the ends of an overall printed beside it that adds up; a field-cut piece at a
    row's end left out. Empty when the piece is in no row code can measure."""
    chain = _chain(spans, piece)
    if chain is None:
        return []
    found: list[tuple[Span, ...]] = [chain]
    stations = [chain[0].a, *(part.b for part in chain)]
    for other in spans.values():
        if other.axis != piece.axis or any(other.id == part.id for part in chain):
            continue
        first = [i for i, at in enumerate(stations) if abs(at - other.a) <= END_PT]
        last = [i for i, at in enumerate(stations) if abs(at - other.b) <= END_PT]
        if len(first) != 1 or len(last) != 1 or last[0] - first[0] < 2:
            continue
        part = chain[first[0] : last[0]]
        if part == chain or all(p.id != piece.id for p in part):
            continue
        if abs(sum((p.inches for p in part), Fraction(0)) - other.inches) <= SAME_SIZE:
            found.append(part)
    rows: list[Row] = []
    for pieces in found:
        kept = _strip_field_cuts(pieces, cuts)
        ids = tuple(p.id for p in kept)
        if len(kept) >= 2 and piece.id in ids and all(row.ids != ids for row in rows):
            rows.append(Row(kept))
    return rows


def _beside(spans: Mapping[str, Span], row: Row, cuts: Any) -> list[Row]:
    """`row` and the other rows on the page over the same stretch: the same way, ends within
    `END_PT` of its ends, the same total (a stretch dimensioned twice, by two rows of sizes)."""
    out = [row]
    for other in spans.values():
        if other.axis != row.pieces[0].axis or other.id in row.ids:
            continue
        for found in rows_of(spans, other, cuts):
            if (
                abs(found.a - row.a) <= END_PT
                and abs(found.b - row.b) <= END_PT
                and abs(found.total - row.total) <= SAME_SIZE
                and all(found.ids != kept.ids for kept in out)
            ):
                out.append(found)
    return out


# ---- the judgement -------------------------------------------------------------------------------


def _shared(joints: Sequence[Fraction], others: Sequence[Fraction]) -> int:
    return sum(1 for j in joints if any(abs(j - o) <= PLACE_TOLERANCE for o in others))


def _place(
    vendor_row: Row,
    vendor: Span,
    reference_rows: Sequence[Row],
    reference: Span,
) -> tuple[str, str, Fraction, Fraction] | None:
    """(outcome, the reference piece's id, the vendor's start, the AI's reference piece's start),
    the reference rows read from their low end and the vendor's row the way round its joints say;
    `None` when the way round is not known."""
    total = vendor_row.total
    joints = vendor_row.joints()
    theirs = sorted({j for row in reference_rows for j in row.joints()})
    same, mirrored = _shared(joints, theirs), _shared([total - j for j in joints], theirs)
    if same == mirrored:
        return None  # which way round the rows run is not known
    start = vendor_row.start(vendor.id)
    if mirrored > same:
        start = total - start - vendor.inches
    own = reference_rows[0]
    at = own.start(reference.id)
    if abs(at - start) <= PLACE_TOLERANCE:
        return ("same_place", reference.id, start, at)
    matches = [
        piece
        for row in reference_rows
        for piece in row.pieces
        if abs(row.start(piece.id) - start) <= PLACE_TOLERANCE
        and abs(piece.inches - vendor.inches) <= SAME_SIZE
    ]
    if matches:
        pick = min(matches, key=lambda p: (abs(p.row - reference.row), p.id))
        return ("repointed", pick.id, start, at)
    return ("elsewhere", reference.id, start, at)


def _key(side: Mapping[str, Any], label_id: object) -> str:
    return f"{side.get('file') or ''}|{side.get('page')}|{label_id}"


def side_key(finding: Mapping[str, Any], side: str) -> str | None:
    """The finding's label on `side` ("vendor" or "architect") as the record names it."""
    dims = finding.get("dims")
    label = dims.get(side) if isinstance(dims, Mapping) else None
    if not isinstance(label, Mapping) or not label.get("id"):
        return None
    return _key(label, label.get("id"))


def _fraction(value: object) -> Fraction | None:
    try:
        return Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _page(pages: Pages, side: Mapping[str, Any]) -> dim_ends.Page | None:
    try:
        return pages(str(side.get("file") or ""), int(side.get("page") or 0))
    except (TypeError, ValueError):
        return None


def judge(finding: Mapping[str, Any], pages: Pages) -> dict[str, Any] | None:
    """Where the item's two pieces start along their rows (see the module): the record, or `None`
    when code cannot measure it (the item stays as it is)."""
    dims = finding.get("dims")
    vendor_side = dims.get("vendor") if isinstance(dims, Mapping) else None
    reference_side = dims.get("architect") if isinstance(dims, Mapping) else None
    if not isinstance(vendor_side, Mapping) or not isinstance(reference_side, Mapping):
        return None
    if not vendor_side.get("id") or not reference_side.get("id"):
        return None
    vendor_page, reference_page = _page(pages, vendor_side), _page(pages, reference_side)
    if vendor_page is None or reference_page is None:
        return None
    vendor_lines, reference_lines = lines_of(vendor_page), lines_of(reference_page)
    vendor = vendor_lines.get(str(vendor_side["id"]))
    reference = reference_lines.get(str(reference_side["id"]))
    if vendor is None or reference is None or abs(vendor.inches - reference.inches) > SAME_SIZE:
        return None
    for side, line in ((vendor_side, vendor), (reference_side, reference)):
        printed = _fraction(side.get("inches"))
        if printed is None or printed != line.inches:
            return None  # the finding's value is not the line's: not this rule's item
    vendor_rows = rows_of(vendor_lines, vendor, vendor_page.field_cuts)
    reference_rows = rows_of(reference_lines, reference, reference_page.field_cuts)
    found: set[tuple[str, str, Fraction, Fraction]] = set()
    for vendor_row in vendor_rows:
        for reference_row in reference_rows:
            if abs(vendor_row.total - reference_row.total) > SAME_SIZE:
                continue
            beside = _beside(reference_lines, reference_row, reference_page.field_cuts)
            placed = _place(vendor_row, vendor, beside, reference)
            if placed is None:
                return None  # the way round is not known: never a guess
            found.add(placed)
    if len(found) != 1:
        return None  # no rows with the same total, or rows that disagree
    outcome, pick, start, at = next(iter(found))
    size = format_inches(vendor.inches)
    record: dict[str, Any] = {
        "outcome": outcome,
        "vendor": _key(vendor_side, vendor.id),
        "architect": _key(reference_side, pick),
        "vendor_start": format_inches(start),
        "reference_start": format_inches(at),
    }
    if outcome == "same_place":
        record["why"] = f"both {size} pieces start {format_inches(start)} along their rows of sizes"
    elif outcome == "repointed":
        record["was"] = _key(reference_side, reference.id)
        record["why"] = (
            f"code paired the vendor's {size} with the reference's {size} that starts where it "
            f"does ({format_inches(start)} along the row of sizes); the AI's pick starts at "
            f"{format_inches(at)}"
        )
    else:
        record["why"] = (
            f"the vendor's {size} starts at {format_inches(start)} along its row of sizes; the "
            f"reference's {size} starts at {format_inches(at)}"
        )
    return record


def elsewhere_reason(finding: Mapping[str, Any]) -> str | None:
    """The plain reason an item's two pieces are not the same piece of their rows, while the item
    still pairs the two labels the record was made for; `None` otherwise."""
    held = finding.get(FIELD)
    if not isinstance(held, Mapping) or held.get("outcome") != "elsewhere":
        return None
    for side in ("vendor", "architect"):
        if side_key(finding, side) != held.get(side):
            return None
    return str(held.get("why") or "") or None


def _provenance(label: Mapping[str, Any], file: object, page: object) -> dict[str, Any]:
    """A label as a finding's `dims` side holds it."""
    return {
        "id": str(label.get("id")),
        "page": int(str(page or 0)),
        "file": file,
        "text": str(label.get("text") or ""),
        "inches": str(label.get("inches")),
        "route": "drawn" if label.get("route") == "drawn" else "text",
        "side": label.get("side"),
        "orientation": label.get("orientation"),
        "box": [float(v) for v in label.get("box") or (0, 0, 0, 0)],
        **({"origin": label["origin"]} if label.get("origin") else {}),
    }


def repoint(finding: dict[str, Any], label: Mapping[str, Any]) -> None:
    """The item compared with code's pick on the reference (`judge`'s `repointed`): its reference
    side, box and printed text become the label's, and code's reading of it its check. The value is
    the same (`judge` only picks a piece of the same length)."""
    from app.review.assistant.dim_findings import label_checks

    dims = dict(finding.get("dims") or {})
    was = dims.get("architect") if isinstance(dims.get("architect"), Mapping) else {}
    file, page = (was or {}).get("file"), (was or {}).get("page")
    dims["architect"] = _provenance(label, file, page)
    finding["dims"] = dims
    x0, y0, x1, y1 = (float(v) for v in label.get("box") or (0, 0, 0, 0))
    finding["arch_box"] = {"page": int(str(page or 0)), "x0": x0, "y0": y0, "x1": x1, "y1": y1}
    finding["arch_box"]["file"] = file
    finding["architect"] = str(label.get("text") or "")
    finding["arch_picture_id"] = None  # the old crop showed the AI's pick
    checks = {
        key: value
        for key, value in dict(finding.get("checks") or {}).items()
        if key not in ("verify_architect", "printed_architect")
    }
    finding["checks"] = label_checks(finding, checks)
