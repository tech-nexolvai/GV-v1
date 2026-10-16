"""What nobody answered at sign-off, printed "Not confirmed by a person" (owner decision 2026-10-16).

"It should be according to the reviewer: if he wants to add, he can; otherwise it just runs." Every
reviewer question is optional, so a review can be signed with questions still open. Nothing open
is ever dropped or treated as resolved: each one is printed on the draft and on the final cloud PDF.

* **The list** is the review screen's own (`app/review/review_items.review_items_for_revision`):
  every item and every FAIL still `open`, plus anything the list could not place (`unaccounted`),
  so the screen, the draft and the signed file name the same questions.
* **On the drawing** (`workflow/redline_outputs.left_for_person_marks`) each open item with a recorded place gets a grey mark there: "Not confirmed by
  a person — <what> (AI: <best guess or reason>)". One with no recorded place is listed with why.
  A FAIL keeps its own FAIL cloud; it is only listed (as not agreed by a person), never greyed.
* **On the cover** "Left for a person (N)", grouped by kind, each line with its words.
* **Frozen at sign-off** in the snapshot (format 5, `app/review/signed_exports.py`), so the signed
  file never reads the live database. A list that cannot be read is frozen as one line saying why,
  never silently empty.

Verification: `tests/review/test_optional_questions.py`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.orm import Session

from app.models.package import Package, PackageRevision

__all__ = [
    "KIND_WORDS",
    "NOT_CONFIRMED",
    "FrozenLeftForPerson",
    "FrozenLeftItem",
    "cover_groups",
    "left_for_person",
]

#: How every such line begins, on the drawing and on the cover.
NOT_CONFIRMED: Final = "Not confirmed by a person"

#: Each kind of question in plain words, in the order the cover lists them.
KIND_WORDS: Final[dict[str, str]] = {
    "number": "Numbers to confirm",
    "depth_reading": "Depth and sink readings to confirm",
    "countertop_by_hand": "Countertops to check by hand",
    "wall_choice": "Wall choices",
    "no_countertop_page": "Pages to decide",
    "architect": "Architect questions",
    "cabinet_kinds": "Cabinet marks",
    "cabinet_run": "Cabinet runs",
    "other_result": "Other results",
    "failure": "Clouded as wrong, not agreed by a person",
    "unaccounted": "Results the review list could not place",
    "unreadable": "The list could not be read",
}

#: Why a question has no grey mark on the drawing.
_NO_PLACE: Final = "no place on the drawing was recorded for it"


class FrozenLeftItem(BaseModel):
    """One question nobody answered, exactly as the cloud PDF prints it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    kind_words: str
    what: str
    page: int | None = None
    words: str
    """ "Not confirmed by a person — <what> (AI: <best guess or reason>)"."""
    place: str | None = None
    """A code-recorded page and polygon, in the JSON shape a finding's evidence carries."""
    unplaced_reason: str | None = None

    @model_validator(mode="after")
    def placed_or_explained(self) -> FrozenLeftItem:
        if (self.place is None) == (self.unplaced_reason is None):
            raise ValueError("an item has either a recorded place or the reason it has none")
        if not self.words.startswith(NOT_CONFIRMED):
            raise ValueError(f"an item left for a person is worded '{NOT_CONFIRMED} ...'")
        return self


class FrozenLeftForPerson(BaseModel):
    """The sign-off snapshot's section (format 5): every question nobody answered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[FrozenLeftItem, ...]


def _words(what: str, ai: str) -> str:
    ai = " ".join(ai.split()).rstrip(".") or "no reading"
    return f"{NOT_CONFIRMED} — {what} (AI: {ai})"


def _place(location: object | None) -> tuple[str | None, str | None]:
    """The item's recorded place as a finding's evidence reference, or why there is none."""
    if location is None:
        return None, _NO_PLACE
    version = getattr(location, "document_version_id", None)
    number = getattr(location, "page_number", None)
    polygon = getattr(location, "polygon", None)
    if version is None or not isinstance(number, int) or number < 1 or not polygon:
        return None, "its recorded place on the drawing is incomplete"
    return (
        json.dumps(
            {
                "document_version_id": str(version),
                "page": number - 1,
                "polygon": [[str(x), str(y)] for x, y in polygon],
                "space": getattr(location, "coordinate_space", "stored") or "stored",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        None,
    )


def _item(
    kind: str, what: str, page: int | None, ai: str, location: object | None
) -> FrozenLeftItem:
    place, reason = _place(location)
    return FrozenLeftItem(
        kind=kind,
        kind_words=KIND_WORDS.get(kind, KIND_WORDS["other_result"]),
        what=what,
        page=page,
        words=_words(what, ai),
        place=place,
        unplaced_reason=reason,
    )


def _on_page(what: str, page: int | None) -> str:
    return what if page is None or f"page {page}" in what.lower() else f"{what}, page {page}"


def left_for_person(
    db: Session, revision: PackageRevision, package: Package
) -> FrozenLeftForPerson:
    """Every question on the review list still open, as the cloud PDF prints it. Read-only."""
    from app.review.review_items import review_items_for_revision

    try:
        listed = review_items_for_revision(
            db, project_id=package.project_id, package_id=package.id, revision=revision
        )
    except Exception as error:  # noqa: BLE001 - frozen as a line saying why, never silent
        return FrozenLeftForPerson(
            items=(
                _item(
                    "unreadable",
                    "The questions left for a person",
                    None,
                    f"the review list could not be read: {type(error).__name__}: {error}",
                    None,
                ),
            )
        )
    items: list[FrozenLeftItem] = []
    for entry in listed.items:
        if entry.status != "open":
            continue
        what = entry.what
        if entry.countertop is not None and entry.countertop.label not in what:
            what = f"{what}, {entry.countertop.label}"
        ai = entry.best_guess.text if entry.best_guess is not None else entry.why
        items.append(
            _item(entry.kind, _on_page(what, entry.page), entry.page, ai, entry.vendor_ref)
        )
    for failure in listed.failures:
        if failure.status != "open":
            continue
        what = failure.rule_id + (f", {failure.row_label}" if failure.row_label else "")
        # A FAIL keeps its own FAIL cloud: listed, never greyed (no mark is drawn for it).
        items.append(
            _item("failure", _on_page(what, failure.page), failure.page, failure.why, None)
        )
    for missing in listed.unaccounted:
        what = "A result" if missing.kind == "finding" else "A page"
        items.append(
            _item("unaccounted", _on_page(what, missing.page), missing.page, missing.why, None)
        )
    return FrozenLeftForPerson(items=tuple(items))


def cover_groups(
    frozen: FrozenLeftForPerson | Sequence[FrozenLeftItem],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The cover's "Left for a person" section: each kind once, in `KIND_WORDS` order, with the
    words of every question of that kind."""
    items = frozen.items if isinstance(frozen, FrozenLeftForPerson) else tuple(frozen)
    order = list(KIND_WORDS.values())
    groups: dict[str, list[str]] = {}
    for item in items:
        groups.setdefault(item.kind_words, []).append(item.words)
    return tuple(
        (words, tuple(lines))
        for words, lines in sorted(
            groups.items(), key=lambda pair: order.index(pair[0]) if pair[0] in order else 99
        )
    )
