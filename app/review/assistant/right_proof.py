"""A Right needs code's proof that both dimensions measure the same thing: code, $0, no AI
(2026-10-27).

A red already needs code to confirm a vendor size and a reference size end at the same places
(`assistant_pairing_proof = code_ends`). A Right had no such bar: two equal numbers that measure
different things were a green tick. A by-eye check of a real upload found two: the AI paired the
vendor's 4" band at the counter's edge with the reference's 4" from the floor to an outlet (made-up
values here). Both say 4", so the tick was true only by luck.

**The rule.** An item called the same that pairs a vendor label with a reference label is Right
only when code proves the pairing, by one of:

1. **Code's geometry** (the red's proofs, `ends_proof`, in its order): the drawings lined up on
   sizes printed once on each, and the two lines' ends land together (`same_extent`); the drawn
   run ties both ends on both drawings (`same_ends`); both ends of both on the same kinds of points
   (`dim_ends.same_kinds`).
2. **The only such size on both drawings**: the vendor's drawing prints that size, running the
   same way, on one dimension only, and so does the reference's (the same size printed again at
   the same place along its run, as a plan above its elevation does, is one dimension).

Where code's geometry shows the two do not end at the same places (the drawings lined up and the
ends land apart, or the drawn run ties an end to another place), the item is Not compared:
"different reference points". Otherwise, with no proof, it is Not compared: "could not confirm
both measure the same thing", with what code saw. Never red, never a Likely problem.

Two geometry answers are not trusted to take a Right away, only to give one: "one is a width and
the other a height" (a plan is often drawn turned on one of the drawings) and the ends' kinds
(an edge named a cabinet's side on one drawing and the top's edge on the other, on real tops that
run wall to wall on both). They leave the item to the second proof.

Items code compared itself are left as they are: GV's formula or field cut, code's own pair, the
sink-centre check, a row piece code placed (`row_places`), ends code proved alike
(`dim_ends.right_ends`), and any item the reviewer decided.

The record is made by the escalation's free step (`escalation._mark_right_proof`) for the two
labels the item pairs then; `classification` reads it. Saved answers made before keep their
outcome. Pure: the labels and pages are passed in. Verification:
`tests/review/assistant/test_right_proof.py` (made-up drawings).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from typing import Any, Final, Literal

from app.review.assistant import dim_ends, ends_proof, row_places, same_ends
from app.review.assistant.full_check_advocate import same_extent

__all__ = [
    "FIELD",
    "judge",
    "needs_proof",
    "only_size",
    "unproved",
]

_log = logging.getLogger(__name__)

#: The finding's record: {"proved": bool, "how": "geometry" | "only_size" | "different" | "none",
#: "why": plain words, "vendor": label key, "architect": label key}.
FIELD: Final = "right_proof"
#: Two printed values this close are the same size (a sixteenth, the drawings' step).
SAME_SIZE: Final = Fraction(1, 16)
#: Two labels of the same size whose lines' ends (or, with no line, whose middles) lie this close
#: along their axis (points) are the same dimension printed again (a plan above its elevation).
COPY_PT: Final = 2.0
#: The geometry's words for two dimensions running different ways: not trusted here (a plan is
#: often drawn turned on one of the drawings).
_TURNED: Final = "one is a width and the other a height"

type Labels = Callable[[str, int], Sequence[Mapping[str, Any]]]
type Pages = Callable[[str, int], dim_ends.Page | None]
type How = Literal["geometry", "only_size", "different", "none"]


def _fraction(value: object) -> Fraction | None:
    try:
        return Fraction(str(value)).limit_denominator(64)
    except (ValueError, ZeroDivisionError):
        return None


def needs_proof(finding: Mapping[str, Any]) -> bool:
    """An item called the same that pairs two labels the AI chose, and nothing code compared
    itself (see the module)."""
    if finding.get("status") != "same":
        return False
    dims = finding.get("dims")
    if not isinstance(dims, Mapping):
        return False
    vendor, reference = dims.get("vendor"), dims.get("architect")
    if not isinstance(vendor, Mapping) or not isinstance(reference, Mapping):
        return False
    if not vendor.get("id") or not reference.get("id"):
        return False
    if finding.get("paired_by") == "code" or finding.get("formula") or finding.get("field_cut"):
        return False  # code's own pair, GV's formula or the field cut compared it
    if finding.get("created_by") == "sink_centre":
        return False  # code's sink-centre check compared it
    decision = finding.get("decision")
    if isinstance(decision, Mapping) and decision.get("decision"):
        return False  # the reviewer decided it on the pair it shows
    placed = finding.get(row_places.FIELD)
    if (
        isinstance(placed, Mapping)
        and placed.get("outcome") in ("same_place", "repointed")
        and placed.get("vendor") == row_places.side_key(finding, "vendor")
        and placed.get("architect") == row_places.side_key(finding, "architect")
    ):
        return False  # code placed both pieces at the same start along their rows
    alike = finding.get(dim_ends.LIKE_FOR_LIKE)
    return not (
        isinstance(alike, Mapping)
        and alike.get("proved") is True
        and alike.get("vendor") == dim_ends._label_key(vendor)
        and alike.get("architect") == dim_ends._label_key(reference)
    )


def _full(labels: Labels, side: Mapping[str, Any]) -> dict[str, Any] | None:
    """The label as the page's reading holds it, with its file and page."""
    file, page = str(side.get("file") or ""), int(side.get("page") or 0)
    for label in labels(file, page) or ():
        if str(label.get("id")) == str(side.get("id")):
            return {**dict(label), "file": file, "page": page}
    return None


def _along(label: Mapping[str, Any], axis: int) -> tuple[float, float] | None:
    """Where the label's dimension lies along its axis: its line's two ends, or (no line) its
    text's middle twice."""
    line = label.get("dim_line")
    if isinstance(line, Sequence) and len(line) == 2:
        try:
            a, b = float(line[0][axis]), float(line[1][axis])
        except (TypeError, ValueError, IndexError):
            pass  # no line's ends: the text's middle
        else:
            return (min(a, b), max(a, b))
    box = label.get("box")
    if isinstance(box, Sequence) and len(box) == 4:
        try:
            middle = (float(box[axis]) + float(box[axis + 2])) / 2
        except (TypeError, ValueError):
            return None
        return (middle, middle)
    return None


def _copies(one: tuple[float, float], other: tuple[float, float], lines: bool) -> bool:
    if lines:
        return abs(one[0] - other[0]) <= COPY_PT and abs(one[1] - other[1]) <= COPY_PT
    return abs(sum(one) / 2 - sum(other) / 2) <= COPY_PT


def only_size(label: Mapping[str, Any], labels: Sequence[Mapping[str, Any]]) -> int:
    """How many dimensions of the label's drawing (its page's labels on its side) print its size
    and run its way (or a way code does not know), the same size printed again at the same place
    along its run counted once."""
    value = _fraction(label.get("inches"))
    if value is None:
        return 0
    way = label.get("orientation")
    axis = 1 if way == "v" else 0
    groups: list[list[tuple[tuple[float, float], bool]]] = []
    for other in labels:
        if other.get("side") != label.get("side"):
            continue
        size = _fraction(other.get("inches"))
        if size is None or abs(size - value) > SAME_SIZE:
            continue
        if way and other.get("orientation") and other.get("orientation") != way:
            continue
        place = _along(other, axis)
        if place is None:
            groups.append([])  # no place known: a dimension of its own
            continue
        has_line = bool(other.get("dim_line"))
        for group in groups:
            if any(_copies(place, seen, has_line and seen_line) for seen, seen_line in group):
                group.append((place, has_line))
                break
        else:
            groups.append([(place, has_line)])
    return len(groups)


def _geometry(
    vendor: Mapping[str, Any],
    reference: Mapping[str, Any],
    labels: Labels,
    pages: Pages | None,
    page_map: Mapping[str, Any] | None,
    cache: dict[Any, Any],
) -> tuple[How, str] | None:
    """Code's geometry for the pair, in the red's order (`ends_proof.code_extent`): ("geometry",
    why) when it proves the same ends, ("different", why) when it shows other ends, `None` when it
    cannot tell (or only says what is not trusted here, see the module)."""

    def side_labels(label: Mapping[str, Any], role: str) -> list[dict[str, Any]]:
        file, page = str(label.get("file")), int(label.get("page") or 0)
        return [
            {**dict(item), "file": file, "page": page}
            for item in labels(file, page) or ()
            if item.get("side") == role
        ]

    state, why = "cannot_tell", ""
    if vendor.get("orientation") == reference.get("orientation"):
        state, why = same_extent(
            vendor,
            reference,
            side_labels(vendor, "vendor"),
            side_labels(reference, str(reference.get("side") or "architect")),
        )
        if state == "different_extent":
            return (
                "different",
                f"the drawings line up and the two dimensions' ends land apart ({why})",
            )
    if ends_proof.drawn_disagreement(vendor, reference, pages) is not None:
        return None  # a line not drawn to its value proves nothing either way (as for a red)
    if state == "same_extent":
        return "geometry", f"the drawings line up and both dimensions' ends land together ({why})"

    def segments(file: str, page: int) -> Sequence[Any]:
        found = pages(file, page) if pages is not None else None
        return found.segments if found is not None else ()

    try:
        state, why = same_ends.prove_labels(
            vendor,
            reference,
            labels_of=labels,
            segments_of=segments,
            page_map=page_map,
            cache=cache,
        )
    except Exception:  # noqa: BLE001 - the drawn run is one proof; the others stand
        _log.exception("right proof: drawn run failed")
        state, why = "cannot_tell", ""
    if state == "same_extent":
        return "geometry", f"the drawn run: {why}"
    if state == "different_extent" and why != _TURNED:
        return "different", f"the drawn run: {why}"
    if pages is None:
        return None
    v_page = pages(str(vendor.get("file")), int(vendor.get("page") or 0))
    a_page = pages(str(reference.get("file")), int(reference.get("page") or 0))
    v_ends = v_page.ends(vendor.get("id")) if v_page is not None else None
    a_ends = a_page.ends(reference.get("id")) if a_page is not None else None
    if v_page is None or a_page is None or v_ends is None or a_ends is None:
        return None
    try:
        kinds, words = dim_ends.same_kinds(v_page, v_ends, a_page, a_ends)
    except Exception:  # noqa: BLE001 - the ends' kinds are one proof
        _log.exception("right proof: kinds proof failed")
        return None
    if kinds == "same_extent":
        return "geometry", f"the ends: {words}"
    return None  # `different_extent` here is not trusted to take a Right away (see the module)


def judge(
    finding: Mapping[str, Any],
    labels: Labels,
    pages: Pages | None = None,
    page_map: Mapping[str, Any] | None = None,
    cache: dict[Any, Any] | None = None,
) -> dict[str, Any] | None:
    """The record for a Right that needs proof (`needs_proof`), else `None` (see the module)."""
    if not needs_proof(finding):
        return None
    dims = finding["dims"]
    keys = {
        "vendor": dim_ends._label_key(dims["vendor"]),
        "architect": dim_ends._label_key(dims["architect"]),
    }
    vendor, reference = _full(labels, dims["vendor"]), _full(labels, dims["architect"])
    if vendor is None or reference is None:
        return {
            "proved": False,
            "how": "none",
            "why": "code did not find both sizes among the drawings' dimensions",
            **keys,
        }
    found = _geometry(vendor, reference, labels, pages, page_map, {} if cache is None else cache)
    if found is not None:
        how, why = found
        return {"proved": how == "geometry", "how": how, "why": why, **keys}
    counts = []
    for label in (vendor, reference):
        file, page = str(label["file"]), int(label["page"])
        counts.append(only_size(label, labels(file, page) or ()))
    if counts == [1, 1]:
        return {
            "proved": True,
            "how": "only_size",
            "why": "the only dimension of that size, running that way, on both drawings",
            **keys,
        }
    size = str(vendor.get("text") or vendor.get("inches"))
    words = []
    for count, whose in zip(counts, ("the vendor's", "the reference's"), strict=True):
        if count > 1:
            words.append(f"{whose} drawing prints {count} dimensions of that size")
    why = (
        "; ".join(words) + ", and code could not tell which one is the other's"
        if words
        else f"code could not place the {size} on both drawings"
    )
    return {"proved": False, "how": "none", "why": why, **keys}


def unproved(finding: Mapping[str, Any]) -> tuple[Literal["different", "none"], str] | None:
    """The item's record when it took the Right away, for the two labels the item still pairs:
    ("different", why) or ("none", why); `None` otherwise (no record, proved, or another pair)."""
    held = finding.get(FIELD)
    if not isinstance(held, Mapping) or held.get("proved") is not False:
        return None
    if finding.get("status") != "same":
        return None
    dims = finding.get("dims")
    if not isinstance(dims, Mapping):
        return None
    if dim_ends._label_key(dims.get("vendor")) != held.get("vendor"):
        return None
    if dim_ends._label_key(dims.get("architect")) != held.get("architect"):
        return None
    why = str(held.get("why") or "")
    return ("different", why) if held.get("how") == "different" else ("none", why)
