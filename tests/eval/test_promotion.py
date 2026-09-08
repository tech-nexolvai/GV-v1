"""Promoting a reviewer's correction into an answer-key entry (#553).

Verification for: `eval/promotion.py`.

**Written against a real correction rather than a hand-built row, and that is the whole reason this
file is useful.** Three defects in the module survived reading it and died the moment a genuine
`correct_evidence` call went through it:

* `_SOURCE_BY_ROLE` was keyed `"shop"` while the column stores `"SHOP"`, so *every* promotion
  refused. It failed closed, so nothing but a passing promotion could have revealed it.
* the stored polygon was read as if it were pixels, putting every promoted region at `(0, 0, 0, 0)`
  — `int(Decimal("0.1"))` is `0`.
* three refusal branches guarded columns that are `NOT NULL`, so they could not fire.

The reading being corrected is built by `tests/review/test_ledger.py:_scenario`, which satisfies the
evidence gate honestly — two extractors, two task runs, the same measurement. Reused rather than
copied so that a change to how a correction is really recorded breaks this file too.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal
from fractions import Fraction
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from alembic import command
from app.auth import Principal, Role
from app.db.session import session_factory, unit_of_work
from app.models import (
    CanonicalObservation,
    CorrectionLedgerEntry,
    EvidenceSupportingCandidate,
    ExtractionRun,
    GoldCase,
    ObservationCandidate,
    Page,
)
from app.review.evidence_actions import correct_evidence
from app.review.ledger import record_correction
from eval.promotion import PromotedCorrection, PromotionRefused, promote_correction
from evidence.coordinates import PageTransform, StoredPoint
from rules.semantic_types import DocumentRole, OperandSource, SemanticType
from tests.app.postgres_fixture import alembic_config
from tests.review.test_ledger import _action, _scenario
from units.measurement import Unit

pytest_plugins = ("tests.app.postgres_fixture",)

#: A letter-size sheet, in PDF points. Spelled as text because that is how migration `0037` stores
#: it — `Decimal(str(...))` all the way through, never a float.
MEDIA_BOX = ["0", "0", "612", "792"]

#: The stored polygon `_scenario` writes: normalised `0..1` against the visible crop box.
STORED_POLYGON = ((Decimal("0.1"), Decimal("0.1")), (Decimal("0.2"), Decimal("0.2")))

DPI = 150


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


def _reviewer(name: str = "keyur") -> Principal:
    return Principal(name, frozenset({Role.REVIEWER}))


def _link(db: Session, finding_id: UUID, observation_id: UUID) -> None:
    """The finding-evidence row `correct_evidence` resolves the observation through."""
    from app.models import FindingEvidence

    db.add(
        FindingEvidence(
            finding_id=finding_id, canonical_observation_id=observation_id, role="operand"
        )
    )
    db.flush()


def _set_dpi(db: Session, observation_id: UUID, *dpis: int) -> None:
    """Record a dpi on the extraction runs behind one observation's evidence.

    `_scenario` leaves it `None`, which is what a run before `#541`'s render settings looks like.
    Set by `UPDATE` rather than by a parameter on the fixture because `extraction_runs` is one of the
    few tables that is *not* append-only — and because a test that hands over two different dpis is
    saying something specific about this seam, not about how a correction gets recorded.
    """
    candidate_ids = list(
        db.scalars(
            select(ObservationCandidate.extraction_run_id)
            .join(
                EvidenceSupportingCandidate,
                EvidenceSupportingCandidate.candidate_id == ObservationCandidate.id,
            )
            .where(EvidenceSupportingCandidate.canonical_observation_id == observation_id)
        )
    )
    assert len(candidate_ids) == 2, "the fixture's reading should rest on two independent routes"
    for run_id, dpi in zip(candidate_ids, dpis * len(candidate_ids), strict=False):
        db.execute(update(ExtractionRun).where(ExtractionRun.id == run_id).values(dpi=dpi))
    db.flush()


def _correction(
    db: Session,
    *,
    media_box: list[str] | None = MEDIA_BOX,
    dpis: tuple[int, ...] = (DPI,),
    corrected: Fraction = Fraction(1, 2),
) -> tuple[UUID, UUID]:
    """One real correction, recorded the way the application records one.

    Returns the ledger entry id and the corrected observation's id. Goes through
    `correct_evidence` — the authorisation, the new immutable row, the review action and the ledger
    write — so that the rendering this module parses is the rendering that is actually stored. A
    hand-written ledger row would have agreed with whatever `eval/promotion.py` expected.
    """
    scenario = _scenario(db, media_box=media_box, crop_box=media_box)
    _link(db, scenario.finding_id, scenario.observation_id)
    if dpis:
        _set_dpi(db, scenario.observation_id, *dpis)
    decision = correct_evidence(
        db,
        principal=_reviewer(),
        review_session_id=scenario.review_session_id,
        finding_id=scenario.finding_id,
        observation_id=scenario.observation_id,
        corrected_value=corrected,
    )
    entry = db.scalars(
        select(CorrectionLedgerEntry).where(
            CorrectionLedgerEntry.review_action_id == decision.action.id
        )
    ).one()
    return entry.id, scenario.observation_id


def _expected_box(dpi: int = DPI) -> tuple[int, int, int, int]:
    """The region the page's own transform puts the stored polygon at.

    Computed here rather than written out as four numbers, so that this asserts the conversion is
    `PageTransform`'s and not that somebody's arithmetic agreed with somebody else's.
    """
    box = (Decimal(0), Decimal(0), Decimal(612), Decimal(792))
    transform = PageTransform(dpi=dpi, rotation=0, media_box=box, crop_box=box)
    pixels = [transform.from_stored(StoredPoint(x, y)) for x, y in STORED_POLYGON]
    xs = [point.x for point in pixels]
    ys = [point.y for point in pixels]
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# A correction becomes an answer
# ---------------------------------------------------------------------------


def test_a_correction_becomes_an_answer_key_entry_carrying_the_reviewers_value(
    postgres_engine: Engine,
) -> None:
    """**Input: a reviewer corrects 1/3" to 1/2". Outcome: an answer-key entry saying 1/2".**

    The whole seam, end to end: the reviewer's value is the answer, their semantic type is carried
    through unchanged, the page number is converted from the reader's zero-based index to the
    one-based sheet an annotator counts, and what the system originally read is kept beside it.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        correction_id, _ = _correction(db)

        promoted = promote_correction(db, correction_id=correction_id, item_id="S_CAB_7")

        assert isinstance(promoted, PromotedCorrection), getattr(promoted, "reason", "")
        assert promoted.observation.value.exact == Fraction(1, 2)
        assert promoted.observation.value.unit is Unit.INCH
        assert promoted.observation.semantic_type is SemanticType.CT001
        assert promoted.observation.source is OperandSource.SHOP
        # `pages.index` is 0 for the first sheet; an annotator writes page 1.
        assert promoted.observation.page == 1
        assert promoted.observation.item_id == "S_CAB_7"
        assert promoted.corrected_by == "keyur"
        assert '"value":"1/3"' in promoted.original_value


def test_the_stored_polygon_is_converted_to_pixels_and_not_read_as_pixels(
    postgres_engine: Engine,
) -> None:
    """**The regression.** Input: a polygon at `0.1..0.2` of the sheet. Outcome: a real region.

    A canonical observation's polygon is normalised `0..1`; an answer key's is image pixels. The
    first version of this module reduced the stored decimals directly, and `int(Decimal("0.1"))` is
    `0` — so every promoted correction claimed the region at the very corner of the sheet. Not a
    near miss: a fabricated box, of the kind `ADR-0014` keeps out of `evidence_localisation_rate`
    because a localisation metric scored against one measures nothing at all.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        correction_id, _ = _correction(db)

        promoted = promote_correction(db, correction_id=correction_id, item_id="S_CAB_7")

        assert isinstance(promoted, PromotedCorrection), getattr(promoted, "reason", "")
        assert promoted.observation.polygon != (0, 0, 0, 0), "the region collapsed to the origin"
        assert promoted.observation.polygon == _expected_box()


def test_the_region_follows_the_dpi_the_evidence_was_rendered_at(
    postgres_engine: Engine,
) -> None:
    """Input: the same polygon on a page rendered at 300 dpi. Outcome: the box scales.

    Proves the transform is genuinely consulted rather than a constant that happens to match at one
    resolution — the failure mode the test above would not catch on its own.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        correction_id, _ = _correction(db, dpis=(300,))

        promoted = promote_correction(db, correction_id=correction_id, item_id="S_CAB_7")

        assert isinstance(promoted, PromotedCorrection), getattr(promoted, "reason", "")
        assert promoted.observation.polygon == _expected_box(300)
        assert promoted.observation.polygon != _expected_box(DPI)


def test_a_promoted_entry_says_the_verdict_half_is_still_owed(postgres_engine: Engine) -> None:
    """**Input: any promotion. Outcome: it names what a person still has to supply.**

    A correction says one reading was wrong. It says nothing about whether the check that used that
    reading should have passed, and `AGENTS.md` §2.6 is explicit that a correction is not a rule
    change. So the verdict half of a gold case stays a reviewer's statement, and a caller that
    ignores `still_owed` produces a case that scores no verdicts at all.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        correction_id, _ = _correction(db)

        promoted = promote_correction(db, correction_id=correction_id, item_id="S_CAB_7")

        assert isinstance(promoted, PromotedCorrection), getattr(promoted, "reason", "")
        assert any("expected finding" in owed for owed in promoted.still_owed)
        assert any("content hash" in owed for owed in promoted.still_owed)


# ---------------------------------------------------------------------------
# What it refuses, and why each refusal is a refusal rather than a guess
# ---------------------------------------------------------------------------


def test_a_page_with_no_recorded_geometry_is_refused(postgres_engine: Engine) -> None:
    """**Input: a page rendered before migration 0037. Outcome: refused, not approximated.**

    Without the media and crop boxes there is no way to turn `0..1` into pixels. Scaling by
    `width_pt`/`height_pt` instead is right for most PDFs and silently wrong for any sheet whose
    crop box differs from its media box — and being silently wrong here would place a reading on a
    region nobody ever looked at.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        correction_id, _ = _correction(db, media_box=None)

        refused = promote_correction(db, correction_id=correction_id, item_id="S_CAB_7")

        assert isinstance(refused, PromotionRefused)
        assert "crop box" in refused.reason
        assert "re-extracted" in refused.reason


def test_evidence_that_recorded_no_dpi_is_refused(postgres_engine: Engine) -> None:
    """Input: extraction runs with no dpi. Outcome: refused.

    The dpi is the pixel grid the normalised polygon is normalised *against*. Defaulting to a
    common one would be inventing the frame the annotator's box is expressed in.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        correction_id, _ = _correction(db, dpis=())

        refused = promote_correction(db, correction_id=correction_id, item_id="S_CAB_7")

        assert isinstance(refused, PromotionRefused)
        assert "no extraction run behind this reading recorded a dpi" in refused.reason


def test_two_disagreeing_dpis_are_refused_rather_than_one_being_picked(
    postgres_engine: Engine,
) -> None:
    """**Input: the same page rendered at 150 and 300 dpi. Outcome: refused as ambiguous.**

    Both routes are real evidence and either grid could be the one an annotator would box against.
    Taking whichever row came back first would make the answer key's geometry depend on a query
    plan — a difference nobody could reproduce, in the file every metric is measured against.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        correction_id, _ = _correction(db, dpis=(150, 300))

        refused = promote_correction(db, correction_id=correction_id, item_id="S_CAB_7")

        assert isinstance(refused, PromotionRefused)
        assert "[150, 300] dpi" in refused.reason
        assert "ambiguous" in refused.reason


def test_a_ledger_value_that_cannot_be_read_back_exactly_is_refused(
    postgres_engine: Engine,
) -> None:
    """**Input: a correction whose stored value is not the rendering this parses. Outcome: refused.**

    `app/review/evidence_actions.py:_canonical_value` writes compact JSON, and this reads that shape
    back strictly. A parser that fell back to something looser would put a number nobody verified
    into the answer key — so an unrecognised rendering is a refusal, and the two staying in step is
    what `test_a_correction_becomes_an_answer_key_entry_carrying_the_reviewers_value` checks.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        scenario = _scenario(db, media_box=MEDIA_BOX, crop_box=MEDIA_BOX)
        _set_dpi(db, scenario.observation_id, DPI)
        action = _action(db, scenario)
        entry = record_correction(
            db,
            review_action_id=action.id,
            canonical_observation_id=scenario.observation_id,
            original='{"value":"1/3"}',
            corrected="about half an inch",
        )

        refused = promote_correction(db, correction_id=entry.id, item_id="S_CAB_7")

        assert isinstance(refused, PromotionRefused)
        assert "could not be read back exactly" in refused.reason


def test_a_correction_that_changed_the_unit_is_refused(postgres_engine: Engine) -> None:
    """Input: a correction whose stored unit differs from the reading's. Outcome: refused.

    A correction that changed the unit is a different fact from one that changed the number, and
    this seam cannot tell which the reviewer meant. Under Q12 the unit is not a detail: inches
    decide and millimetres never do, so promoting the wrong one would put a non-deciding value into
    the file the deciding ones are scored against.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        scenario = _scenario(db, media_box=MEDIA_BOX, crop_box=MEDIA_BOX)
        _set_dpi(db, scenario.observation_id, DPI)
        action = _action(db, scenario)
        entry = record_correction(
            db,
            review_action_id=action.id,
            canonical_observation_id=scenario.observation_id,
            original='{"semantic_type":"CT001","unit":"in","value":"1/3"}',
            # The reading is in inches. This says the same region is 13 millimetres.
            corrected='{"semantic_type":"CT001","unit":"mm","value":"13/1"}',
        )

        refused = promote_correction(db, correction_id=entry.id, item_id="S_CAB_7")

        assert isinstance(refused, PromotionRefused)
        assert "could not be read back exactly" in refused.reason


def test_an_unknown_correction_is_refused_rather_than_raised(postgres_engine: Engine) -> None:
    """**Input: an id naming no correction. Outcome: a refusal a caller can keep walking past.**

    Returned rather than raised on purpose: a caller promoting a week of corrections needs to be
    told which ones it skipped, not stopped at the first one it could not use.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        refused = promote_correction(db, correction_id=uuid4(), item_id="S_CAB_7")

        assert isinstance(refused, PromotionRefused)
        assert refused.reason == "no correction with that id"


# ---------------------------------------------------------------------------
# The two standing guards
# ---------------------------------------------------------------------------


def test_promotion_writes_nothing(postgres_engine: Engine) -> None:
    """**Input: a promotion. Outcome: not one row added, changed or deleted.**

    This is what makes it a promotion rather than a feed. An answer key that grew by itself would
    become the thing every metric is measured against without anybody having agreed to a single
    entry in it — the same reasoning `AGENTS.md` §2.6 applies to corrections becoming rules. So the
    function hands back a candidate and a person decides.

    Counted across the tables a promotion could plausibly touch, and checked through the session's
    own pending sets as well: a row added and not yet flushed would not show up in a count.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        correction_id, _ = _correction(db)
        db.flush()
        watched = (GoldCase, CanonicalObservation, CorrectionLedgerEntry, Page)
        before = [db.scalar(select(func.count()).select_from(model)) for model in watched]

        promoted = promote_correction(db, correction_id=correction_id, item_id="S_CAB_7")

        assert isinstance(promoted, PromotedCorrection), getattr(promoted, "reason", "")
        assert not db.new and not db.dirty and not db.deleted
        assert [db.scalar(select(func.count()).select_from(model)) for model in watched] == before


def test_nothing_here_types_a_reading() -> None:
    """**The semantic-type guard, read at this seam.** Outcome: the type is carried, never derived.

    A `GoldObservation`'s semantic type is a human label — it reached the observation through
    `confirm_candidate_type`, where a person named it. If this module could name one instead, the
    answer key would start containing types nobody agreed to, and every metric measured against it
    would be measuring the system's own guess.

    Read off the identifiers the module actually references, not off its text. The first version
    grepped the source and failed on its own docstring — the sentence *"nothing in this path infers
    or re-derives it"* contains `infer`. A guard that a comment can trip is a guard people delete.
    """
    import ast

    import eval.promotion as module

    source = module.__file__
    assert source is not None
    tree = ast.parse(pathlib.Path(source).read_text(encoding="utf-8"))
    referenced = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }

    typing_names = sorted(
        name
        for name in referenced
        if any(word in name.lower() for word in ("infer", "guess", "classify", "predict"))
    )
    assert not typing_names, f"the promotion path reaches {typing_names}"


def test_every_document_role_has_an_operand_source() -> None:
    """**Input: the roles that exist today. Outcome: all three map.**

    The refusal for an unmapped role is a real branch and no row in the database can reach it, since
    the schema constrains `document_role` to exactly these three. This is the test that fires when a
    fourth role is added — at which point somebody has to decide what a gold case should call it,
    rather than discovering later that every reading off that drawing silently stopped promoting.
    """
    from eval.promotion import _SOURCE_BY_ROLE

    assert {role.value for role in DocumentRole} == set(_SOURCE_BY_ROLE)
