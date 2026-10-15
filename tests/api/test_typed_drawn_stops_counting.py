"""Drawn numbers typed before confirm-only stop counting (#1303).

Since #1253 a number drawn on the vendor's or the architect's drawing is confirmed from a reading and
never typed. Values typed before that stayed in the database and still counted, outranking a later
confirmed reading. Decided 2026-10-15: they stop counting. A drawn value must come from a confirmed
reading.

* A drawn field holding only an old typed value: the check waits (NOT_FOUND), and its reason says
  the typed value no longer counts and why. The typed value stays stored (append-only).
* An old typed value and a reading of the same number: the reading is used, as before.
* An old typed value and a reading of a different number (person-confirmed or corroborated by two
  readers): neither decides. The check waits for a person (REVIEW_REQUIRED), naming both values, so
  stopping the typed value can never turn a FAIL into a PASS, or a PASS into a FAIL.
* A value typed by design (the site width, a typed list, a setting) is unchanged.

"Drawn" is #1253's own definition, `workflow.measurements.confirm_only`: one value on a SHOP or ARCH
drawing. Every check runs through the real engine (`DatabaseStages.run_checks`). Every number and
name is synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.evidence.confirm import confirm_candidate_type
from app.models import (
    CanonicalObservation,
    Document,
    DocumentKind,
    DocumentVersion,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    ExtractionRun,
    ObservationCandidate,
    PackageRevisionDocument,
    Page,
    SourceArtifact,
    TaskRun,
    VerdictInput,
    WorkflowRun,
)
from app.models.evidence import EvidenceCandidateRole
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.models.parameters import to_rows
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity
from tests.api.test_waiting_values import (
    PROJECT,
    _client,
    _findings,
    _package,
    _run,
    _settings,
    _upgrade,
)
from tests.workflow.test_stages import _publish_rulebook
from units.measurement import Unit
from units.normalise import normalise_to_inches
from workflow.measurements import TYPED_DRAWN_STOPPED, operands_for, run_parameters_for

pytest_plugins = ("tests.app.postgres_fixture",)

#: The part of the reason that says why: the same words whatever the inputs are.
NO_LONGER_COUNTS = "before drawn numbers became confirm-only no longer counts"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    _publish_rulebook(opened)
    opened.commit()
    try:
        yield opened
    finally:
        opened.close()


def _typed_before(session: Session, revision: UUID, **values: Fraction) -> None:
    """Numbers typed on Measurements before #1253, stored the way the endpoint used to store them:
    one RUN set for the revision, keyed `rule:input`."""
    _typed_before_in(session, revision, "in", values)


def _typed_before_in(
    session: Session, revision: UUID, unit: str, values: dict[str, Fraction]
) -> None:
    """`_typed_before`, each number in `unit`."""
    versions = session.scalars(
        select(StoredParameterSet.version).where(
            StoredParameterSet.package_revision_id == revision,
            StoredParameterSet.layer == ParameterLayer.RUN.value,
        )
    ).all()
    earlier = ParameterSet(
        project_id=str(PROJECT),
        layer=ParameterLayer.RUN,
        version=max(versions, default=0) + 1,
        parameters={
            key.replace("__", ":"): ParameterValue(
                value=Quantity(value=value, unit=Unit(unit)),
                provenance=Provenance.MEASURED,
                set_by="synthetic reviewer",
                set_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
            for key, value in values.items()
        },
    )
    stored, rows = to_rows(earlier, package_revision_id=revision)
    session.add(stored)
    for row in rows:
        session.add(row)
    session.commit()


def _depth_settings(client: Any, package: UUID) -> None:
    """The job's cabinet depth and overhang: the countertop depth should be 25 1/2"."""
    _settings(client, package, cabinet_depth='24"', countertop_overhang='1 1/2"')


def _used(session: Session, finding: dict[str, Any], name: str) -> VerdictInput:
    rows = session.scalars(
        select(VerdictInput).where(
            VerdictInput.check_run_id == UUID(finding["check_run_id"]),
            VerdictInput.operand_name == name,
        )
    ).all()
    assert len(rows) == 1, rows
    return rows[0]


def test_a_drawn_value_typed_before_no_longer_counts_and_the_check_waits(
    session: Session,
) -> None:
    """Outcome: a countertop depth typed before confirm-only, with no reading, no longer decides
    CT-DEPTH-001 (on the base it PASSed on the typed value). The check waits, and says why in plain
    words. The typed value is still in the database, and the waiting card lists the drawn number as
    waiting for the drawing, not as typed since."""
    client = _client(session)
    package, revision = _package(session)
    _depth_settings(client, package)
    _typed_before(session, revision, **{"CT-DEPTH-001__countertop_depth": Fraction(51, 2)})

    _run(session, revision)
    finding = _findings(client, package)["CT-DEPTH-001"]

    assert finding["outcome"] == "NOT_FOUND", finding
    assert NO_LONGER_COUNTS in finding["reason"], finding["reason"]
    assert "countertop_depth" in finding["reason"]
    assert "Confirm a reading" in finding["reason"]
    assert "CT-DEPTH-001" not in operands_for(session, revision)

    # Append-only: the typed value is still stored, exactly as typed.
    kept = session.scalar(
        select(func.count())
        .select_from(StoredParameterValue)
        .where(StoredParameterValue.name == "CT-DEPTH-001:countertop_depth")
    )
    assert kept == 1

    drawn = next(n for n in finding["missing_inputs"] if n["key"] == "SHOP:CT010")
    assert drawn["scope"] == "drawing"
    assert (
        drawn["saved"] is None
    ), "a typed drawn value no longer counts, so it is not 'saved since'"


def test_the_reason_names_every_stopped_input_of_the_check(session: Session) -> None:
    """Two drawn inputs of one check typed before, neither read: one sentence names both."""
    client = _client(session)
    package, revision = _package(session)
    _typed_before(
        session,
        revision,
        **{
            "CT-BACK-OFFSET-MIN-001__countertop_depth": Fraction(51, 2),
            "CT-BACK-OFFSET-MIN-001__front_offset": Fraction(4),
        },
    )

    _run(session, revision)
    finding = _findings(client, package)["CT-BACK-OFFSET-MIN-001"]

    assert finding["outcome"] == "NOT_FOUND", finding
    assert finding["reason"].count(NO_LONGER_COUNTS) == 1, finding["reason"]
    assert "countertop_depth and front_offset" in finding["reason"]


def _drawing(session: Session, package: UUID, revision: UUID) -> tuple[UUID, UUID, UUID]:
    """One page of a vendor drawing in the revision, with no views: (version, page, extraction)."""
    artifact = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256=uuid4().hex * 2, size=1)
    document = Document(package_id=package, kind=DocumentKind.SHOP.value)
    session.add_all((artifact, document))
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=artifact.id,
        sha256=artifact.sha256,
        page_count=1,
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision,
            package_id=package,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash=artifact.sha256,
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
        media_box=["0", "0", "612", "792"],
        crop_box=["0", "0", "612", "792"],
    )
    workflow_run = WorkflowRun(package_revision_id=revision, engine_run_id=str(uuid4()))
    session.add_all((page, workflow_run))
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    extraction = ExtractionRun(
        task_run_id=task.id,
        extractor="pdfplumber",
        extractor_version="synthetic-1",
        config_hash="synthetic",
        dpi=150,
    )
    session.add(extraction)
    session.flush()
    return version.id, page.id, extraction.id


def _candidate(drawing: tuple[UUID, UUID, UUID], text: str, row: int) -> ObservationCandidate:
    version, page, extraction = drawing
    exact = normalise_to_inches(text).exact
    top = 100 + 40 * row
    return ObservationCandidate(
        document_version_id=version,
        page_id=page,
        extraction_run_id=extraction,
        raw_text=text,
        value_numerator=exact.numerator,
        value_denominator=exact.denominator,
        unit="in",
        polygon=[[100, top], [200, top], [200, top + 20], [100, top + 20]],
        coordinate_space="image",
        ambiguity_flags=[],
    )


def _confirmed(session: Session, package: UUID, revision: UUID, *texts: str) -> None:
    """Countertop depth readings a person confirmed, all on one page of one vendor drawing."""
    drawing = _drawing(session, package, revision)
    for row, text in enumerate(texts):
        candidate = _candidate(drawing, text, row)
        session.add(candidate)
        session.flush()
        confirm_candidate_type(
            session, candidate_id=candidate.id, semantic_type="CT010", confirmed_by="a reviewer"
        )
    session.commit()


def _corroborated(session: Session, package: UUID, revision: UUID, text: str) -> None:
    """A countertop depth two readers read alike (CORROBORATED by SECOND_READER), no person yet."""
    drawing = _drawing(session, package, revision)
    version, page, _extraction = drawing
    readers = [_candidate(drawing, text, 0), _candidate(drawing, text, 0)]
    exact = normalise_to_inches(text).exact
    observation = CanonicalObservation(
        document_version_id=version,
        page_id=page,
        document_role="SHOP",
        polygon=[["0.16", "0.13"], ["0.33", "0.13"], ["0.33", "0.15"], ["0.16", "0.15"]],
        coordinate_space="stored",
        semantic_type="CT010",
        value_numerator=exact.numerator,
        value_denominator=exact.denominator,
        unit="in",
        status="CORROBORATED",
        authority="AUTHORITATIVE",
        evidence_crop_uri=None,
    )
    session.add_all((*readers, observation))
    session.flush()
    session.add_all(
        EvidenceSupportingCandidate(
            canonical_observation_id=observation.id,
            candidate_id=reader.id,
            role=(
                EvidenceCandidateRole.PRIMARY if index == 0 else EvidenceCandidateRole.CORROBORATING
            ),
        )
        for index, reader in enumerate(readers)
    )
    session.add(
        EvidenceCorroborationLane(canonical_observation_id=observation.id, lane="SECOND_READER")
    )
    session.commit()


def test_a_reading_that_agrees_with_a_drawn_value_typed_before_is_used(session: Session) -> None:
    """Outcome: typed 26" before confirm-only and a confirmed reading of 26": the check records the
    reading, and FAILs on it exactly as it FAILed on the typed value."""
    client = _client(session)
    package, revision = _package(session)
    _depth_settings(client, package)
    _confirmed(session, package, revision, '26"')
    _typed_before(session, revision, **{"CT-DEPTH-001__countertop_depth": Fraction(26)})

    _run(session, revision)
    finding = _findings(client, package)["CT-DEPTH-001"]

    used = _used(session, finding, "countertop_depth")
    assert Fraction(used.value_numerator, used.value_denominator) == 26
    assert used.canonical_observation_id is not None, "the reading, not the typed value"
    assert finding["outcome"] == "FAIL", finding
    assert NO_LONGER_COUNTS not in (finding["reason"] or "")


@pytest.mark.parametrize("reading", ["confirmed", "corroborated"])
@pytest.mark.parametrize(
    ("typed", "read", "typed_text", "read_text"),
    [
        # FAIL on the typed value on main; the reading alone would PASS. Must not PASS.
        (Fraction(30), '25 1/2"', "30 in", "25 1/2 in"),
        # PASS on the typed value on main; the reading alone would FAIL. Must not FAIL either.
        (Fraction(51, 2), '26"', "25 1/2 in", "26 in"),
    ],
)
def test_a_reading_that_differs_from_a_drawn_value_typed_before_waits_for_a_person(
    session: Session, reading: str, typed: Fraction, read: str, typed_text: str, read_text: str
) -> None:
    """Outcome: an old typed depth and a reading of a different number. Neither decides: the check
    waits for a person (REVIEW_REQUIRED), and its reason names both values and the day typed. Only
    ever toward waiting, never from FAIL to PASS (the review's probe) or from PASS to FAIL."""
    client = _client(session)
    package, revision = _package(session)
    _depth_settings(client, package)
    if reading == "confirmed":
        _confirmed(session, package, revision, read)
    else:
        _corroborated(session, package, revision, read)
    _typed_before(session, revision, **{"CT-DEPTH-001__countertop_depth": typed})

    _run(session, revision)
    finding = _findings(client, package)["CT-DEPTH-001"]

    assert finding["outcome"] == "REVIEW_REQUIRED", finding
    assert f"({typed_text})" in finding["reason"], finding["reason"]
    assert f"({read_text})" in finding["reason"], finding["reason"]
    assert "2026-10-01" in finding["reason"]
    assert "a person decides" in finding["reason"]
    assert NO_LONGER_COUNTS not in finding["reason"]


@pytest.mark.parametrize(
    "typed_mm",
    [
        # 647.7 mm is exactly 25 1/2": the same length, but not the same number and unit.
        Fraction(6477, 10),
        # The same number as the reading, in the other unit: only the unit check tells them apart.
        Fraction(51, 2),
    ],
    ids=["same-length-in-mm", "same-number-in-mm"],
)
def test_a_value_typed_in_mm_never_agrees_with_an_inch_reading(
    session: Session, typed_mm: Fraction
) -> None:
    """#1307: a reading agrees with an old typed value only when both the number and the unit are
    the same (`workflow.measurements.agrees`). A depth typed in mm beside a confirmed reading of
    25 1/2" (which alone would PASS) is never taken as agreeing, so the reading never decides alone:
    the check waits for a person (REVIEW_REQUIRED). Inches are the authoritative unit; mm never
    decides, and never lends its number to an inch reading."""
    client = _client(session)
    package, revision = _package(session)
    _depth_settings(client, package)
    _confirmed(session, package, revision, '25 1/2"')
    _typed_before_in(session, revision, "mm", {"CT-DEPTH-001__countertop_depth": typed_mm})

    _run(session, revision)
    finding = _findings(client, package)["CT-DEPTH-001"]

    assert finding["outcome"] == "REVIEW_REQUIRED", finding
    assert "(25 1/2 in)" in finding["reason"], finding["reason"]
    assert " mm)" in finding["reason"], finding["reason"]
    assert "a person decides" in finding["reason"]


def test_agrees_needs_the_same_number_and_the_same_unit() -> None:
    """#1307: `agrees` directly. Same number and unit agrees; same number in another unit, or the
    same length written in the other unit, does not."""
    from units.measurement import Measurement
    from workflow.measurements import TypedDrawn, agrees

    when = datetime(2026, 10, 1, tzinfo=UTC)
    reading = Measurement(Fraction(51, 2), Unit.INCH, '25 1/2"')

    def typed(value: Fraction, unit: Unit) -> TypedDrawn:
        return TypedDrawn(value=Measurement(value, unit, None), set_at=when)

    assert agrees(typed(Fraction(51, 2), Unit.INCH), reading)
    assert not agrees(typed(Fraction(51, 2), Unit.MM), reading)
    assert not agrees(typed(Fraction(6477, 10), Unit.MM), reading)
    assert not agrees(TypedDrawn(value=None, set_at=when), reading)


def test_a_reading_used_is_never_called_a_value_that_no_longer_counts(session: Session) -> None:
    """The back-offset check, waiting for its front offset: its countertop depth was typed before
    and is also read and confirmed, the same number, so the reading is used and the check's reason
    says nothing about the depth no longer counting (#1303 review, item 3)."""
    client = _client(session)
    package, revision = _package(session)
    _confirmed(session, package, revision, '25 1/2"')
    _typed_before(
        session, revision, **{"CT-BACK-OFFSET-MIN-001__countertop_depth": Fraction(51, 2)}
    )

    _run(session, revision)
    finding = _findings(client, package)["CT-BACK-OFFSET-MIN-001"]

    assert finding["outcome"] == "NOT_FOUND", finding
    assert NO_LONGER_COUNTS not in finding["reason"], finding["reason"]


def test_two_readings_that_cannot_be_used_are_not_answered_with_confirm_a_reading(
    session: Session,
) -> None:
    """Two confirmed depth readings on one drawing, and an old typed depth: the check waits, says the
    typed value no longer counts, and says the readings found could not be used, not "Confirm a
    reading of it" (#1303 review, item 4)."""
    client = _client(session)
    package, revision = _package(session)
    _depth_settings(client, package)
    _confirmed(session, package, revision, '25 1/2"', '26"')
    _typed_before(session, revision, **{"CT-DEPTH-001__countertop_depth": Fraction(51, 2)})

    _run(session, revision)
    finding = _findings(client, package)["CT-DEPTH-001"]

    assert finding["outcome"] == "NOT_FOUND", finding
    assert NO_LONGER_COUNTS in finding["reason"], finding["reason"]
    assert "Confirm a reading" not in finding["reason"]
    assert "could not be used" in finding["reason"]


def test_readings_withheld_for_their_own_reason_are_left_out_of_the_note(
    session: Session,
) -> None:
    """Depth readings on two different drawings are withheld (they may be different countertops), so
    the check waits for a person with that reason; it does not add "no longer counts, confirm a
    reading" about the typed depth (#1303 review, item 4)."""
    client = _client(session)
    package, revision = _package(session)
    _depth_settings(client, package)
    _confirmed(session, package, revision, '25 1/2"')
    _confirmed(session, package, revision, '25 1/2"')
    _typed_before(session, revision, **{"CT-DEPTH-001__countertop_depth": Fraction(51, 2)})

    _run(session, revision)
    finding = _findings(client, package)["CT-DEPTH-001"]

    assert finding["outcome"] == "REVIEW_REQUIRED", finding
    assert "different drawings" in finding["reason"], finding["reason"]
    assert NO_LONGER_COUNTS not in finding["reason"]


def test_typed_inputs_that_are_not_drawn_are_unchanged(session: Session) -> None:
    """The site width (USER_INPUT), a typed list (cabinet runs) and a run setting typed beside an
    old drawn value all still count, exactly as typed; only the drawn one is left out."""
    _package_id, revision = _package(session)
    _typed_before(
        session,
        revision,
        **{
            "CAB-FILLER-001__field_width": Fraction(120),
            "CT-WIDTH-001__cabinet_widths#0": Fraction(30),
            "CT-WIDTH-001__cabinet_widths#1": Fraction(36),
            "CT-WIDTH-001__countertop_width": Fraction(66),
            "sink_interior_width": Fraction(15),
        },
    )

    typed = operands_for(session, revision)

    field = typed["CAB-FILLER-001"]["field_width"]
    assert field.value.exact == 120  # type: ignore[union-attr]
    run = typed["CT-WIDTH-001"]["cabinet_widths"]
    assert [m.exact for m in run.value] == [30, 36]  # type: ignore[union-attr]
    assert "countertop_width" not in typed["CT-WIDTH-001"]
    layer = run_parameters_for(session, revision)
    assert layer is not None
    assert layer.parameters["sink_interior_width"].value.value == 15


def test_the_sentence_is_plain_and_says_what_to_do() -> None:
    """The reason is one plain sentence a reviewer can act on: what happened, and what clears it."""
    sentence = TYPED_DRAWN_STOPPED.format(inputs="countertop_depth")
    assert NO_LONGER_COUNTS in sentence
    assert "Confirm a reading" in sentence
    assert "{" not in sentence
