"""Turn a reviewer's correction into an answer-key entry, deliberately and one at a time.

**The answer key has two sources and this is the second one.** The first is annotation: somebody sits
down with a reviewed package and writes the answers out (#188, blocked on #274). The second is
ordinary use — every time a reviewer corrects a reading, the drawing, the region and the right value
are all established by a human who was looking at it. `correction_ledger` already keeps that
permanently (`D5.1`, reachable since `#548`). This is the seam that lets it become a gold case.

**It is a promotion, not a feed.** Nothing here runs automatically and nothing here writes to the
gold set. `AGENTS.md` §2.6 forbids a correction becoming a rule by accumulation, and the same
reasoning applies one layer up: an answer key that grew by itself would become the thing every metric
is measured against without anybody having agreed to a single entry in it. So this produces a
*candidate* entry and says what is missing from it, and a person decides.

**What a correction cannot supply, it does not supply.** A `GoldObservation` needs a page and a
polygon; the ledger has neither, because it points at a `CanonicalObservation` and that is where the
geometry lives. So this reads the geometry from the observation rather than inventing it, and refuses
when it is absent. A gold case with a made-up polygon would score evidence localisation against
fiction, and `ADR-0014` keeps synthetic geometry out of that metric for exactly this reason.

**A correction is not an expected finding.** It says one reading was wrong; it says nothing about
whether the check that used it should have passed. `ExpectedFinding` is the reviewer's verdict and
has to be stated by a person, so a promoted entry carries observations only and the caller is told
that the verdict half is still owed.

Source: issue #553 · Verification: `tests/eval/test_promotion.py`
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Page
from app.models.evidence import (
    CanonicalObservation,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.review import CorrectionLedgerEntry, ReviewAction
from app.models.runs import ExtractionRun
from eval.gold_set.schema import GoldObservation
from evidence.coordinates import PageTransform, StoredPoint
from rules.semantic_types import DocumentRole, OperandSource, SemanticType
from units.measurement import Measurement, Unit

__all__ = ["PromotedCorrection", "PromotionRefused", "promote_correction"]

#: How a `document_role` on an observation maps to the operand source a gold case names.
#:
#: Not the same vocabulary and not merged into one: `DocumentRole` says which drawing a reading came
#: off, `OperandSource` says where a rule gets an operand from — and the second includes `LITERAL`
#: and `USER_INPUT`, which no observation can ever be. A dict rather than a cast, so a role with no
#: gold-set meaning is a `KeyError` at the boundary instead of a wrong label inside a case.
#: Keyed on `DocumentRole`'s own values rather than on strings typed out here. The first version
#: spelled them lowercase — `"shop"` — while the column stores `"SHOP"`, so every single promotion
#: refused with "document role 'SHOP' has no operand source". It failed *closed*, which is why
#: nothing but a test that promotes a real correction would ever have shown it.
_SOURCE_BY_ROLE = {
    DocumentRole.SHOP.value: OperandSource.SHOP,
    DocumentRole.ARCH.value: OperandSource.ARCH,
    DocumentRole.PRODUCT_SPEC.value: OperandSource.PRODUCT_SPEC,
}


@dataclass(frozen=True, slots=True)
class PromotionRefused:
    """Why this correction cannot become an answer-key entry yet.

    Returned rather than raised: a reviewer's correction being unpromotable is an ordinary fact about
    what the system happened to record, not a bug, and a caller walking a week of corrections needs
    to be told which ones it skipped rather than stopped at the first.
    """

    correction_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class PromotedCorrection:
    """One answer-key observation drawn from one correction, and what it still needs.

    `still_owed` is not decoration. A gold case is only usable when somebody has also stated the
    verdict each check should reach, and a promoted observation cannot know that — so the list
    travels with the entry and a caller that ignores it produces a case that scores nothing.
    """

    correction_id: UUID
    observation: GoldObservation
    corrected_by: str
    original_value: str
    """What the system read, kept beside the answer. The ledger's own reason for existing: without
    it there is no way to ask what we got wrong, which is the whole of the correction rate."""

    still_owed: tuple[str, ...]


def _corrected_measurement(entry: CorrectionLedgerEntry, unit: Unit) -> Measurement | None:
    """The reviewer's value, read out of the ledger's stored rendering.

    `app/review/evidence_actions.py:_canonical_value` writes the corrected fact as compact JSON —
    `{"semantic_type": …, "unit": …, "value": "n/d"}` — so this reads that shape back. It is
    deliberately strict: a rendering it does not recognise returns `None` and the correction is
    refused, because guessing at the format would put an unverified number into the answer key every
    other measurement is scored against.
    """
    try:
        rendered = json.loads(entry.corrected_value)
        numerator, _, denominator = str(rendered["value"]).partition("/")
        exact = Fraction(int(numerator), int(denominator or 1))
        stated = Unit(rendered["unit"])
    except (ValueError, KeyError, TypeError, ZeroDivisionError):
        return None
    if stated is not unit:
        # A correction that changed the unit is a different fact from a correction that changed the
        # number, and this seam does not know which the reviewer meant. Refused rather than assumed.
        return None
    return Measurement(exact=exact, unit=stated, raw_text=None)


def promote_correction(
    session: Session, *, correction_id: UUID, item_id: str
) -> PromotedCorrection | PromotionRefused:
    """Draw one answer-key observation from one recorded correction.

    `item_id` comes from the caller because the ledger does not have one: a `CanonicalObservation`
    names a page and a polygon, not the drawing item it sits on, and `GoldObservation` requires the
    item so that a match can be scored later. Asking for it is honest — the person promoting the
    correction is looking at the drawing and knows which item it is — and inventing one would put a
    fabricated identifier into the file every metric is measured against.

    **The semantic type is the reviewer's, carried through unchanged.** It reached the observation
    through `confirm_candidate_type`, where a human named it; nothing in this path infers or
    re-derives it, and the auto-typing guard stays exactly as green as it was.
    """
    entry = session.get(CorrectionLedgerEntry, correction_id)
    if entry is None:
        return PromotionRefused(correction_id, "no correction with that id")

    observation = session.get(CanonicalObservation, entry.canonical_observation_id)
    if observation is None:
        return PromotionRefused(
            correction_id,
            "the corrected observation no longer exists, so there is nothing to say the answer is "
            "about",
        )
    role = str(observation.document_role)
    if role not in _SOURCE_BY_ROLE:
        return PromotionRefused(
            correction_id, f"document role {role!r} has no operand source a gold case can name"
        )

    unit = Unit(observation.unit)
    corrected = _corrected_measurement(entry, unit)
    if corrected is None:
        return PromotionRefused(
            correction_id,
            "the corrected value could not be read back exactly from the ledger, and an answer key "
            "entry will not be built from a value this code had to guess at",
        )

    page = session.get(Page, observation.page_id)
    if page is None:
        return PromotionRefused(correction_id, "the observation's page row no longer exists")
    try:
        region = _region(session, observation, page)
    except _Unplaceable as unplaceable:
        return PromotionRefused(correction_id, str(unplaceable))

    try:
        promoted = GoldObservation(
            semantic_type=SemanticType(observation.semantic_type),
            source=_SOURCE_BY_ROLE[role],
            value=corrected,
            # `GoldObservation.page` is one-based, the way an annotator counts sheets; `pages.index`
            # is zero-based, the way the reader addresses them. The conversion is here rather than
            # in the schema so that a hand-authored answer key stays readable as a human document.
            page=int(page.index) + 1,
            polygon=region,
            item_id=item_id,
        )
    except (ValueError, TypeError) as refused:
        return PromotionRefused(correction_id, f"the promoted entry is not valid: {refused}")

    return PromotedCorrection(
        correction_id=correction_id,
        observation=promoted,
        corrected_by=_actor(session, entry),
        original_value=entry.original_value,
        still_owed=(
            (
                "an expected finding per check this reading feeds — a correction says the reading "
                "was wrong, never whether the check should have passed, and only a reviewer can "
                "say that"
            ),
            (
                "the content hash of the drawing this was annotated against, so the entry can be "
                "refused when the drawing changes underneath it (eval/gold_set/store.py)"
            ),
        ),
    )


class _Unplaceable(Exception):
    """The region on the sheet cannot be recovered from what was recorded.

    Raised rather than returned so that the four separate ways this can happen each carry their own
    sentence to the caller, and none of them can be mistaken for a placed region.
    """


def _region(
    session: Session, observation: CanonicalObservation, page: Page
) -> tuple[int, int, int, int]:
    """The reading's region as the four image-pixel integers a gold observation records.

    **The two sides use different coordinate spaces and the conversion is not a scale factor.** A
    `CanonicalObservation` polygon is in *stored* coordinates — normalised `0..1` against the visible
    crop box, with the page rotation already applied (`observation_space` pins that in the schema).
    A `GoldObservation` polygon is in image pixels, the way an annotator boxes a region on a rendered
    sheet. Migration `0037` persists the media box, the crop box and the run's dpi for exactly this
    trip, and `PageTransform.from_stored` makes it.

    The first version of this function read the stored decimals as pixels directly, which put every
    promoted correction's box at `(0, 0, 0, 0)`: `int(Decimal("0.1"))` is `0`. Found by computing it
    on the polygon the ledger fixture actually stores. That box is not a near miss — it is a
    fabricated region at the origin, and `ADR-0014` keeps synthetic geometry out of
    `evidence_localisation_rate` because a metric scored against one measures nothing.

    So anything missing is a refusal, never a guess: no dpi, no boxes, boxes that do not describe a
    real page, or two dpis disagreeing.
    """
    if page.media_box is None or page.crop_box is None:
        raise _Unplaceable(
            "the page's media box and crop box were never recorded, so a stored polygon cannot be "
            "placed on the sheet. Migration 0037 persists them for renders after it; a page read "
            "before that has to be re-extracted rather than approximated."
        )

    #: Every dpi the evidence behind this reading was rendered at, through the candidates that
    #: support it. Read through the observation's own provenance rather than off any run that
    #: happens to have one: a different sheet's dpi would place the box somewhere nobody looked.
    dpis = {
        # `is not None` as well as the SQL filter: the column is nullable, so the narrowing has to
        # be visible to the type checker and not only to PostgreSQL.
        int(value)
        for value in session.scalars(
            select(ExtractionRun.dpi)
            .join(ObservationCandidate, ObservationCandidate.extraction_run_id == ExtractionRun.id)
            .join(
                EvidenceSupportingCandidate,
                EvidenceSupportingCandidate.candidate_id == ObservationCandidate.id,
            )
            .where(
                EvidenceSupportingCandidate.canonical_observation_id == observation.id,
                ExtractionRun.dpi.isnot(None),
            )
        )
        if value is not None
    }
    if not dpis:
        raise _Unplaceable(
            "no extraction run behind this reading recorded a dpi, and the stored polygon cannot be "
            "converted to pixels without one"
        )
    if len(dpis) > 1:
        # Two routes rendered the same page at two resolutions. Either could be the frame the
        # annotator would box against, and picking whichever row came back first would make the
        # answer key's geometry depend on a query plan.
        raise _Unplaceable(
            f"the evidence behind this reading was rendered at {sorted(dpis)} dpi, so which pixel "
            "grid the region belongs to is ambiguous"
        )

    try:
        media = _box(page.media_box)
        crop = _box(page.crop_box)
        transform = PageTransform(
            dpi=dpis.pop(), rotation=page.rotation or 0, media_box=media, crop_box=crop
        )
        pixels = [
            transform.from_stored(StoredPoint(Decimal(str(point[0])), Decimal(str(point[1]))))
            for point in _points(observation.polygon)
        ]
    except (ArithmeticError, TypeError, ValueError) as refused:
        raise _Unplaceable(
            f"the page's recorded geometry does not describe a page this reading can be placed on: "
            f"{refused}"
        ) from refused

    xs = [point.x for point in pixels]
    ys = [point.y for point in pixels]
    # A box, because that is what `GoldObservation.polygon` is and what the localisation metric
    # compares. Reduced rather than reshaped: pretending a four-tuple holds an arbitrary outline
    # would lose the shape silently.
    return (min(xs), min(ys), max(xs), max(ys))


def _box(stored: object) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """One persisted page box as the four `Decimal`s a transform takes.

    Stored as JSON text precisely so it survives the trip exactly; `Decimal(str(...))` rather than
    `Decimal(float)` for the reason the whole units layer exists.
    """
    if not isinstance(stored, (list, tuple)) or len(stored) != 4:
        raise ValueError("a page box must be four numbers")
    left, bottom, right, top = (Decimal(str(value)) for value in stored)
    return (left, bottom, right, top)


def _points(polygon: object) -> list[tuple[object, object]]:
    """The polygon as pairs, refusing anything else rather than reading past the end of a row."""
    if not isinstance(polygon, list) or not polygon:
        raise ValueError("the observation's polygon is not a list of points")
    points: list[tuple[object, object]] = []
    for point in polygon:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("each polygon point must be a pair")
        points.append((point[0], point[1]))
    return points


def _actor(session: Session, entry: CorrectionLedgerEntry) -> str:
    """Who corrected it, read through the action the ledger entry hangs off.

    The ledger stores no actor of its own — it resolves `(review_action_id, action)` against
    `review_actions`, which is where the authenticated caller was recorded. Reading it from there
    rather than duplicating it is what keeps the two from disagreeing.
    """
    action = session.get(ReviewAction, entry.review_action_id)
    return "" if action is None else action.actor
