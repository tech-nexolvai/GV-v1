"""A setting is proposed as a pointer to a passage, never from the vendor's drawing (#849).

Verification for: `workflow/parameter_proposals.py`, `app/models/parameter_proposals.py`, migration
`0057_parameter_proposals`, `vocabulary/parameter_terms.py`, and `confirmed_views_only` in
`app/evidence/sides.py`.

The Q10 guards come first, one test each, in the order the issue lists them. The one that matters
most is `test_a_vendor_side_run_is_refused`: a passage on the vendor's drawing must never become a
setting the vendor's drawing is then checked against.

Every sheet here is synthetic: a 200 x 100 pt page with a few invented notes on it, read by the real
extraction stage, so the runs, their boxes and their transforms are the ones production stores. The
left and right halves are recorded as drawings the way the panel step records a stamp.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import tempfile
import typing
from collections.abc import Iterator
from dataclasses import fields
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.documents import storage_key
from app.db.session import session_factory
from app.evidence.sides import ReadingSides, SideRefusalReason
from app.models import (
    Document,
    DocumentVersion,
    DrawingView,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Page,
    ParameterProposal,
    Project,
    SourceArtifact,
    ViewRole,
)
from app.models.parameter_proposals import CLAIMABLE_SOURCES_SQL
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from retrieval.package_text import PhraseGrouping, build_package_phrases, search_package_text
from rules.parameter_sources import ALLOWED_SOURCES, citable_sources
from rules.parameters import Provenance
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.extraction.test_annotations import _free_text
from tests.extraction.test_annotations import _pdf as _annotated_pdf
from tests.extraction.test_reader import _pdf
from vocabulary.parameter_terms import PARAMETER_TERMS, search_terms
from vocabulary.semantic_types import DocumentRole
from workflow.idempotency import stage_idempotency_key
from workflow.parameter_proposals import (
    PROPOSER,
    PROPOSER_VERSION,
    Citation,
    CitationRefusal,
    CitationRefusalReason,
    ProposalOutcome,
    SettingProposal,
    _read_inch_dimension,
    _span,
    check_proposal,
    current_parameter_proposals,
    propose_setting,
)
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages
from workflow.view_roles import confirm_view_role, record_panel_view

pytest_plugins = ("tests.app.postgres_fixture",)

#: The deployment's stated gap (`scripts/demo.sh`, the admin's decision of 2026-10-03), so these
#: sheets' words join the way the client's do.
GROUPING = PhraseGrouping(gap_line_heights=Decimal("0.34"))

OVERHANG = "countertop_overhang"

LEFT_HALF = (
    (Decimal(0), Decimal(0)),
    (Decimal("0.5"), Decimal(0)),
    (Decimal("0.5"), Decimal(1)),
    (Decimal(0), Decimal(1)),
)
RIGHT_HALF = (
    (Decimal("0.5"), Decimal(0)),
    (Decimal(1), Decimal(0)),
    (Decimal(1), Decimal(1)),
    (Decimal("0.5"), Decimal(1)),
)
WHOLE = (
    (Decimal(0), Decimal(0)),
    (Decimal(1), Decimal(0)),
    (Decimal(1), Decimal(1)),
    (Decimal(0), Decimal(1)),
)

#: Where a note sits: on the left half's lines, or the right half's.
LEFT, RIGHT = 10, 110


def _sheet(*notes: tuple[int, int, str]) -> bytes:
    """A 200 x 100 pt page with each `(x, y, text)` written in 6 pt Helvetica."""
    return _pdf(
        b"".join(
            b"BT /F1 6 Tf 1 0 0 1 %d %d Tm (%s) Tj ET\n" % (x, y, text.encode("latin-1"))
            for x, y, text in notes
        )
    )


# ---------------------------------------------------------------------------
# Reading the span: exactly one inch-marked number, never stored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "inches"),
    [('1 1/2"', Fraction(3, 2)), ('24"', Fraction(24)), ("3/4 in", Fraction(3, 4))],
)
def test_one_inch_marked_number_is_read_exactly(text: str, inches: Fraction) -> None:
    assert _read_inch_dimension(text) == inches


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        # Millimetres are converted by `normalise_to_inches`, so they are refused before it.
        ("38 mm", CitationRefusalReason.MILLIMETRES),
        ("984mm", CitationRefusalReason.MILLIMETRES),
        ("1 1/2", CitationRefusalReason.NO_INCH_MARK),
        ("3'", CitationRefusalReason.NO_INCH_MARK),
        ('1" 2"', CitationRefusalReason.NOT_ONE_NUMBER),
        ('2 4"', CitationRefusalReason.NOT_ONE_NUMBER),
        ("3'-6\"", CitationRefusalReason.NOT_ONE_NUMBER),
        ('A1 1"', CitationRefusalReason.NOT_ONE_NUMBER),
        ('1/0"', CitationRefusalReason.UNREADABLE),
        ("TYP", CitationRefusalReason.NO_NUMBER),
    ],
)
def test_anything_but_one_inch_dimension_is_refused(
    text: str, reason: CitationRefusalReason
) -> None:
    refused = _read_inch_dimension(text)

    assert isinstance(refused, CitationRefusal)
    assert refused.reason is reason
    # The words a reviewer is shown never quote the drawing.
    assert text not in refused.detail


@pytest.mark.parametrize(
    ("runs", "span"),
    [
        (["OVERHANG", '1 1/2"', "TYP."], (1, 1)),
        (["OVERHANG", "1", '1/2"'], (1, 2)),
        # A reader that stores the unit as its own word still has it seen.
        (["BACKSPLASH", "984", "mm"], (1, 2)),
        # A word between two numbers is inside the span, which makes it unreadable.
        (['1"', "OVERHANG", '2"'], (0, 2)),
        (["OVERHANG", "TYP."], None),
    ],
)
def test_the_span_runs_from_the_first_digit_to_the_last(
    runs: list[str], span: tuple[int, int] | None
) -> None:
    assert _span(runs) == span


def test_no_public_result_carries_the_number() -> None:
    """**The schema is the guarantee**, as it is for `PhraseHit`. A result is ids, positions, an
    outcome and words; nothing typed to hold a measurement. A new field fails here first."""
    numeric = (Fraction, Decimal, float)
    for result in (SettingProposal, Citation, CitationRefusal):
        hints = typing.get_type_hints(result)
        for name, hint in hints.items():
            assert not any(kind in typing.get_args(hint) or hint is kind for kind in numeric), name

    assert [field.name for field in fields(Citation)] == [
        "setting",
        "phrase_id",
        "first_member",
        "last_member",
        "claimed_source",
        "candidate_ids",
    ]
    assert int not in {
        typing.get_type_hints(SettingProposal)[f.name] for f in fields(SettingProposal)
    }


# ---------------------------------------------------------------------------
# The search words
# ---------------------------------------------------------------------------


def test_the_search_words_are_words_only() -> None:
    """No digit, and no inch mark beyond a quoted phrase's own quotes: a term finds a passage and
    never suggests what it should say."""
    for setting, terms in PARAMETER_TERMS.items():
        assert terms, setting
        for term in terms:
            assert term.strip() and not any(character.isdigit() for character in term), term
            assert '"' not in term.strip('"'), f"{term} quotes part of itself"


def test_only_a_setting_that_may_be_cited_has_search_words() -> None:
    """A company standard with search words would be one search from being read off a drawing."""
    for setting in PARAMETER_TERMS:
        assert citable_sources(setting), setting
    assert search_terms("field_cut") == ()
    assert search_terms("not_a_setting") == ()


# ---------------------------------------------------------------------------
# The stored row
# ---------------------------------------------------------------------------


def _migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0057_parameter_proposals.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0057", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_admits_exactly_the_sources_the_model_admits() -> None:
    """The migration writes the list out and the model derives it; this holds them together."""
    assert _migration().CLAIMABLE_SOURCES == CLAIMABLE_SOURCES_SQL == "'G.C / Client'"


# ---------------------------------------------------------------------------
# Stored packages
# ---------------------------------------------------------------------------


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _document(
    session: Session,
    store: LocalStore,
    revision: PackageRevision,
    *,
    kind: str,
    data: bytes,
) -> None:
    document = Document(package_id=revision.package_id, kind=kind)
    session.add(document)
    session.flush()
    digest = hashlib.sha256(data).hexdigest()
    key = storage_key(document.id, digest)
    store.put(key, io.BytesIO(data), content_type="application/pdf")
    artifact = SourceArtifact(storage_key=key, sha256=digest, size=len(data))
    session.add(artifact)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=1
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=revision.package_id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    session.flush()


def _read(
    session: Session, store: LocalStore, sheet: bytes, *, kind: str, also: str | None = None
) -> PackageRevision:
    """The sheet uploaded as one document of `kind` and read, with its phrases built. Given `also`,
    a second drawing of that kind is in the package, the way a two-PDF package arrives."""
    project = Project(name=f"parameter proposals {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.EXTRACTING
    )
    session.add(revision)
    session.flush()
    _document(session, store, revision, kind=kind, data=sheet)
    if also is not None:
        _document(session, store, revision, kind=also, data=_sheet((LEFT, 50, "ELEVATION")))
    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    session.add(
        TaskRun(
            workflow_run_id=workflow_run.id,
            idempotency_key=stage_idempotency_key(
                package_revision_id=revision.id,
                stage="extract_pages",
                engine_version=ENGINE_VERSION,
            ),
            task_type="extract_pages",
            attempt=1,
            outcome="claimed",
        )
    )
    session.flush()
    DatabaseStages(store).extract_pages(session, revision.id)
    build_package_phrases(session, revision.id, GROUPING)
    return revision


def _page(session: Session, revision: PackageRevision, sheet: bytes) -> Page:
    return session.execute(
        select(Page)
        .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == revision.id,
            DocumentVersion.sha256 == hashlib.sha256(sheet).hexdigest(),
        )
    ).scalar_one()


def _drawings(
    session: Session, revision: PackageRevision, sheet: bytes, *roles: ViewRole | None
) -> list[DrawingView]:
    """The sheet's two halves as drawings, given two roles, or the whole sheet as one, given one.
    `None` leaves a drawing for a person to confirm."""
    regions = (LEFT_HALF, RIGHT_HALF) if len(roles) == 2 else (WHOLE,)
    views = []
    for index, (region, role) in enumerate(zip(regions, roles, strict=True)):
        view = record_panel_view(
            session,
            page_id=_page(session, revision, sheet).id,
            annotation_index=index,
            stored_points=region,
            proposed_role=None,
            heading=None,
            reason="a drawing on the test sheet",
        )
        if role is not None:
            confirm_view_role(session, view=view, role=role, actor="a reviewer")
        views.append(view)
    return views


def _run(session: Session, revision: PackageRevision, words: str) -> ObservationCandidate:
    """The page's own text run that says `words`."""
    return session.execute(
        select(ObservationCandidate)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == ObservationCandidate.document_version_id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == revision.id,
            ExtractionRun.extractor == "pdfplumber",
            ObservationCandidate.raw_text == words,
        )
    ).scalar_one()


def _rows(session: Session) -> list[ParameterProposal]:
    return list(
        session.scalars(
            select(ParameterProposal).order_by(ParameterProposal.created_at, ParameterProposal.id)
        )
    )


def _refusals(result: SettingProposal) -> set[CitationRefusalReason]:
    return {refusal.reason for refusal in result.refusals}


# ---------------------------------------------------------------------------
# The Q10 guards
# ---------------------------------------------------------------------------


def test_a_run_from_a_confirmed_architect_view_is_accepted(
    session: Session, store: LocalStore
) -> None:
    """**Outcome: one pointer, to the architect's passage, holding no number.** The vendor's half
    states a different overhang; it is refused, so it is not a disagreement."""
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2" TYP.'), (RIGHT, 70, 'OVERHANG 1"'))
    revision = _read(session, store, sheet, kind="shop")
    _drawings(session, revision, sheet, ViewRole.ARCH, ViewRole.SHOP)

    result = propose_setting(session, revision.id, OVERHANG)

    assert result.outcome is ProposalOutcome.PROPOSED
    assert _refusals(result) == {CitationRefusalReason.WRONG_SIDE}
    (row,) = _rows(session)
    assert row.id == result.proposal_id
    assert (row.setting_name, row.claimed_source) == (OVERHANG, Provenance.GC_CLIENT.value)
    assert (row.proposer, row.proposer_version) == (PROPOSER, PROPOSER_VERSION)
    assert (row.first_member, row.last_member) == (1, 1)

    citation = check_proposal(session, row)
    assert isinstance(citation, Citation)
    assert citation.candidate_ids == (_run(session, revision, '1 1/2"').id,)
    assert current_parameter_proposals(session, revision.id) == {OVERHANG: row}


@pytest.mark.parametrize("drawings", ["confirmed", "none"])
def test_a_vendor_side_run_is_refused(session: Session, store: LocalStore, drawings: str) -> None:
    """**The point of the issue.** Outcome: nothing proposed from the vendor's drawing, whether a
    person confirmed it as the vendor's or the upload says the whole file is."""
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2"'))
    revision = _read(session, store, sheet, kind="shop")
    if drawings == "confirmed":
        _drawings(session, revision, sheet, ViewRole.SHOP)

    result = propose_setting(session, revision.id, OVERHANG)

    assert result.outcome is ProposalOutcome.NOT_FOUND
    assert _refusals(result) == {CitationRefusalReason.WRONG_SIDE}
    assert "vendor's drawing" in result.refusals[0].detail
    assert _rows(session) == []


def test_a_vendors_label_beside_an_architects_number_is_refused(
    session: Session, store: LocalStore
) -> None:
    """One line across the boundary: `OVERHANG` ends on the vendor's half, `1 1/2"` starts on the
    architect's. The number alone would pass. Outcome: refused, because the passage is not the
    architect saying what the number is."""
    sheet = _sheet((65, 70, 'OVERHANG 1 1/2"'))
    revision = _read(session, store, sheet, kind="shop")
    _drawings(session, revision, sheet, ViewRole.SHOP, ViewRole.ARCH)
    sides = ReadingSides(session)
    assert sides.of(_run(session, revision, "OVERHANG")) is DocumentRole.SHOP
    assert sides.of(_run(session, revision, '1 1/2"')) is DocumentRole.ARCH

    result = propose_setting(session, revision.id, OVERHANG)

    assert result.outcome is ProposalOutcome.NOT_FOUND
    assert _refusals(result) == {CitationRefusalReason.WRONG_SIDE}
    assert _rows(session) == []


@pytest.mark.parametrize("package", ["combined sheet", "two-PDF package"])
def test_an_unconfirmed_view_is_refused(session: Session, store: LocalStore, package: str) -> None:
    """Outcome: refused, saying a person must confirm the drawing — **even in a two-PDF package**,
    where the form-filler would take the upload's word for it (admin, 2026-10-01). A setting does
    not."""
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2"'))
    if package == "combined sheet":
        revision = _read(session, store, sheet, kind="architectural")
        _drawings(session, revision, sheet, None, None)
    else:
        revision = _read(session, store, sheet, kind="architectural", also="shop")
        _drawings(session, revision, sheet, None)
        # The difference is real: without the flag, this run is the architect's.
        run = _run(session, revision, '1 1/2"')
        assert ReadingSides(session).of(run) is DocumentRole.ARCH

    result = propose_setting(session, revision.id, OVERHANG)

    assert result.outcome is ProposalOutcome.NOT_FOUND
    assert {refusal.side_refusal for refusal in result.refusals} == {
        SideRefusalReason.VIEW_ROLE_UNCONFIRMED
    }
    assert _rows(session) == []


def test_a_reviewers_markup_is_refused(session: Session, store: LocalStore) -> None:
    """A reviewer's note writing a number on the architect's drawing is the reviewer's, not the
    architect's (#802). Outcome: refused as markup."""
    sheet = _annotated_pdf(annotations=[_free_text('OVERHANG 1 1/2"', rect=b"[40 40 160 60]")])
    revision = _read(session, store, sheet, kind="architectural")
    _drawings(session, revision, sheet, ViewRole.ARCH)

    result = propose_setting(session, revision.id, OVERHANG)

    assert result.outcome is ProposalOutcome.NOT_FOUND
    assert {refusal.side_refusal for refusal in result.refusals} == {SideRefusalReason.MARKUP}
    assert _rows(session) == []


def test_a_company_standard_setting_is_refused_from_any_run(
    session: Session, store: LocalStore
) -> None:
    """A company standard, the field cut and the fabricator's clearance may be cited from nothing.
    Outcome: refused before any search, and a pointer to a confirmed architect's passage is refused
    whatever source it claims."""
    sheet = _sheet((LEFT, 70, 'FIELD CUT 1"'))
    revision = _read(session, store, sheet, kind="shop")
    _drawings(session, revision, sheet, ViewRole.ARCH)
    (hit,) = search_package_text(session, revision.id, '"field cut"')
    uncitable = sorted(setting for setting in ALLOWED_SOURCES if not citable_sources(setting))
    assert {"field_cut", "front_offset_required", "sink_cutout_clearance"} <= set(uncitable)

    for setting in uncitable:
        result = propose_setting(session, revision.id, setting)
        assert result.outcome is ProposalOutcome.REFUSED, setting
        assert "never read off a package" in result.note

        for source in Provenance:
            forged = ParameterProposal(
                package_revision_id=revision.id,
                setting_name=setting,
                phrase_id=hit.phrase_id,
                first_member=2,
                last_member=2,
                claimed_source=source.value,
                proposer=PROPOSER,
                proposer_version=PROPOSER_VERSION,
            )
            refused = check_proposal(session, forged)
            assert isinstance(refused, CitationRefusal), (setting, source)
            assert refused.reason is CitationRefusalReason.NOT_CITABLE
    assert _rows(session) == []


@pytest.mark.parametrize(
    ("note", "reason"),
    [
        ("OVERHANG 38 mm", CitationRefusalReason.MILLIMETRES),
        ("OVERHANG 1 1/2", CitationRefusalReason.NO_INCH_MARK),
        ('OVERHANG 1" OR 2"', CitationRefusalReason.NOT_ONE_NUMBER),
    ],
)
def test_mm_no_unit_or_two_numbers_are_refused(
    session: Session, store: LocalStore, note: str, reason: CitationRefusalReason
) -> None:
    """On the architect's confirmed drawing, so the only thing wrong is the number."""
    sheet = _sheet((LEFT, 70, note))
    revision = _read(session, store, sheet, kind="architectural")
    _drawings(session, revision, sheet, ViewRole.ARCH)

    result = propose_setting(session, revision.id, OVERHANG)

    assert result.outcome is ProposalOutcome.NOT_FOUND
    assert _refusals(result) == {reason}
    assert _rows(session) == []


def test_two_different_values_in_one_package_give_no_proposal(
    session: Session, store: LocalStore
) -> None:
    """Outcome: no pointer, a REVIEW note that quotes neither value, and both passages named."""
    sheet = _sheet((LEFT, 80, 'OVERHANG 1 1/2"'), (LEFT, 50, 'OVERHANG 1"'))
    revision = _read(session, store, sheet, kind="architectural")
    _drawings(session, revision, sheet, ViewRole.ARCH)

    result = propose_setting(session, revision.id, OVERHANG)

    assert result.outcome is ProposalOutcome.REVIEW
    assert result.proposal_id is None and len(result.phrase_ids) == 2
    assert '1"' not in result.note and "1/2" not in result.note
    assert _rows(session) == []


def test_the_same_value_twice_is_one_proposal(session: Session, store: LocalStore) -> None:
    sheet = _sheet((LEFT, 80, 'OVERHANG 1 1/2"'), (LEFT, 50, 'OVERHANG 1.5"'))
    revision = _read(session, store, sheet, kind="architectural")
    _drawings(session, revision, sheet, ViewRole.ARCH)

    result = propose_setting(session, revision.id, OVERHANG)

    assert result.outcome is ProposalOutcome.PROPOSED
    assert len(_rows(session)) == 1


def test_the_stored_row_carries_no_value(session: Session) -> None:
    """**No value column**, as `measurement_proposals`. The table holds where, never what: no
    number, no unit, no text, and no side, which is decided at use."""
    columns = set(
        session.scalars(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'parameter_proposals'"
            )
        )
    )
    assert columns == {
        "id",
        "created_at",
        "package_revision_id",
        "setting_name",
        "phrase_id",
        "first_member",
        "last_member",
        "claimed_source",
        "proposer",
        "proposer_version",
    }


def test_the_database_refuses_a_source_that_may_cite_nothing(
    session: Session, store: LocalStore
) -> None:
    """The guard in code has a twin in the schema: a row claiming a company standard is refused."""
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2"'))
    revision = _read(session, store, sheet, kind="architectural")
    (hit,) = search_package_text(session, revision.id, "overhang")
    session.commit()

    for source, span in (
        (Provenance.COMPANY_STANDARD.value, (1, 1)),
        (Provenance.GC_CLIENT.value, (1, 0)),
    ):
        session.add(
            ParameterProposal(
                package_revision_id=revision.id,
                setting_name=OVERHANG,
                phrase_id=hit.phrase_id,
                first_member=span[0],
                last_member=span[1],
                claimed_source=source,
                proposer=PROPOSER,
                proposer_version=PROPOSER_VERSION,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


# ---------------------------------------------------------------------------
# The newest wins, and the side is decided at use
# ---------------------------------------------------------------------------


def test_the_newest_proposal_wins_and_an_unchanged_one_is_not_filed_twice(
    session: Session, store: LocalStore
) -> None:
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2"'))
    revision = _read(session, store, sheet, kind="architectural")
    _drawings(session, revision, sheet, ViewRole.ARCH)

    first = propose_setting(session, revision.id, OVERHANG)
    again = propose_setting(session, revision.id, OVERHANG)
    assert again.proposal_id == first.proposal_id
    assert len(_rows(session)) == 1

    # A rebuild under another gap is a new build, with new phrases: a new pointer, which wins.
    build_package_phrases(session, revision.id, PhraseGrouping(gap_line_heights=Decimal("0.35")))
    newer = propose_setting(session, revision.id, OVERHANG)

    older, newest = _rows(session)
    assert (older.id, newest.id) == (first.proposal_id, newer.proposal_id)
    assert older.phrase_id != newest.phrase_id
    assert current_parameter_proposals(session, revision.id) == {OVERHANG: newest}


def test_a_pointer_is_withdrawn_when_its_drawing_is_confirmed_as_the_vendors(
    session: Session, store: LocalStore
) -> None:
    """**The side is decided at use, never stored.** A person corrects the drawing's role after the
    pointer was filed. Outcome: the pointer no longer holds, and the guard says why."""
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2"'))
    revision = _read(session, store, sheet, kind="architectural")
    (view,) = _drawings(session, revision, sheet, ViewRole.ARCH)
    propose_setting(session, revision.id, OVERHANG)
    (row,) = _rows(session)

    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="a reviewer")

    refused = check_proposal(session, row)
    assert isinstance(refused, CitationRefusal)
    assert refused.reason is CitationRefusalReason.WRONG_SIDE
    assert current_parameter_proposals(session, revision.id) == {}


def test_a_later_disagreement_withdraws_the_pointer(session: Session, store: LocalStore) -> None:
    """The second passage is on a drawing nobody had confirmed when the pointer was filed. Once it
    is confirmed as the architect's, the package states two values. Outcome: no current pointer,
    and proposing again says REVIEW."""
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2"'), (RIGHT, 70, 'OVERHANG 1"'))
    revision = _read(session, store, sheet, kind="architectural")
    _, right = _drawings(session, revision, sheet, ViewRole.ARCH, None)
    assert propose_setting(session, revision.id, OVERHANG).outcome is ProposalOutcome.PROPOSED

    confirm_view_role(session, view=right, role=ViewRole.ARCH, actor="a reviewer")

    assert current_parameter_proposals(session, revision.id) == {}
    assert propose_setting(session, revision.id, OVERHANG).outcome is ProposalOutcome.REVIEW


def test_a_package_never_indexed_finds_nothing(session: Session, store: LocalStore) -> None:
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2"'))
    revision = _read(session, store, sheet, kind="architectural")
    other = PackageRevision(
        package_id=revision.package_id, revision_number=2, state=PackageState.EXTRACTING
    )
    session.add(other)
    session.flush()

    assert propose_setting(session, other.id, OVERHANG).outcome is ProposalOutcome.NOT_FOUND
    with pytest.raises(ValueError, match="no package revision"):
        propose_setting(session, uuid4(), OVERHANG)


def test_a_pointer_to_another_revisions_passage_is_refused(
    session: Session, store: LocalStore
) -> None:
    sheet = _sheet((LEFT, 70, 'OVERHANG 1 1/2"'))
    revision = _read(session, store, sheet, kind="architectural")
    _drawings(session, revision, sheet, ViewRole.ARCH)
    propose_setting(session, revision.id, OVERHANG)
    (row,) = _rows(session)
    elsewhere = ParameterProposal(
        package_revision_id=uuid4(),
        setting_name=OVERHANG,
        phrase_id=row.phrase_id,
        first_member=row.first_member,
        last_member=row.last_member,
        claimed_source=row.claimed_source,
        proposer=PROPOSER,
        proposer_version=PROPOSER_VERSION,
    )

    refused = check_proposal(session, elsewhere)

    assert isinstance(refused, CitationRefusal)
    assert refused.reason is CitationRefusalReason.OUTSIDE_REVISION
