"""The simple full check's shared shapes: the work list, the compare call's answer and its outcome.

Every module of the simple full check reads these, so they live in one place (CONTRACT-SIMPLE,
2026-10-19). Pure data and pure checks: no PDF, no picture, no model call.

**The work list** (`WorkList`, made by `worklist.py`): one `WorkItem` per vendor drawing (its page,
its drawing's box, its round, its area, the products its words name), with the architect drawings
of the same area, the governing one first (a detail or the larger scale), and every page of the set
said to be covered or skipped with a code reason.

**The compare call's answer** (`CompareResult`, the contract's form): for each of the vendor
drawing's numbered labels, the architect label it pairs with (or none), what the two measure, where
each end lands, and a verdict; then the items code has no label for (a missing outlet, doors against
drawers, a tag, a number with no label); then the vendor labels the model could not place. Label ids
are code's (`p23.D17`, from the set pack's `dims.json`): the model never types a labelled value.
Boxes are PDF points on the named page (origin top-left, as the set pack's boxes).

**What the model fills in** (`Reply`, sent as the structured-output schema `REPLY_SCHEMA`) is the same
form, except that a box is given in pixels on one of the call's pictures (`vendor_image` +
`vendor_box_px`): Anthropic's guidance is to ask for absolute pixel boxes on a picture sized by
code, and code maps them to PDF points (`compare.py`), because the model sees pictures, not pages.

**Checked by code** (`check_result`): every vendor label of the drawing is accounted for (in a pair
or in `unaccounted_labels`), every label named is one of the item's, a verdict that pairs names an
architect label, and every item without a label has the box code needs to find it. A result with a
problem goes back to the model once (`compare.py`); still wrong, the item is `failed`, with the
reason, and is never dropped.

The JSON schema follows the structured-outputs limits (Anthropic docs, 2026-10): every object has
`additionalProperties: false` and lists every property as required; no numeric, length or array-size
constraints; at most 16 nullable (union) fields. Enum case is not guaranteed by the API, so the
models lower-case verdicts and kinds before checking them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "END_WORDS",
    "OTHER_KINDS",
    "OTHER_VERDICTS",
    "REPLY_SCHEMA",
    "SCHEMA_VERSION",
    "VERDICTS",
    "Box",
    "CompareOutcome",
    "CompareResult",
    "Drawing",
    "Label",
    "OtherItem",
    "PageCover",
    "Pair",
    "Picture",
    "Reply",
    "ReplyItem",
    "Skipped",
    "WorkItem",
    "WorkList",
    "canonical_ref",
    "check_result",
    "end_word",
    "label_ref",
    "parse_label_ref",
]

#: Changes when a shape below changes in a way a stored result cannot be read with.
SCHEMA_VERSION: Final = 1

type Box = tuple[float, float, float, float]
type Verdict = Literal["same", "different", "no_architect_value", "different_points", "unsure"]
type OtherVerdict = Literal["same", "different", "no_architect_value", "unsure"]
type OtherKind = Literal[
    "layout_count",
    "tag",
    "note",
    "missing_on_vendor",
    "added_on_vendor",
    "dimension_without_label",
]
type Role = Literal["vendor", "architect", "brand_prototype"]

VERDICTS: Final[tuple[str, ...]] = (
    "same",
    "different",
    "no_architect_value",
    "different_points",
    "unsure",
)
OTHER_VERDICTS: Final[tuple[str, ...]] = ("same", "different", "no_architect_value", "unsure")
OTHER_KINDS: Final[tuple[str, ...]] = (
    "layout_count",
    "tag",
    "note",
    "missing_on_vendor",
    "added_on_vendor",
    "dimension_without_label",
)
#: Verdicts that say two labels were compared, so they need an architect label.
_PAIRED: Final = frozenset({"same", "different", "different_points"})

#: The words a dimension's end is written with ("centre_line (fridge outlet)"): the prompt asks for
#: one of them first, so code can compare ends without reading prose (`end_word`).
END_WORDS: Final[tuple[str, ...]] = (
    "floor",
    "top_of_counter",
    "underside_of_counter",
    "top_of_splash",
    "underside_of_upper",
    "top_of_unit",
    "wall_face",
    "cabinet_side",
    "cabinet_end",
    "centre_line",
    "ceiling_soffit",
    "box_above_unit",
    "blocking",
    "opening_edge",
    "other",
)
_END_ALIASES: Final[Mapping[str, str]] = {
    "center_line": "centre_line",
    "centerline": "centre_line",
    "centreline": "centre_line",
    "cl": "centre_line",
    "ceiling": "ceiling_soffit",
    "soffit": "ceiling_soffit",
    "bulkhead": "ceiling_soffit",
    "wall": "wall_face",
    "aff": "floor",
}

_REF: Final = re.compile(r"^p([1-9]\d{0,3})\.D([1-9]\d{0,3})$")
_LOOSE_REF: Final = re.compile(
    r"^\s*(?:p(?:age)?\.?\s*(\d{1,4})\s*[.:\s]\s*)?D\s*(\d{1,4})\s*$", re.IGNORECASE
)
#: How many missing ids one problem line names before "and N more".
_SHOWN_IDS: Final = 25


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _lower(value: object) -> object:
    """Enum words as the API may case them ("Same", "DIFFERENT"): lower case, spaces as `_`."""
    if isinstance(value, str):
        return "_".join(value.strip().lower().replace("-", " ").split())
    return value


# ---- label ids ---------------------------------------------------------------------------------


def label_ref(page: int, label_id: str) -> str:
    """A label's id across the set: page 23's `D17` is `p23.D17`."""
    return f"p{int(page)}.{label_id}"


def parse_label_ref(ref: str) -> tuple[int, str]:
    """`p23.D17` -> (23, "D17"). Raises `ValueError` for anything else."""
    found = _REF.match(ref)
    if found is None:
        raise ValueError(f"{ref!r} is not a label id like p23.D17")
    return int(found.group(1)), f"D{found.group(2)}"


def canonical_ref(text: object, page: int | None = None) -> str | None:
    """A label id as a model may write it ("p23.D17", "P23 D17", "p.23:D17", or "D17" when `page`
    says which page) in code's form, or `None` when it is not one."""
    if not isinstance(text, str):
        return None
    found = _LOOSE_REF.match(text)
    if found is None:
        return None
    named = int(found.group(1)) if found.group(1) else page
    if named is None or named < 1 or int(found.group(2)) < 1:
        return None
    return f"p{named}.D{int(found.group(2))}"


def end_word(text: object) -> str | None:
    """The end word a `*_from` / `*_to` field starts with ("centre_line (fridge outlet)" ->
    `centre_line`), or `None` when it starts with none of `END_WORDS`."""
    if not isinstance(text, str):
        return None
    plain = "_".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())
    for word in sorted(END_WORDS, key=len, reverse=True):
        if plain == word or plain.startswith(word + "_"):
            return word
    for alias, word in _END_ALIASES.items():
        if plain == alias or plain.startswith(alias + "_"):
            return word
    return None


def _check_ref(value: object) -> object:
    if isinstance(value, str) and _REF.match(value) is None:
        raise ValueError(f"{value!r} is not a label id like p23.D17")
    return value


# ---- the work list -----------------------------------------------------------------------------


class Label(_Frozen):
    """One of code's numbered dimension labels (`dims.json`), as the work list carries it."""

    ref: str
    """`p23.D17`."""
    page: int
    id: str
    """`D17`: the page's own id."""
    text: str
    """The value as printed (`3' - 0"`, `254 [10]`)."""
    inches: str | None
    """The value in inches as code read it (a decimal), when it has one."""
    box: Box
    side: str
    """`vendor`, `architect`, `brand` or `unknown` (the set pack's word)."""
    orientation: str | None = None
    """`h`, `v` or `turned`."""
    route: str | None = None
    """`text` (the PDF's characters) or `drawn` (pen strokes read by two OCR engines)."""
    mm: str | None = None

    _ref = field_validator("ref")(_check_ref)


class Drawing(_Frozen):
    """One drawing pasted on a page (a page map `half`)."""

    ref: str
    """A vendor drawing: `p{page}.v{round}` (`v0`: round not known), with `.1`, `.2`... when one
    page has two vendor drawings of one round, in reading order (`p11.v1.2`). An architect drawing:
    `p23.a1` (the page's first); a brand prototype: `p28.b1`."""
    file: str
    page: int
    half: int
    """Its place in the page map's `halves` of that page (0-based)."""
    role: Role
    round: int | None = None
    heading: str | None = None
    box: Box
    """The drawing's box from the page map (PDF points, origin top-left)."""
    crop_box: Box
    """What its pictures show: the box, its labels and a small margin, kept on the page."""
    labels: tuple[Label, ...] = ()
    titles: tuple[str, ...] = ()
    """Printed view titles found on it ("KITCHENETTE ELEVATION")."""
    tags: tuple[str, ...] = ()
    """Printed tags (`notes.json`)."""
    notes: tuple[str, ...] = ()
    """Printed notes (`notes.json`), short."""
    scale_in_per_pt: float | None = None
    """Inches of the real thing per PDF point, measured from its own printed dimensions."""
    scale_note: str | None = None
    """The printed scale note beside it (`1/2" = 1'-0"`), when there is one."""
    detail: bool = False
    """Its title says detail, section or enlarged (governs over a plan or elevation, R5)."""


class WorkItem(_Frozen):
    """One vendor drawing to compare, with the architect drawings of its area."""

    id: str
    """The vendor drawing's ref (`p23.v1`, `p54.v2.1`): the same id every builder uses."""
    file: str
    page: int
    section: str | None = None
    area: str | None = None
    vendor: Drawing
    architect: tuple[Drawing, ...]
    """The area's architect drawings, the governing one first: a detail, then the larger scale
    (R5); the same page before others on a tie."""
    reference: tuple[Drawing, ...] = ()
    """The area's brand prototype drawings: reference only unless the architect points to them
    (R1). Listed, not sent."""
    round: int | None = None
    newest_round: bool = True
    """No later round of the same area exists in the set."""
    earlier_rounds: tuple[str, ...] = ()
    later_rounds: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    """Registry product ids the drawings' printed words name (`product_scope.named_products`,
    hints included); empty when they name none."""
    architect_only_tags: tuple[str, ...] = ()
    """Tags printed on the area's architect drawing and not on its vendor drawing (`notes.json`)."""
    hard: tuple[str, ...] = ()
    """Why this item goes to the stronger model (empty: the default model)."""

    @property
    def vendor_refs(self) -> tuple[str, ...]:
        return tuple(label.ref for label in self.vendor.labels)

    @property
    def architect_refs(self) -> tuple[str, ...]:
        return tuple(label.ref for drawing in self.architect for label in drawing.labels)

    @property
    def architect_pages(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(drawing.page for drawing in self.architect))


class Skipped(_Frozen):
    """A page (or one drawing on it) code does not send to the model, with code's reason."""

    file: str
    page: int
    drawing: str | None = None
    reason: str


class PageCover(_Frozen):
    """How one page of the set is covered."""

    page: int
    how: Literal["vendor", "architect", "reference", "skipped"]
    """`vendor`: an item compares a vendor drawing on it; `architect`: it is only looked at as an
    item's architect drawing; `reference`: only a brand prototype of an item's area; `skipped`."""
    items: tuple[str, ...] = ()
    reason: str | None = None


class WorkList(_Frozen):
    version: int = SCHEMA_VERSION
    file: str
    items: tuple[WorkItem, ...]
    skipped: tuple[Skipped, ...]
    pages: tuple[PageCover, ...]
    """Every page of the file, in order."""
    uncovered: tuple[int, ...] = ()
    """Pages with a drawing that no item covers and no skip explains (always empty when the
    work list is right; a test holds it so)."""


# ---- the compare call's answer -----------------------------------------------------------------


class Pair(_Frozen):
    """One vendor label and the architect label that measures the same thing (or none)."""

    vendor_label: str
    architect_label: str | None
    measures: str
    """What the vendor dimension measures, in a few words."""
    vendor_from: str
    vendor_to: str
    architect_from: str
    architect_to: str
    verdict: Verdict
    note: str

    _refs = field_validator("vendor_label", "architect_label")(_check_ref)
    _verdict = field_validator("verdict", mode="before")(_lower)


class OtherItem(_Frozen):
    """Something to compare that code has no label for (boxes in PDF points)."""

    kind: OtherKind
    what: str
    vendor: str | None
    architect: str | None
    vendor_box: Box | None
    """On the work item's vendor page."""
    architect_box: Box | None
    architect_page: int | None
    verdict: OtherVerdict
    millwork: bool
    """False for room features (ceilings, soffits, doors, artwork, switches, lights)."""

    _kind = field_validator("kind", "verdict", mode="before")(_lower)


class CompareResult(_Frozen):
    """The contract's form: one per work item."""

    work_item: str
    pairs: tuple[Pair, ...]
    other_items: tuple[OtherItem, ...]
    unaccounted_labels: tuple[str, ...]

    _refs = field_validator("unaccounted_labels")(
        lambda values: tuple(_check_ref(v) for v in values)
    )


class ReplyItem(_Frozen):
    """An `OtherItem` as the model writes it: boxes in pixels on one of the call's pictures."""

    kind: OtherKind
    what: str
    vendor: str | None
    architect: str | None
    vendor_image: int | None
    vendor_box_px: tuple[float, ...] | None
    architect_image: int | None
    architect_box_px: tuple[float, ...] | None
    verdict: OtherVerdict
    millwork: bool

    _kind = field_validator("kind", "verdict", mode="before")(_lower)


class ReplyPair(_Frozen):
    """A `Pair` as the model writes it: its label ids are put in code's form after."""

    vendor_label: str
    architect_label: str | None
    measures: str
    vendor_from: str
    vendor_to: str
    architect_from: str
    architect_to: str
    verdict: Verdict
    note: str

    _verdict = field_validator("verdict", mode="before")(_lower)


class Reply(_Frozen):
    """The model's structured answer (`REPLY_SCHEMA`), before code maps its boxes."""

    work_item: str
    pairs: tuple[ReplyPair, ...]
    other_items: tuple[ReplyItem, ...]
    unaccounted_labels: tuple[str, ...]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


_TEXT: Final[dict[str, str]] = {"type": "string"}
_PIXEL_BOX: Final[dict[str, Any]] = {
    "type": "array",
    "items": {"type": "number"},
    "description": "[x0, y0, x1, y1] in pixels on that image (origin top-left)",
}

#: The compare call's structured-output schema (`output_config.format`). Hand-written, so its bytes
#: never change between calls (the schema is part of what the prompt cache keys on).
REPLY_SCHEMA: Final[dict[str, Any]] = _object(
    {
        "work_item": {"type": "string", "description": "The work item id you were given"},
        "pairs": {
            "type": "array",
            "items": _object(
                {
                    "vendor_label": {"type": "string", "description": "A vendor label id, p23.D17"},
                    "architect_label": _nullable(
                        {"type": "string", "description": "An architect label id, or null"}
                    ),
                    "measures": _TEXT,
                    "vendor_from": _TEXT,
                    "vendor_to": _TEXT,
                    "architect_from": _TEXT,
                    "architect_to": _TEXT,
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "note": _TEXT,
                }
            ),
        },
        "other_items": {
            "type": "array",
            "items": _object(
                {
                    "kind": {"type": "string", "enum": list(OTHER_KINDS)},
                    "what": _TEXT,
                    "vendor": _nullable(_TEXT),
                    "architect": _nullable(_TEXT),
                    "vendor_image": _nullable({"type": "integer"}),
                    "vendor_box_px": _nullable(_PIXEL_BOX),
                    "architect_image": _nullable({"type": "integer"}),
                    "architect_box_px": _nullable(_PIXEL_BOX),
                    "verdict": {"type": "string", "enum": list(OTHER_VERDICTS)},
                    "millwork": {"type": "boolean"},
                }
            ),
        },
        "unaccounted_labels": {"type": "array", "items": {"type": "string"}},
    }
)


# ---- the compare call's outcome ----------------------------------------------------------------


class Picture(_Frozen):
    """One picture a compare call sent: what it shows and how its pixels map to PDF points."""

    number: int
    """`Image N` in the call (1-based)."""
    drawing: str
    """The drawing's ref (`p23.v1`, `p23.a1`)."""
    role: Role
    file: str
    page: int
    box: Box
    """The PDF points the picture shows (origin top-left); pixel (0, 0) is its top-left."""
    width: int
    height: int
    dpi: int
    tokens: int
    """Visual tokens: ceil(width / 28) x ceil(height / 28)."""
    part: str
    """`whole`, `overview`, or `tile 1 of 2`."""
    labels: tuple[str, ...] = ()
    """The label ids marked on it."""


class CompareOutcome(_Frozen):
    """What one work item's compare call gave. `failed` items are listed, never dropped."""

    work_item: str
    status: Literal["ok", "failed"]
    result: CompareResult | None = None
    reason: str | None = None
    """Why it failed, in plain words."""
    problems: tuple[str, ...] = ()
    """Code's checks that the last answer did not pass."""
    partial: CompareResult | None = None
    """A failed item's last answer, for a person to read: never a source of findings."""
    asked_model: str
    model: str | None = None
    """The model that answered (a refusal fallback may be another)."""
    effort: str
    attempts: int = 0
    """Model calls made (retries and the repair turn included)."""
    repaired: bool = False
    usage: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    """Estimated from `usage` at Anthropic's list prices (`compare.estimate_cost_usd`)."""
    seconds: float = 0.0
    pictures: tuple[Picture, ...] = ()


# ---- code's checks on an answer ----------------------------------------------------------------


def _ids(refs: Iterable[str]) -> str:
    listed = list(refs)
    shown = ", ".join(listed[:_SHOWN_IDS])
    more = len(listed) - _SHOWN_IDS
    return shown + (f" and {more} more" if more > 0 else "")


def check_result(result: CompareResult, item: WorkItem) -> list[str]:
    """Code's checks on one answer, as plain sentences the model can act on; empty when it passes.

    - it answers this work item;
    - every vendor label of the drawing is in a pair or in `unaccounted_labels`, and none is both;
    - every label named is one of the item's (vendor labels from its vendor drawing, architect
      labels from its architect drawings);
    - `same`, `different` and `different_points` name an architect label; `no_architect_value`
      names none;
    - an item without a label carries the box code needs to find it on the drawing: an architect
      item missing on the vendor its architect value, box and page; an item the vendor added its
      vendor value and box; any other at least one box, on an architect page of this item."""
    problems: list[str] = []
    if result.work_item != item.id:
        problems.append(f"work_item is {result.work_item!r}; this work item is {item.id!r}")
    vendor = set(item.vendor_refs)
    architect = set(item.architect_refs)
    paired: set[str] = set()
    for n, pair in enumerate(result.pairs):
        where = f"pairs[{n}]"
        if pair.vendor_label not in vendor:
            problems.append(
                f"{where}.vendor_label {pair.vendor_label} is not a label of the vendor drawing "
                f"{item.id}"
            )
        paired.add(pair.vendor_label)
        if pair.architect_label is not None and pair.architect_label not in architect:
            problems.append(
                f"{where}.architect_label {pair.architect_label} is not a label of this item's "
                "architect drawings"
            )
        if pair.verdict in _PAIRED and pair.architect_label is None:
            problems.append(
                f"{where} ({pair.vendor_label}) says {pair.verdict} but names no architect label: "
                "name it, or use no_architect_value (or unsure)"
            )
        if pair.verdict == "no_architect_value" and pair.architect_label is not None:
            problems.append(
                f"{where} ({pair.vendor_label}) says no_architect_value but names "
                f"{pair.architect_label}"
            )
    unaccounted = set(result.unaccounted_labels)
    foreign = sorted(unaccounted - vendor, key=_order)
    if foreign:
        problems.append(f"unaccounted_labels holds ids that are not vendor labels: {_ids(foreign)}")
    both = sorted(unaccounted & paired, key=_order)
    if both:
        problems.append(f"in a pair and also in unaccounted_labels: {_ids(both)}")
    missing = sorted(vendor - paired - unaccounted, key=_order)
    if missing:
        problems.append(
            f"vendor labels not accounted for ({len(missing)}): {_ids(missing)}; put each in a "
            "pair or in unaccounted_labels"
        )
    pages = set(item.architect_pages)
    for n, other in enumerate(result.other_items):
        where = f"other_items[{n}] ({other.kind}: {other.what[:60]})"
        if other.architect_page is not None and other.architect_page not in pages:
            problems.append(
                f"{where}: architect_page {other.architect_page} is not one of this item's "
                f"architect pages {sorted(pages)}"
            )
        if other.architect_box is not None and other.architect_page is None:
            problems.append(f"{where}: an architect box needs its architect page")
        if other.kind == "missing_on_vendor" and (
            not other.architect or other.architect_box is None or other.architect_page is None
        ):
            problems.append(
                f"{where}: an item missing on the vendor needs the architect's value or tag and "
                "its box on the architect drawing"
            )
        elif other.kind == "added_on_vendor" and (not other.vendor or other.vendor_box is None):
            problems.append(
                f"{where}: an item the vendor added needs the vendor's value and its box"
            )
        elif other.vendor_box is None and other.architect_box is None:
            problems.append(f"{where}: give the box of what you compared on at least one drawing")
    return problems


def _order(ref: str) -> tuple[int, int]:
    found = _REF.match(ref)
    return (int(found.group(1)), int(found.group(2))) if found else (0, 0)
