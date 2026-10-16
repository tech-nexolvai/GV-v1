"""A person's answer to a proposed cabinet run: which run, what each piece is, the site width.

The answer to the `cabinet_run` item (`app/review/review_items.py`). It is checked here against the
proposal as the list shows it now, and turned into exactly what `CAB-FILLER-001` reads: the vendor's
two fillers and its cabinets in wall order (the same RUN-layer lists Measurements stores), the
reviewer's mark for each cabinet (`workflow/classifications.py`), and the site wall-to-wall. The
API stores them in one transaction and queues one re-check (`app/api/review_items.py`).

**Confirm-only.** No width is typed: each comes from the sealed reading the proposal names, and
each stored value's reference names that reading. The person types only the site wall-to-wall,
which is not on any drawing (the client's sheet: F_Wall_2_Wall_Dim, "User Input"), in inches.

**The run shape the check compares**: one filler at each end and cabinets between
(`verdict/operations/distribution.py:_pair`). A run marked otherwise (no filler at an end, a filler
between cabinets) is refused with that reason, never stored as a shape the check would abstain on.

Verification: `tests/api/test_review_item_cabinet_run.py`.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from fractions import Fraction
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.review.cabinet_run_proposal import CABINET_RULE, FILLER, SITE_WIDTH_INPUT
from units.imperial import ImperialParseError, format_inches, parse_imperial
from units.measurement import Measurement, Unit

__all__ = ["CabinetRunAnswer", "CabinetRunIn", "CabinetRunRefused", "cabinet_run_answer"]

#: The other cabinet check that reads the vendor's cabinets, position by position.
ARCH_VS_SHOP_RULE: Final = "CAB-ARCH-VS-SHOP-001"
SHOP_FILLERS: Final = "shop_fillers"
SHOP_CABINETS: Final = "shop_cabinets"
CABINET_TYPE: Final = "cabinet_type"
LIST_MARKER: Final = "#"


class CabinetRunIn(BaseModel):
    """`use_cabinet_run`: the run chosen, what each piece is, and the site wall-to-wall."""

    model_config = ConfigDict(extra="forbid")

    row_id: UUID = Field(description="The proposed run's `row_id`.")
    pieces: list[str] = Field(
        max_length=200,
        description=(
            "One answer per piece, left to right: `filler`, or a `cabinet_choices` category. 409 "
            "when the run's pieces changed."
        ),
    )
    site_width: str | None = Field(
        default=None,
        max_length=64,
        description=(
            'The site wall-to-wall, measured on site, in inches (e.g. `75 1/2"`). Required unless '
            "one is already saved (`cabinet_proposal.site_width`)."
        ),
    )


class CabinetRunRefused(Exception):
    """The answer is refused with a status code and a plain sentence; nothing is stored."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CabinetRunAnswer:
    values: dict[str, Measurement]
    """Stored keys (`rule:name` or `rule:name#i`) to exact inches."""
    typed: dict[str, str]
    references: dict[str, str]
    """Stored key to the reference recorded with it: the sealed reading it came from."""
    categories: tuple[str, ...]
    """The reviewer's mark for each cabinet, left to right."""
    summary: str


def _inches(value: Fraction) -> Measurement:
    return Measurement(value, Unit.INCH, None)


def _site_width(text: str) -> Fraction:
    cleaned = text.strip().lower()
    if "mm" in cleaned or "cm" in cleaned or "'" in cleaned:
        raise CabinetRunRefused(
            422, 'Enter the site wall-to-wall in inches, e.g. 75 1/2". Nothing was saved.'
        )
    cleaned = cleaned.removesuffix('"').removesuffix("in").strip()
    try:
        value = parse_imperial(cleaned)
    except (ImperialParseError, ValueError, ArithmeticError):
        raise CabinetRunRefused(
            422,
            f'"{text}" is not an inch width. Enter it like 75 1/2". Nothing was saved.',
        ) from None
    if value <= 0:
        raise CabinetRunRefused(422, "The site wall-to-wall must be above zero. Nothing was saved.")
    return value


def cabinet_run_answer(
    proposal: object,
    answer: CabinetRunIn | None,
    *,
    offered: Collection[str],
    declared: Collection[tuple[str, str]],
) -> CabinetRunAnswer:
    """What to store for this answer, checked against the proposal shown now.

    `proposal` is the item's `cabinet_proposal`; `offered` the cabinet categories it offers;
    `declared` every published `(rule id, input name)`, so nothing is stored that no check reads.
    """
    if answer is None:
        raise CabinetRunRefused(
            422, "Say which run, what each piece is, and the site width (`cabinet_run`)."
        )
    runs = list(getattr(proposal, "runs", None) or [])
    run = next((candidate for candidate in runs if candidate.row_id == answer.row_id), None)
    if run is None:
        raise CabinetRunRefused(
            409, "This run is no longer proposed. Nothing was saved. Reload the list."
        )
    if len(answer.pieces) != len(run.pieces):
        raise CabinetRunRefused(
            409,
            f"This run now has {len(run.pieces)} pieces, not {len(answer.pieces)}. Nothing was "
            "saved. Reload the list.",
        )
    allowed = {FILLER, *offered}
    for position, kind in enumerate(answer.pieces, start=1):
        if kind not in allowed:
            raise CabinetRunRefused(
                422,
                f"Piece {position}: {kind!r} is not one of {', '.join(sorted(allowed))}. Nothing "
                "was saved.",
            )
    fillers = [index for index, kind in enumerate(answer.pieces) if kind == FILLER]
    last = len(answer.pieces) - 1
    if fillers != [0, last] or last < 2:
        raise CabinetRunRefused(
            422,
            "The cabinet check compares a run with one filler at each end and cabinets between "
            "(the client's sheet: filler left, cabinets, filler right). This run is not marked "
            "that way, so check it by hand. Nothing was saved.",
        )
    saved = getattr(proposal, "site_width", None)
    if answer.site_width is None or not answer.site_width.strip():
        if saved is None:
            raise CabinetRunRefused(
                422, "Enter the site wall-to-wall (measured on site), in inches. Nothing was saved."
            )
        site: Fraction | None = None
    else:
        site = _site_width(answer.site_width)

    def width(index: int) -> Fraction:
        piece = run.pieces[index]
        return Fraction(int(piece.width.numerator), int(piece.width.denominator))

    def reference(index: int) -> str:
        return f"confirmed AI reading {run.pieces[index].candidate_id}"

    values: dict[str, Measurement] = {}
    typed: dict[str, str] = {}
    references: dict[str, str] = {}

    def put(key: str, value: Fraction, why: str) -> None:
        values[key] = _inches(value)
        typed[key] = f'{format_inches(value)}"'
        references[key] = why

    for slot, index in enumerate((0, last)):
        put(f"{CABINET_RULE}:{SHOP_FILLERS}{LIST_MARKER}{slot}", width(index), reference(index))
    cabinets = list(range(1, last))
    for rule in (CABINET_RULE, ARCH_VS_SHOP_RULE):
        if (rule, SHOP_CABINETS) not in declared:
            continue
        for slot, index in enumerate(cabinets):
            put(f"{rule}:{SHOP_CABINETS}{LIST_MARKER}{slot}", width(index), reference(index))
    if site is not None:
        put(
            f"{CABINET_RULE}:{SITE_WIDTH_INPUT}",
            site,
            "site wall-to-wall, entered on the review list",
        )
    categories = tuple(answer.pieces[index] for index in cabinets)
    summary = (
        f"Used the run on page {run.page}: 2 fillers and {len(cabinets)} cabinets, each width as "
        "two AIs read it"
        + ("" if site is None else f'; site wall-to-wall {format_inches(site)}"')
        + ". The cabinet check runs again with them."
    )
    return CabinetRunAnswer(values, typed, references, categories, summary)
