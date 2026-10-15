"""One read-only list of exactly what a person must check on a package (#1338, simple review W1).

The simple review screen shows one list instead of the advanced view's tabs. This builds it from the
projections the advanced view already reads, and adds nothing of its own: no model, no check, no
write, no guess.

**Three groups, and every blocking result in exactly one of them.**

* `items`: the questions a person answers. A width number the AI could not settle (`number`, one
  per reading, so each can be confirmed on its own), a countertop to check by hand
  (`countertop_by_hand`: the row itself was held, or its held numbers have no AI guess), a wall
  choice, a page listed with no countertop, an architect question, a depth or sink reading to
  confirm (`depth_reading`, one per reading, #1360; one `rechecking` item once a person confirmed
  it, until the re-check), or any other result that waits for a person (`other_result`).
* `failures`: FAIL results. Each stays blocking and needs one Agree (Anant, decision 2).
* `not_checked`: the depth and sink checks that could not run because nothing was read for them
  (Anant, decision 1: one quiet group). Each names the Company settings fields its run also lacked
  (`settings`, #1373), worked out from the rule and the settings its run cited, so the screen can
  link to them; none when it lacked only drawn values. Each says whether it is set now (`set_now`,
  #1382): saved since the run, so the checks need running again.

**A cabinet run waiting on a person is one `cabinet_kinds` item** (#648, Step 2). Each cabinet of
the run entered on Measurements is a row: its place, the architect's and the vendor's width, and the
kind the reviewer marked last (none until they do). The reviewer marks each one: it keeps its width
(an equipment cabinet: sink, microwave, fridge, other) or it can be resized (single door, double
door, drawer). Nothing is pre-marked by code or AI: no suggestion exists for a cabinet's kind. The
per-type width limits the run lacked are listed in `settings`, each a Company settings field, never a
guessed width. Marks saved after the result are read by the next run: the item is `rechecking` until
then. A run with no cabinet list stays a result to check (`other_result`).

`unaccounted` lists any blocking result, or any page that blocks sign-off, that is in none of the
groups or in more than one. It must be empty; it is computed here, on every read, so the screen can
never hide a result by accident (never silent).

**The best guess, and what is never one.** For a held number the guess is exactly its stored value,
the one a person's Confirm records (W2, #1339), never the PDF text or one reader's text when they
differ from it (#1358). Every reading (the PDF's own text, each AI reader's) is listed beside it, so
a disagreement is in sight. A number with no stored value in inches has no guess. A number under
GV's markup has no guess and no reader texts: GV's markup is the answer key, never a reading
source. The hold text "reviewer wrote X" is never shown. A number with no guess is checked by hand,
with no typing (confirm-only).

**A countertop whose only open question is its walls is one wall choice** (#1358): every number
settled, nothing held, no layout set by a person and the check not run on any walls. It is listed
as `wall_choice` even when the readers proposed a layout, never as checked by hand (whose "It is
right" would confirm a result that depends on walls nobody chose). Each layout is offered with the
result the width check recorded for it, the readers' proposal pre-selected.

**Status is the result's, not the number's.** Several `number` items of one width result share its
finding id and its status. Nothing records a decision on one held number yet; when W3/W2 add one,
each number's own status should be read from it.

**Reader texts** come from the reading's own record (`reader:<model>:<text>` on the candidate), so
nothing under `extraction/` is imported (`tests/api/test_no_heavy_work.py`).

Source: issues #1338, #1360. Verification: `tests/api/test_review_items.py`,
`tests/api/test_review_item_depth.py`.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CheckRun, Finding, PackageRevision, RuleDefinition, RuleSnapshot
from app.models.drawing import VendorPagePicture
from app.models.evidence import (
    CanonicalObservation,
    EvidenceSupportingCandidate,
    ItemClassification,
    ObservationCandidate,
)
from app.review.approval import readiness_and_decisions
from app.review.depth_readings import depth_readings_for
from app.review.requirements import BLOCKING_OUTCOMES
from app.review.row_location import RowLocation
from app.review.waiting_values import missing_settings_for, settings_set_now
from app.review.wall_cases import unavailable_reason
from app.schemas.findings import DepthReadingOut
from app.schemas.visual_ui import CountertopItemRefOut, CountertopResultOut, ExactValueOut
from app.verdicts.rulebook import snapshot_store
from rules.required_inputs import categories_for
from rules.schema import Applicability
from rules.semantic_types import SemanticType
from units.imperial import format_inches, parse_imperial
from verdict.operands import EvidenceStatus
from verdict.outcomes import Outcome
from vocabulary.cabinet_categories import CABINET_CATEGORY_LABELS, CabinetCategory
from vocabulary.reviewer_reasons import finding_reviewer_reason, reviewer_reason
from vocabulary.setting_names import setting_name
from vocabulary.wall_layouts import wall_case_words
from workflow.measurements import operands_for
from workflow.slot_row_scope import SlotRow, candidate_is_sealed, slot_rows
from workflow.wall_cases import is_every_wall_case

__all__ = [
    "CABINET_RULE",
    "CABINET_TYPE_INPUT",
    "DEPTH_AND_SINK_RULES",
    "NOT_CHECKED_NOTE",
    "BestGuessOut",
    "ReviewItemOut",
    "ReviewItemsOut",
    "WallChoiceOut",
    "account",
    "best_guess",
    "parsed_inches",
    "review_items_for_revision",
]

ItemKind = Literal[
    "number",
    "countertop_by_hand",
    "wall_choice",
    "no_countertop_page",
    "architect",
    "depth_reading",
    "cabinet_kinds",
    "other_result",
]
Status = Literal["open", "done", "rechecking"]
GuessSource = Literal["pdf_text", "agreed_text", "suggestion"]

#: The six depth and sink checks (`app/review/waiting_values.py`), with their plain names.
DEPTH_AND_SINK_RULES: Final[Mapping[str, str]] = {
    "CT-DEPTH-001": "Countertop depth",
    "CT-SINK-CUTOUT-WIDTH-001": "Sink cut-out width",
    "CT-SINK-CUTOUT-DEPTH-001": "Sink cut-out depth",
    "CT-SINK-OFFSET-FRONT-001": "Sink front offset",
    "CT-BACK-OFFSET-MIN-001": "Back offset minimum",
    "CT-SINK-CABINET-WIDTH-001": "Sink cabinet width",
}

WIDTH_RULE: Final = "CT-WIDTH-001"
ARCHITECT_RULE: Final = "CT-ARCH-WIDTH-001"
TEXT_LAYER_SOURCE: Final = "pdf-text-layer"

#: The cabinet run check that reads the reviewer's marks, and the input they answer (#648).
CABINET_RULE: Final = "CAB-FILLER-001"
CABINET_TYPE_INPUT: Final = "cabinet_type"
#: The run's cabinet lists on Measurements, the architect's and the vendor's, in wall order.
_CABINET_LISTS: Final = ("architectural_cabinets", "shop_cabinets")

NOT_CHECKED_NOTE: Final = (
    "These depth and sink checks did not run: nothing was read for them (the depth and sink "
    "reader is off, or read nothing here). They are printed NOT CHECKED."
)

#: The one hold code that is about the row's numbers, not the whole row: every other row hold
#: (lines split, a tall appliance, stone into pockets or short of the ends) is checked by hand.
_NUMBER_LEVEL_HOLD: Final = "row-held"

#: GV's ink on a number: its markup is never a guess or a reading source.
_GV_MARKUP_FLAGS: Final = frozenset({"reviewer-markup", "gv-tick", "gv-other-mark"})

#: "…; reviewer wrote 42" in a stored hold reason: GV's own words, never shown as a guess.
#: Removed to the end of its clause (the next ";"), so "42.5" or "3 1/2. Box" leave nothing behind.
_REVIEWER_WROTE = re.compile(r";?\s*reviewer wrote\b[^;]*", re.IGNORECASE)

#: A record whose texts may hold more than the vendor's one label: a piece with two or more labels
#: (every label's reader texts are written on one record, with no ink flag, and one of them may be
#: GV's correction beside the vendor's number), or a label whose ink was never checked.
_UNTRUSTED_TEXT_FLAGS: Final = frozenset({"two-labels", "ink-unchecked"})

_ACTIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "number": ("confirm", "fix"),
    "countertop_by_hand": ("checked_right", "checked_wrong"),
    "wall_choice": ("choose_wall",),
    "no_countertop_page": ("confirm_no_countertop", "there_is_a_countertop"),
    "architect": ("answer",),
    "depth_reading": ("confirm", "not_this"),
    "cabinet_kinds": ("mark_cabinets",),
    "other_result": ("checked_right", "checked_wrong"),
    "failure": ("agree",),
    "not_checked": ("settle_group",),
}


class BestGuessOut(BaseModel):
    """The held number's stored value, shown to confirm: exactly what a Confirm records."""

    text: str = Field(description='The stored value in inches, e.g. `42 1/2"`.')
    value: ExactValueOut | None = Field(
        default=None, description="The exact inches, when the text is a plain dimension."
    )
    source: GuessSource = Field(
        description=(
            "Who wrote the stored value. `pdf_text`: the PDF's own text for the label. "
            "`agreed_text`: both AI readers wrote it identically, but a guard held it. "
            "`suggestion`: neither; it is the stored suggestion, and `readers` show the readings."
        )
    )


class ReaderTextOut(BaseModel):
    reader: str
    text: str


class WallChoiceOut(BaseModel):
    """One wall layout a person may pick for a countertop, with the result it would give.

    The result is the width check's own, recorded for every wall case when only the walls were
    open (#1202): nothing is worked out here. Null when there is no such recorded result.
    """

    layout: str = Field(description="The published wall layout, sent back as `wall_config`.")
    words: str = Field(
        description='The layout in the field-cut rule\'s words, e.g. "walls at both ends".'
    )
    proposed: bool = Field(description="True for the AI readers' proposal: shown pre-selected.")
    result: Outcome | None = Field(
        default=None, description="PASS or FAIL with these walls, as the width check recorded it."
    )
    needed: ExactValueOut | None = Field(
        default=None, description="The overall these walls need, as the width check worked it out."
    )
    difference: ExactValueOut | None = Field(
        default=None, description="The vendor's printed overall minus the needed overall."
    )


class ArchitectRefOut(BaseModel):
    """Where the architect's matching number is, or why there is none."""

    location: RowLocation | None = None
    reason: str | None = None


class NotCheckedSettingOut(BaseModel):
    """A setting a not-checked check is waiting for, as listed on Company settings (#1373)."""

    key: str = Field(
        description=(
            "The setting's name (`countertop_overhang`): the field "
            "`#/settings/company?focus=<key>&project=<project_id>` opens."
        )
    )
    name: str = Field(description="The setting's plain name.")
    project_id: UUID = Field(description="The project whose setting it is.")
    set_now: bool = Field(
        default=False,
        description=(
            "Whether the company or project settings hold it now (#1382). The run lacked it, so "
            "true means it was saved after this check ran: saving never re-runs a check, so the "
            "screen says to run the checks again instead of linking to a field that has a value."
        ),
    )


class CabinetChoiceOut(BaseModel):
    """One kind a cabinet may be marked as (`vocabulary/cabinet_categories.py`)."""

    category: str = Field(description="Sent back in `cabinet_kinds`, e.g. `sink_cabinet`.")
    label: str = Field(description="The kind in plain words.")
    keeps_width: bool = Field(
        description="True for an equipment cabinet, whose width never changes; false: resizable."
    )


class CabinetRowOut(BaseModel):
    """One cabinet of the run, left to right, as entered on Measurements."""

    position: int = Field(description="1-based place in the run, left to right.")
    architect_width: str | None = Field(default=None, description='e.g. `24"`.')
    vendor_width: str | None = Field(default=None, description='e.g. `23 1/2"`.')
    marked: str | None = Field(
        default=None, description="The kind the reviewer marked last; null until they mark it."
    )
    keeps_width: bool | None = Field(
        default=None, description="Whether the marked kind keeps its width; null when unmarked."
    )


class ReviewItemOut(BaseModel):
    """One question a person answers."""

    key: str = Field(description="Stable key for this question.")
    kind: ItemKind
    page: int | None = Field(description="1-based vendor page number.")
    countertop: CountertopItemRefOut | None = None
    row_label: str | None = None
    what: str = Field(description="What is being checked, in plain words.")
    best_guess: BestGuessOut | None = Field(
        default=None,
        description="Null when the AI has no guess: the item is then checked by hand, no typing.",
    )
    readers: list[ReaderTextOut] = Field(default_factory=list)
    why: str = Field(description="Why a person is asked, in plain words.")
    vendor_ref: RowLocation | None = None
    architect_ref: ArchitectRefOut | None = None
    zoom_ref: str | None = Field(
        default=None,
        description="API path (under the API prefix) of a close-up PNG from the vendor-only page.",
    )
    actions: list[str] = Field(description="What a person may do now; empty once answered.")
    status: Status
    decided_by: str | None = None
    decided_at: datetime | None = None
    finding_id: UUID | None = None
    row_id: UUID | None = None
    candidate_id: UUID | None = None
    wall_choices: list[WallChoiceOut] = Field(
        default_factory=list,
        description=(
            "`wall_choice` only: every wall layout, one click each, the readers' proposal marked "
            "`proposed`, each with the result it would give. Empty for every other kind."
        ),
    )
    wall_results_note: str | None = Field(
        default=None,
        description=(
            "`wall_choice` only: why the choices carry no result (e.g. the checks must run first). "
            "Null when each choice shows its result."
        ),
    )
    vendor_wrote: ExactValueOut | None = Field(
        default=None,
        description="`wall_choice` only: the vendor's printed overall the choices were checked against.",
    )
    cabinets: list[CabinetRowOut] = Field(
        default_factory=list,
        description="`cabinet_kinds` only: each cabinet of the run, left to right, to mark.",
    )
    cabinet_choices: list[CabinetChoiceOut] = Field(
        default_factory=list,
        description="`cabinet_kinds` only: the kinds a cabinet may be marked as.",
    )
    settings: list[NotCheckedSettingOut] = Field(
        default_factory=list,
        description=(
            "`cabinet_kinds` only: the per-type cabinet width limits the run lacked, each a "
            "Company settings field (no company value; set per project). Never guessed."
        ),
    )


class FailureOut(BaseModel):
    """A FAIL result: it will be clouded as wrong, and needs one Agree."""

    key: str
    finding_id: UUID
    rule_id: str
    page: int | None = None
    countertop: CountertopItemRefOut | None = None
    row_label: str | None = None
    why: str
    vendor_ref: RowLocation | None = None
    actions: list[str]
    status: Status
    decided_by: str | None = None
    decided_at: datetime | None = None


class NotCheckedOut(BaseModel):
    """A check that could not run because nothing was read for it."""

    key: str
    finding_id: UUID
    rule_id: str
    check: str = Field(description="The check in plain words.")
    page: int | None = None
    countertop: CountertopItemRefOut | None = None
    row_label: str | None = None
    why: str
    settings: list[NotCheckedSettingOut] = Field(
        default_factory=list,
        description=(
            "The company or project settings the check was missing when it ran, each a field on "
            "Company settings (#1373). Worked out from the rule and what its run had "
            "(`missing_settings_for`), never from the words; empty when it lacked none (a drawn value, "
            "say). The sink's sizes are on the project's sink card, not a field, so never listed."
        ),
    )
    status: Status
    decided_by: str | None = None
    decided_at: datetime | None = None


class UnaccountedOut(BaseModel):
    """A blocking result or page the list failed to place exactly once. Must never happen."""

    kind: Literal["finding", "page"]
    finding_id: UUID | None = None
    page: int | None = None
    why: str


class ReviewHeaderOut(BaseModel):
    pages_read: int = Field(description="Vendor pages the reader looked at for a countertop.")
    numbers: int = Field(description="Width numbers on the countertop rows read.")
    confirmed_by_ai: int = Field(description="Of those, numbers both AI readers settled.")
    checked_by_you: int = Field(
        description="Items, failures and not-checked results a person decided."
    )
    open: int = Field(description="Items and failures still waiting for a person.")
    failures: int
    not_checked: int
    rechecking: int = Field(description="Items waiting for the next check run.")
    can_finalise: bool
    blocked_reason: str | None = None


class ReviewItemsOut(BaseModel):
    package_id: UUID
    revision_id: UUID
    revision_number: int
    header: ReviewHeaderOut
    items: list[ReviewItemOut]
    failures: list[FailureOut]
    not_checked_note: str = NOT_CHECKED_NOTE
    not_checked: list[NotCheckedOut]
    unaccounted: list[UnaccountedOut] = Field(
        description="Must be empty. Anything here is a blocking result the list did not place once."
    )


# --- the best guess -------------------------------------------------------------------------------


def _flags(candidate: object) -> list[str]:
    return [str(flag) for flag in getattr(candidate, "ambiguity_flags", None) or ()]


def under_gv_markup(flags: Iterable[str]) -> bool:
    """Whether GV's ink sits on this number (its markup is never a guess or a reading)."""
    for flag in flags:
        if flag in _GV_MARKUP_FLAGS:
            return True
        if flag.startswith("ink:") and flag != "ink:vendor":
            return True
    return False


def vendor_ink_only(flags: Iterable[str]) -> bool:
    """Whether this record's texts are positively the vendor's one label: checked as vendor ink
    (`ink:vendor`, and no other ink), with no second label and no unchecked ink. Anything else gives
    no guess and no reader texts: the number is checked by hand."""
    listed = set(flags)
    inks = {flag for flag in listed if flag.startswith("ink:")}
    return (
        inks == {"ink:vendor"}
        and not listed & _UNTRUSTED_TEXT_FLAGS
        and not under_gv_markup(listed)
    )


def reader_texts(flags: Iterable[str]) -> list[tuple[str, str]]:
    """Each source's text as the reading recorded it (`reader:<source>:<text>`), in order."""
    found: list[tuple[str, str]] = []
    for flag in flags:
        if not flag.startswith("reader:"):
            continue
        source, _, text = flag.removeprefix("reader:").partition(":")
        if source and text.strip():
            found.append((source, text.strip()))
    return found


def _exact(value: Fraction | None) -> ExactValueOut | None:
    if value is None:
        return None
    return ExactValueOut(
        numerator=str(value.numerator),
        denominator=str(value.denominator),
        display=f'{format_inches(value)}"',
    )


def parsed_inches(text: str) -> Fraction | None:
    """A shown value read back exactly (`20"`, `20 in`, `12 1/4`); None when it is not one."""
    return _parsed(text)


def _parsed(text: str) -> Fraction | None:
    cleaned = text.strip().removesuffix('"').removesuffix(" in").strip()
    try:
        value = parse_imperial(cleaned)
    except (ValueError, TypeError, ArithmeticError):
        return None
    return value if value > 0 else None


def stored_inches(candidate: object) -> Fraction | None:
    """The held number's own stored value in inches, above zero: what a person's Confirm writes
    (W2, #1339). None when nothing is stored, or it is not in inches."""
    numerator = getattr(candidate, "value_numerator", None)
    denominator = getattr(candidate, "value_denominator", None)
    if numerator is None or not denominator or getattr(candidate, "unit", None) != "in":
        return None
    value = Fraction(numerator, denominator)
    return value if value > 0 else None


def best_guess(candidate: object) -> BestGuessOut | None:
    """The held number's guess: exactly its stored value, the one a Confirm records, never another
    reading. `source` says who wrote that value: the PDF's own text, both readers identically, or
    neither (the stored suggestion). The other readings are the item's `readers`, so a
    disagreement is in sight. None under GV's markup, and None when nothing is stored."""
    flags = _flags(candidate)
    if not vendor_ink_only(flags):
        return None
    stored = stored_inches(candidate)
    if stored is None:
        return None
    texts = reader_texts(flags)
    pdf = next((text for source, text in texts if source == TEXT_LAYER_SOURCE), None)
    readers = [text for source, text in texts if source != TEXT_LAYER_SOURCE]
    source: GuessSource = "suggestion"
    if pdf is not None and _parsed(pdf) == stored:
        source = "pdf_text"
    elif len(readers) >= 2 and all(_parsed(text) == stored for text in readers):
        source = "agreed_text"
    return BestGuessOut(text=f'{format_inches(stored)}"', value=_exact(stored), source=source)


def _readers(candidate: object) -> list[ReaderTextOut]:
    """Every reading of the number as recorded: the PDF's own text (`pdf-text-layer`) and each AI
    reader's. Empty unless the record is positively the vendor's one label."""
    flags = _flags(candidate)
    if not vendor_ink_only(flags):
        return []
    return [ReaderTextOut(reader=source, text=text) for source, text in reader_texts(flags)]


def plain_why(text: str | None, fallback: str) -> str:
    """A reason in plain words, without GV's own written words ("reviewer wrote X")."""
    cleaned = _REVIEWER_WROTE.sub("", text or "").strip()
    if not cleaned:
        return fallback
    return cleaned[:1].upper() + cleaned[1:]


# --- the accounting -------------------------------------------------------------------------------


def account(
    *,
    blocking_findings: Iterable[UUID],
    blocking_pages: Iterable[int],
    items: Sequence[ReviewItemOut],
    failures: Sequence[FailureOut],
    not_checked: Sequence[NotCheckedOut],
) -> list[UnaccountedOut]:
    """Every blocking result in exactly one group, and every blocking page in exactly one item.

    In `items` one width result may span several questions (one per held number, plus one for its
    numbers checked by hand): it is still one group. In `failures` and `not_checked` a result is
    listed once. A result in no group, or in two, is unaccounted; so is a listed result that is not
    blocking (the list would ask for something sign-off does not need).
    """
    groups: dict[UUID, list[str]] = {}
    for name, ids in (
        ("items", list({item.finding_id for item in items if item.finding_id is not None})),
        ("failures", [entry.finding_id for entry in failures]),
        ("not_checked", [entry.finding_id for entry in not_checked]),
    ):
        for finding_id in ids:
            groups.setdefault(finding_id, []).append(name)
    required = set(blocking_findings)
    found: list[UnaccountedOut] = []
    for finding_id in sorted(required | set(groups), key=str):
        placed = groups.get(finding_id, [])
        if finding_id not in required:
            found.append(
                UnaccountedOut(
                    kind="finding", finding_id=finding_id, why="listed but not a blocking result"
                )
            )
        elif not placed:
            found.append(UnaccountedOut(kind="finding", finding_id=finding_id, why="in no group"))
        elif len(placed) > 1:
            found.append(
                UnaccountedOut(
                    kind="finding",
                    finding_id=finding_id,
                    why="listed more than once: " + ", ".join(placed),
                )
            )
    page_items = Counter(
        item.page
        for item in items
        if item.page is not None
        and (item.kind == "no_countertop_page" or (item.finding_id is None and item.row_id))
    )
    for page in sorted(set(blocking_pages)):
        count = page_items.get(page, 0)
        if count != 1:
            found.append(
                UnaccountedOut(
                    kind="page",
                    page=page,
                    why="in no item" if count == 0 else f"in {count} items",
                )
            )
    return found


# --- building the list ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Live:
    finding: Finding
    rule_id: str
    rule_snapshot_id: UUID


def _live_findings(session: Session, revision_id: UUID) -> list[_Live]:
    return [
        _Live(finding, rule_id, snapshot_id)
        for finding, rule_id, snapshot_id in session.execute(
            select(Finding, RuleDefinition.rule_id, CheckRun.rule_snapshot_id)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
            .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
            .where(Finding.package_revision_id == revision_id, CheckRun.superseded_at.is_(None))
            .order_by(Finding.created_at, Finding.id)
        ).all()
    ]


@dataclass(frozen=True, slots=True)
class _Cited:
    """What `missing_settings_for` reads of a result."""

    id: UUID
    rule_snapshot_id: UUID
    parameter_set_versions: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _DepthItem:
    """What `depth_readings_for` reads of a result."""

    id: UUID
    package_revision_id: UUID
    rule_id: str
    rule_snapshot_id: UUID
    outcome: Outcome
    scope_item_id: UUID | None
    scope_row_candidate_id: UUID | None


#: The view a depth or sink value was read on, as the item's sentence names it.
_ON_VIEW: Final[Mapping[str, str]] = {
    "plan": "the plan view",
    "section": "the section",
    "side": "the side view",
    "front": "the front view",
    "detail": "the detail view",
}


def _depth_item(
    line: DepthReadingOut,
    *,
    finding: Finding,
    what: str,
    page: int | None,
    countertop: CountertopItemRefOut | None,
    row_label: str | None,
    state: Status,
    decided_by: str | None,
    decided_at: datetime | None,
    sole: bool,
) -> ReviewItemOut:
    """One depth or sink reading to confirm (#1360): its value is exactly what a Confirm records.

    Confirm is offered only where the advanced depth card offers it (`can_confirm`: the countertop's
    own result, alone on its drawing, with the reading's picture, not answered yet, #1296). "Not
    this" records the whole result as not checked (Anant, 2026-10-16), so it is offered only when
    the result is one countertop's own and this is its one reading open (`sole`): never on a result
    covering several countertops, and never to settle another reading unseen."""
    stored = None if line.value is None else _parsed(line.value)
    on = _ON_VIEW.get(line.view or "", "the drawing")
    offered = [
        *(["confirm"] if line.can_confirm else []),
        *(["not_this"] if sole else []),
    ]
    if sole:
        ask = "Confirm it once, or say it is not this value (the check is then not checked)."
    elif line.can_confirm:
        ask = (
            "Confirm each reading of this check on its own; to say one is not this value, use the "
            "advanced depth card."
        )
    else:
        ask = "Answer it on the advanced depth card."
    return ReviewItemOut(
        key=f"depth:{finding.id}:{line.candidate_id}",
        kind="depth_reading",
        page=page if page is not None else line.page_number,
        countertop=countertop,
        row_label=row_label or line.countertop,
        what=what,
        best_guess=(
            None
            if stored is None
            else BestGuessOut(
                text=f'{format_inches(stored)}"', value=_exact(stored), source="agreed_text"
            )
        ),
        why=(
            f"Both AI readers read {line.value} for the {line.label.lower()} on {on}, but only "
            f"its drawn length checks it. {ask}"
        ),
        actions=offered if state == "open" else [],
        status=state,
        decided_by=decided_by,
        decided_at=decided_at,
        finding_id=finding.id,
        row_id=finding.scope_row_candidate_id,
        candidate_id=line.candidate_id,
    )


def _confirmed_at(session: Session, candidate_ids: Iterable[UUID]) -> dict[UUID, datetime]:
    """When a person last confirmed each reading (its newest HUMAN_CONFIRMED observation), from
    this screen or the advanced depth card. A result made before that waits for the re-check that
    uses it (#1360); one made after it has used it."""
    ids = list(set(candidate_ids))
    if not ids:
        return {}
    newest: dict[UUID, datetime] = {}
    for candidate_id, created_at in session.execute(
        select(EvidenceSupportingCandidate.candidate_id, CanonicalObservation.created_at)
        .join(
            CanonicalObservation,
            CanonicalObservation.id == EvidenceSupportingCandidate.canonical_observation_id,
        )
        .where(
            EvidenceSupportingCandidate.candidate_id.in_(ids),
            CanonicalObservation.status == EvidenceStatus.HUMAN_CONFIRMED.value,
        )
    ).all():
        if candidate_id not in newest or created_at > newest[candidate_id]:
            newest[candidate_id] = created_at
    return newest


def _slot(candidate: object) -> int | None | Literal[-1]:
    raw = next(
        (flag.removeprefix("slot:") for flag in _flags(candidate) if flag.startswith("slot:")), ""
    )
    if raw == "overall":
        return None
    return int(raw) if raw.isdigit() else -1


def _what(position: int | None) -> str:
    return "Overall countertop width" if position is None else f"Piece {position + 1} width"


def review_items_for_revision(
    session: Session,
    *,
    project_id: UUID,
    package_id: UUID,
    revision: PackageRevision,
    api_root: str = "",
) -> ReviewItemsOut:
    """The list for one revision. Read-only; a fixed handful of reads whatever the rows."""
    # Imported here: the projection lives with its route, and importing it at module load would
    # make `app.review` load the API package first (as `app/review/signed_exports.py` does).
    from app.api.visual_countertops import _countertop_results_for_revision

    projection = _countertop_results_for_revision(session, package_id, revision)
    readiness, records = readiness_and_decisions(session, revision.id)
    open_ids = set(readiness.blocking_finding_ids)
    corrected = set(records.corrected)
    live = _live_findings(session, revision.id)
    # Every result sign-off waits on: a blocking outcome, decided or not, and a corrected result
    # waiting for its re-run (readiness lists it whatever its outcome).
    blocking = [
        entry
        for entry in live
        if entry.finding.outcome in BLOCKING_OUTCOMES or entry.finding.id in open_ids
    ]

    rows = slot_rows(session, revision.id) if projection.countertops_checked else ()
    row_by_id: dict[UUID, SlotRow] = {row.anchor.id: row for row in rows}
    result_by_row: dict[UUID, CountertopResultOut] = {
        item.row_id: item for item in projection.items
    }
    result_by_finding: dict[UUID, CountertopResultOut] = {
        item.finding_id: item for item in projection.items if item.finding_id is not None
    }
    pictured = (
        set(
            session.scalars(
                select(VendorPagePicture.page_id).where(
                    VendorPagePicture.page_id.in_(
                        {candidate.page_id for row in rows for candidate in row.candidates}
                    )
                )
            ).all()
        )
        if rows
        else set()
    )
    prefix = f"{api_root}/projects/{project_id}/packages/{package_id}"

    def zoom(candidate: ObservationCandidate) -> str | None:
        if candidate.page_id not in pictured:
            return None
        return f"{prefix}/candidates/{candidate.id}/vendor-only/picture"

    def status_of(finding_id: UUID | None) -> tuple[Status, str | None, datetime | None]:
        if finding_id is None or finding_id in corrected:
            return "rechecking", None, None
        if finding_id in open_ids:
            return "open", None, None
        decision = records.decisions.get(finding_id)
        if decision is None:
            return "done", None, None
        return "done", decision.action.actor, decision.action.created_at

    published: list[tuple[str, ...]] = []

    def layouts() -> tuple[str, ...]:
        """The published wall layouts, read once and only when a wall choice needs them."""
        if not published:
            published.append(_published_wall_layouts(session))
        return published[0]

    def actions(kind: str, status: Status) -> list[str]:
        return list(_ACTIONS[kind]) if status == "open" else []

    def scope_of(finding: Finding) -> tuple[int | None, CountertopItemRefOut | None, str | None]:
        row_id = finding.scope_row_candidate_id
        result = None if row_id is None else result_by_row.get(row_id)
        if result is not None:
            return result.page_number, result.countertop_item, result.label
        row = None if row_id is None else row_by_id.get(row_id)
        if row is not None:
            return row.page_number, None, row.label
        return None, None, None

    def architect_ref(result: CountertopResultOut | None) -> ArchitectRefOut:
        if result is None:
            return ArchitectRefOut(reason="Not compared with the architect's drawing here.")
        located = next(
            (
                pair.architect_location
                for pair in result.architect.compared
                if pair.architect_location
            ),
            None,
        )
        if located is not None:
            return ArchitectRefOut(location=located)
        return ArchitectRefOut(
            reason=result.architect.not_compared_reason
            or result.architect.reason
            or "No architect number was placed for this row."
        )

    depth_lines = depth_readings_for(
        session,
        [
            _DepthItem(
                entry.finding.id,
                entry.finding.package_revision_id,
                entry.rule_id,
                entry.rule_snapshot_id,
                Outcome(entry.finding.outcome),
                entry.finding.scope_item_id,
                entry.finding.scope_row_candidate_id,
            )
            for entry in blocking
            if entry.rule_id in DEPTH_AND_SINK_RULES
        ],
    )

    confirmed_at = _confirmed_at(
        session,
        (
            line.candidate_id
            for lines in depth_lines.values()
            for line in lines
            if line.state == "confirmed" and line.candidate_id is not None
        ),
    )

    # The settings each depth and sink check was missing when it ran (#1373): from the rule as run
    # and the settings versions it cited, never from the reason's words. Only fields on Company
    # settings are listed; the sink's sizes live on its card.
    missing = missing_settings_for(
        session,
        [
            _Cited(entry.finding.id, entry.rule_snapshot_id, entry.finding.parameter_set_versions)
            for entry in blocking
            if entry.rule_id in DEPTH_AND_SINK_RULES or entry.rule_id == CABINET_RULE
        ],
    )

    # Which of them are held now (#1382): saved since the run, so the checks need running again.
    held_now = settings_set_now(
        session, project_id, {name for names in missing.values() for name in names}
    )

    def settings_for(finding_id: UUID) -> list[NotCheckedSettingOut]:
        return [
            NotCheckedSettingOut(
                key=name,
                name=setting_name(name),
                project_id=project_id,
                set_now=name in held_now,
            )
            for name in missing.get(finding_id, [])
        ]

    cabinet_runs: list[tuple[tuple[CabinetRowOut, ...], datetime | None] | None] = []

    def cabinet_run() -> tuple[tuple[CabinetRowOut, ...], datetime | None] | None:
        """The run's cabinets and when they were last marked, read once and only when needed."""
        if not cabinet_runs:
            cabinet_runs.append(_cabinet_run(session, revision.id))
        return cabinet_runs[0]

    items: list[ReviewItemOut] = []
    failures: list[FailureOut] = []
    not_checked: list[NotCheckedOut] = []

    for entry in blocking:
        finding, rule_id = entry.finding, entry.rule_id
        state, decided_by, decided_at = status_of(finding.id)
        page, countertop, row_label = scope_of(finding)
        reason = finding_reviewer_reason(finding.outcome, finding.reason)
        why = plain_why(reason, "This result needs a person.")
        result = result_by_finding.get(finding.id)
        if finding.outcome == Outcome.FAIL.value:
            failures.append(
                FailureOut(
                    key=f"failure:{finding.id}",
                    finding_id=finding.id,
                    rule_id=rule_id,
                    page=page,
                    countertop=countertop,
                    row_label=row_label,
                    why=why,
                    vendor_ref=None if result is None else result.row_location,
                    actions=actions("failure", state),
                    status=state,
                    decided_by=decided_by,
                    decided_at=decided_at,
                )
            )
            continue
        if rule_id in DEPTH_AND_SINK_RULES:
            lines = depth_lines.get(finding.id, [])
            to_confirm = [
                line
                for line in lines
                if line.state == "to_confirm" and line.candidate_id is not None
            ]
            if to_confirm:
                items.extend(
                    _depth_item(
                        line,
                        finding=finding,
                        what=DEPTH_AND_SINK_RULES[rule_id],
                        page=page,
                        countertop=countertop,
                        row_label=row_label,
                        state=state,
                        decided_by=decided_by,
                        decided_at=decided_at,
                        sole=finding.scope_item_id is not None and len(to_confirm) == 1,
                    )
                    for line in to_confirm
                )
            elif state == "open" and any(
                line.state == "confirmed"
                and line.candidate_id in confirmed_at
                and confirmed_at[line.candidate_id] > finding.created_at
                for line in lines
            ):
                # A person confirmed its reading here; the result waits for the re-check that uses
                # it. Never "Not checked": something was read, and a person answered it (#1360).
                items.append(
                    ReviewItemOut(
                        key=f"depth:{finding.id}",
                        kind="depth_reading",
                        page=page,
                        countertop=countertop,
                        row_label=row_label,
                        what=DEPTH_AND_SINK_RULES[rule_id],
                        why="A person confirmed the reading; the checks run again to use it.",
                        actions=[],
                        status="rechecking",
                        finding_id=finding.id,
                        row_id=finding.scope_row_candidate_id,
                    )
                )
            else:
                not_checked.append(
                    NotCheckedOut(
                        key=f"not-checked:{finding.id}",
                        finding_id=finding.id,
                        rule_id=rule_id,
                        check=DEPTH_AND_SINK_RULES[rule_id],
                        page=page,
                        countertop=countertop,
                        row_label=row_label,
                        why=why,
                        settings=settings_for(finding.id),
                        status=state,
                        decided_by=decided_by,
                        decided_at=decided_at,
                    )
                )
            continue
        if rule_id == WIDTH_RULE and result is not None:
            items.extend(
                _width_items(
                    result,
                    row_by_id.get(result.row_id),
                    finding=finding,
                    why=why,
                    state=state,
                    decided_by=decided_by,
                    decided_at=decided_at,
                    actions=actions,
                    zoom=zoom,
                    architect=architect_ref(result),
                    layouts=layouts,
                )
            )
            continue
        if rule_id == CABINET_RULE and finding.outcome != Outcome.FAIL.value:
            run = cabinet_run()
            if run is not None:
                cabinet_rows, marked_at = run
                marked_since = marked_at is not None and marked_at > finding.created_at
                cabinet_state: Status = (
                    "rechecking"
                    if state == "open" and marked_since and all(row.marked for row in cabinet_rows)
                    else state
                )
                items.append(
                    ReviewItemOut(
                        key=f"cabinets:{finding.id}",
                        kind="cabinet_kinds",
                        page=None,
                        what="Which cabinets keep their width?",
                        why=why,
                        actions=actions("cabinet_kinds", cabinet_state),
                        status=cabinet_state,
                        decided_by=decided_by,
                        decided_at=decided_at,
                        finding_id=finding.id,
                        cabinets=list(cabinet_rows),
                        cabinet_choices=cabinet_choices(),
                        settings=settings_for(finding.id),
                    )
                )
                continue
        kind: ItemKind = "architect" if rule_id == ARCHITECT_RULE else "other_result"
        items.append(
            ReviewItemOut(
                key=f"result:{finding.id}",
                kind=kind,
                page=page,
                countertop=countertop,
                row_label=row_label,
                what=(
                    "Compare with the architect's drawing"
                    if kind == "architect"
                    else f"Result of {rule_id}"
                ),
                why=why,
                vendor_ref=None if result is None else result.row_location,
                architect_ref=architect_ref(result) if kind == "architect" else None,
                actions=actions(kind, state),
                status=state,
                decided_by=decided_by,
                decided_at=decided_at,
                finding_id=finding.id,
                row_id=finding.scope_row_candidate_id,
            )
        )

    # A page a person said has a countertop, waiting for its first result (#1191).
    for result in projection.items:
        if result.finding_id is None and result.needs_decision:
            items.append(
                ReviewItemOut(
                    key=f"countertop:{result.row_id}",
                    kind="countertop_by_hand",
                    page=result.page_number,
                    countertop=result.countertop_item,
                    row_label=result.label,
                    what="This countertop",
                    why=plain_why(
                        None if result.hold is None else result.hold.reason,
                        "Waiting for the next check run.",
                    ),
                    vendor_ref=result.row_location,
                    architect_ref=architect_ref(result),
                    actions=[],
                    status="rechecking",
                    row_id=result.row_id,
                )
            )

    # Pages every AI said have no countertop, which a person must confirm (#1191).
    for listed in projection.pages_without_countertop:
        if not listed.needs_answer:
            continue
        confirmation = listed.confirmation
        page_state: Status = "open" if confirmation is None else "done"
        items.append(
            ReviewItemOut(
                key=f"page:{listed.record_id}",
                kind="no_countertop_page",
                page=listed.page_number,
                what="No countertop on this page?",
                why=plain_why(listed.reason, "Every AI said this page has no countertop."),
                architect_ref=ArchitectRefOut(
                    reason="Not compared: no countertop was found on this page."
                ),
                actions=actions("no_countertop_page", page_state),
                status=page_state,
                decided_by=None if confirmation is None else confirmation.confirmed_by,
                decided_at=None if confirmation is None else confirmation.confirmed_at,
                row_id=listed.record_id,
            )
        )

    unaccounted = account(
        blocking_findings=[entry.finding.id for entry in blocking] + list(open_ids),
        blocking_pages=[
            *readiness.unconfirmed_no_countertop_pages,
            *readiness.unchecked_countertop_pages,
        ],
        items=items,
        failures=failures,
        not_checked=not_checked,
    )

    numbers = [candidate for row in rows for candidate in row.candidates if _slot(candidate) != -1]
    everything: list[ReviewItemOut | FailureOut | NotCheckedOut] = [
        *items,
        *failures,
        *not_checked,
    ]
    decided = {
        (entry.finding_id or getattr(entry, "row_id", None) or entry.key)
        for entry in everything
        if entry.status == "done" and entry.decided_by
    }
    header = ReviewHeaderOut(
        pages_read=len(
            {result.page_number for result in projection.items}
            | {page.page_number for page in projection.pages_without_countertop}
        ),
        numbers=len(numbers),
        confirmed_by_ai=sum(1 for candidate in numbers if candidate_is_sealed(candidate)),
        checked_by_you=len(decided),
        open=sum(1 for item in items if item.status == "open")
        + sum(1 for failure in failures if failure.status == "open"),
        failures=len(failures),
        not_checked=len(not_checked),
        rechecking=sum(1 for entry in everything if entry.status == "rechecking"),
        can_finalise=readiness.can_approve,
        blocked_reason=readiness.reason,
    )
    return ReviewItemsOut(
        package_id=package_id,
        revision_id=revision.id,
        revision_number=revision.revision_number,
        header=header,
        items=sorted(items, key=lambda item: (item.page or 0, item.key)),
        failures=failures,
        not_checked=not_checked,
        unaccounted=unaccounted,
    )


def _width_items(
    result: CountertopResultOut,
    row: SlotRow | None,
    *,
    finding: Finding,
    why: str,
    state: Status,
    decided_by: str | None,
    decided_at: datetime | None,
    actions: Callable[[str, Status], list[str]],
    zoom: Callable[[ObservationCandidate], str | None],
    architect: ArchitectRefOut,
    layouts: Callable[[], tuple[str, ...]],
) -> list[ReviewItemOut]:
    """One width result's questions: a held row is one countertop to check by hand; otherwise one
    item per held number with a guess, one more for the held numbers without, or the walls."""
    common = {
        "page": result.page_number,
        "countertop": result.countertop_item,
        "row_label": result.label,
        "status": state,
        "decided_by": decided_by,
        "decided_at": decided_at,
        "finding_id": finding.id,
        "row_id": result.row_id,
        "architect_ref": architect,
    }
    hold = result.hold
    if hold is not None and hold.code != _NUMBER_LEVEL_HOLD:
        return [
            ReviewItemOut(
                key=f"countertop:{finding.id}",
                kind="countertop_by_hand",
                what="This countertop",
                why=plain_why(hold.reason, why),
                vendor_ref=result.row_location,
                actions=actions("countertop_by_hand", state),
                **common,  # type: ignore[arg-type]
            )
        ]
    locations: dict[int | None, RowLocation | None] = {
        piece.index: piece.location for piece in result.pieces
    }
    locations[None] = result.printed_overall_location
    held: list[ObservationCandidate] = []
    if row is not None:
        # Every held number, a record with no `slot:` place too (checked by hand, never skipped).
        held = [candidate for candidate in row.candidates if not candidate_is_sealed(candidate)]
    found: list[ReviewItemOut] = []
    by_hand: list[ObservationCandidate] = []
    for candidate in held:
        guess = best_guess(candidate)
        position = _slot(candidate)
        if guess is None or position == -1:
            by_hand.append(candidate)
            continue
        flags = _flags(candidate)
        found.append(
            ReviewItemOut(
                key=f"number:{candidate.id}",
                kind="number",
                what=_what(position),
                best_guess=guess,
                readers=_readers(candidate),
                # The number's own hold first ("readers differ"); a row's words only without it.
                why=plain_why(
                    candidate.review_reason,
                    reviewer_reason(flags, candidate.review_reason) or why,
                ),
                vendor_ref=locations.get(position),
                zoom_ref=zoom(candidate),
                actions=actions("number", state),
                candidate_id=candidate.id,
                **common,  # type: ignore[arg-type]
            )
        )
    missing_positions: list[int | None] = [
        *([None] if result.printed_overall is None else []),
        *(piece.index for piece in result.pieces if piece.source == "missing"),
    ]
    if by_hand or (not found and (hold is not None or missing_positions)):
        first = by_hand[0] if by_hand else None
        unplaced = sum(1 for candidate in by_hand if _slot(candidate) == -1)
        positions = [position for candidate in by_hand if (position := _slot(candidate)) != -1] or (
            [] if unplaced else missing_positions
        )
        named = [_what(position) for position in positions] + (
            ["a number whose place on the row was not recorded"] * min(unplaced, 1)
        )
        reasons = [
            reviewer_reason(_flags(candidate), candidate.review_reason) for candidate in by_hand
        ]
        found.append(
            ReviewItemOut(
                key=f"countertop:{finding.id}",
                kind="countertop_by_hand",
                what="Check by hand: " + ", ".join(named) if named else "This countertop",
                why=plain_why(
                    next((reason for reason in reasons if reason), None)
                    or (None if hold is None else hold.reason),
                    why,
                ),
                vendor_ref=(
                    (locations.get(_slot(first)) if first is not None else None)
                    or result.row_location
                ),
                zoom_ref=None if first is None else zoom(first),
                actions=actions("countertop_by_hand", state),
                candidate_id=None,
                **common,  # type: ignore[arg-type]
            )
        )
    if found:
        return found
    if _walls_only_open(result, row, finding):
        return [_wall_item(result, row, finding=finding, why=why, common=common, layouts=layouts)]
    return [
        ReviewItemOut(
            key=f"countertop:{finding.id}",
            kind="countertop_by_hand",
            what="This countertop",
            why=why,
            vendor_ref=result.row_location,
            actions=actions("countertop_by_hand", state),
            **common,  # type: ignore[arg-type]
        )
    ]


def _published_wall_layouts(session: Session) -> tuple[str, ...]:
    """The wall layouts the published width check offers, from the rulebook alone (the same
    reading as the wall review's choices); empty when it offers none."""
    snapshot = snapshot_store(session).latest(WIDTH_RULE)
    if snapshot is None or not isinstance(snapshot.rule.applicability, Applicability):
        return ()
    applicability = snapshot.rule.applicability
    if applicability.discriminator != SemanticType.WALL_CONFIG.value:
        return ()
    return tuple(variant.when for variant in applicability.variants)


def _walls_only_open(result: CountertopResultOut, row: SlotRow | None, finding: Finding) -> bool:
    """Whether the walls are this result's open question (#1358): a result waiting for a person
    whose width check ran on no walls (for every wall case, or not at all), with no automatic
    layout and no person's wall answer it already used. Called once nothing on the row is held and
    every number is settled."""
    if result.wall_layout.config is None and result.wall_layout.source == "not established":
        return True
    if finding.outcome != Outcome.REVIEW_REQUIRED.value:
        return False
    if result.wall_layout.source in ("code ink and both AIs", "not needed"):
        return False
    if not is_every_wall_case(finding.trace) and finding.variant:
        # The check ran on a layout: the walls are not the question.
        return False
    return not _answer_used(row, finding)


def _answer_newer(row: SlotRow | None, finding: Finding) -> bool:
    """A person's wall answer saved after this result: it waits for the re-check."""
    decision = None if row is None else row.decision
    return (
        decision is not None
        and decision.wall_config is not None
        and decision.created_at is not None
        and finding.created_at is not None
        and decision.created_at > finding.created_at
    )


def _answer_used(row: SlotRow | None, finding: Finding) -> bool:
    """A person's wall answer this result was checked after: the walls were answered already."""
    decision = None if row is None else row.decision
    return (
        decision is not None
        and decision.wall_config is not None
        and not _answer_newer(row, finding)
    )


def _readers_proposal(result: CountertopResultOut, row: SlotRow | None) -> str | None:
    """The layout the AI readers proposed for this row, as the wall review shows it; None when a
    person chose, the readers' walls were held, or a person's earlier answer is asked again."""
    layout = result.wall_layout
    if layout.chosen_by_reviewer or layout.config is None:
        return None
    if row is not None:
        if row.wall_hold is not None:
            return None
        flags = () if row.wall_candidate is None else row.wall_candidate.ambiguity_flags or ()
        if any(str(flag).startswith("walls-held:") for flag in flags):
            return None
    return layout.config


def _wall_item(
    result: CountertopResultOut,
    row: SlotRow | None,
    *,
    finding: Finding,
    why: str,
    common: Mapping[str, object],
    layouts: Callable[[], tuple[str, ...]],
) -> ReviewItemOut:
    """One click: every wall layout with the result the width check recorded for it, the readers'
    proposal pre-selected. No right/wrong: the walls are chosen, then the checks run again."""
    proposal = _readers_proposal(result, row)
    cases = result.wall_cases
    if cases.cases:
        choices = [
            WallChoiceOut(
                layout=case.layout,
                words=case.words,
                proposed=case.layout == proposal,
                result=case.outcome,
                needed=case.expected_total,
                difference=case.difference,
            )
            for case in cases.cases
        ]
        note = None
    else:
        choices = [
            WallChoiceOut(layout=layout, words=wall_case_words(layout), proposed=layout == proposal)
            for layout in layouts()
        ]
        note = cases.unavailable_reason or unavailable_reason(finding, saved_after=False)
    fields = dict(common)
    # A person's wall answer the result has not used yet: it waits for the re-check.
    if _answer_newer(row, finding) and fields["status"] == "open":
        fields.update(status="rechecking", decided_by=None, decided_at=None)
    state = fields["status"]
    return ReviewItemOut(
        key=f"walls:{finding.id}",
        kind="wall_choice",
        what="Which ends of this countertop meet a wall",
        why=plain_why(result.wall_layout.line, why),
        vendor_ref=result.row_location,
        actions=list(_ACTIONS["wall_choice"]) if state == "open" else [],
        wall_choices=choices,
        wall_results_note=note,
        vendor_wrote=cases.vendor_wrote,
        **fields,  # type: ignore[arg-type]
    )


def cabinet_choices() -> list[CabinetChoiceOut]:
    """Every kind the rulebook allows a cabinet to be marked as, in the form's order."""
    allowed = set(categories_for(SemanticType.CABINET_CATEGORY.value))
    return [
        CabinetChoiceOut(
            category=category.value,
            label=label.removesuffix(" (width cannot change)"),
            keeps_width=category.is_equipment,
        )
        for category, label in CABINET_CATEGORY_LABELS
        if category.value in allowed
    ]


def _inches_text(value: object) -> str | None:
    exact = getattr(value, "exact", None)
    return None if exact is None else f'{format_inches(exact)}"'


def _cabinet_run(
    session: Session, revision_id: UUID
) -> tuple[tuple[CabinetRowOut, ...], datetime | None] | None:
    """The cabinet run entered on Measurements, each with its latest mark; None with no list.

    The lists are the ones the check reads (`workflow/measurements.py:operands_for`), and the marks
    are its classification (`workflow/classifications.py`), latest per position.
    """
    operands = operands_for(session, revision_id).get(CABINET_RULE, {})
    lists: list[tuple[object, ...]] = []
    for name in _CABINET_LISTS:
        operand = operands.get(name)
        value = None if operand is None else operand.value
        lists.append(tuple(value) if isinstance(value, tuple) else ())
    architect, vendor = lists
    count = len(architect) or len(vendor)
    if count == 0:
        return None
    marks = operands.get(CABINET_TYPE_INPUT)
    marked = marks.value if marks is not None and isinstance(marks.value, tuple) else ()
    rows = []
    for index in range(count):
        mark = str(marked[index]) if index < len(marked) else None
        try:
            keeps = None if mark is None else CabinetCategory(mark).is_equipment
        except ValueError:
            keeps = None
        rows.append(
            CabinetRowOut(
                position=index + 1,
                architect_width=_inches_text(architect[index]) if index < len(architect) else None,
                vendor_width=_inches_text(vendor[index]) if index < len(vendor) else None,
                marked=mark,
                keeps_width=keeps,
            )
        )
    marked_at = session.scalars(
        select(ItemClassification.created_at)
        .where(
            ItemClassification.package_revision_id == revision_id,
            ItemClassification.rule_id == CABINET_RULE,
            ItemClassification.input_name == CABINET_TYPE_INPUT,
        )
        .order_by(ItemClassification.created_at.desc())
        .limit(1)
    ).first()
    return tuple(rows), marked_at
