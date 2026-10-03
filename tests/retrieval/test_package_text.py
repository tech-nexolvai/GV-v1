"""A search over a package's own words returns passages, never numbers (#836).

Verification for: `retrieval/package_text.py`, `app/models/package_text.py` and migration
`0056_text_phrases`.

Two halves. The first needs no database: it holds the grouping rule to what the module says — one
line, one route, a gap measured in line heights, markup on its own — on runs written by hand. The
second builds phrases from rows in PostgreSQL and searches them, because the properties that matter
most are properties of the stored rows: nothing from another package, nothing a model or an OCR
engine read, no layout anchor, and a result with nowhere to put a number.

Every page here is synthetic: invented words on an invented sheet, in image pixels.
"""

from __future__ import annotations

import typing
from collections.abc import Iterator
from dataclasses import fields
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    Document,
    DocumentVersion,
    EvidenceArtifact,
    LayoutProposal,
    ObservationAssociation,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Page,
    PhraseTag,
    Project,
    SourceArtifact,
    TextPhrase,
    TextPhraseMember,
)
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from retrieval.package_text import (
    EXACT_TEXT_ROUTES,
    PackageTextError,
    PhraseGrouping,
    PhraseHit,
    TextRun,
    build_package_phrases,
    group_runs,
    search_package_text,
)
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

#: A synthetic setting, chosen for these pages. It is not the deployment's value, which is measured
#: on the client's drawings and stated where the worker runs.
GROUPING = PhraseGrouping(gap_line_heights=Decimal("0.3"))

VECTOR = "pdfplumber"
MARKUP = "extraction.annotations"
STAMP = "extraction.stamp_text"


# ---------------------------------------------------------------------------
# Grouping, on runs written by hand
# ---------------------------------------------------------------------------

PAGE = uuid4()
RUN = uuid4()


def _run(
    text: str,
    box: tuple[int, int, int, int] | None,
    *,
    route: str = VECTOR,
    run: UUID = RUN,
    attached: bool = False,
) -> TextRun:
    return TextRun(
        candidate_id=uuid4(),
        page_id=PAGE,
        page_index=0,
        extraction_run_id=run,
        route=route,
        text=text,
        box=box,
        attached=attached,
    )


def _contents(runs: list[TextRun]) -> list[str]:
    return [phrase.content for phrase in group_runs(runs, GROUPING)]


def test_two_notes_on_one_baseline_far_apart_are_not_glued() -> None:
    """**The case the issue names.** Two notes share a line; each note's words are a quarter of a
    line height apart, and the notes are thirty line heights apart. Two phrases, not one."""
    runs = [
        _run("FILLER", (100, 500, 160, 520)),
        _run("PANEL", (165, 500, 215, 520)),
        _run("LED", (815, 500, 845, 520)),
        _run("STRIP", (850, 500, 900, 520)),
    ]

    assert _contents(runs) == ["FILLER PANEL", "LED STRIP"]


def test_words_are_joined_left_to_right_whatever_order_they_were_stored_in() -> None:
    runs = [
        _run("PANEL", (165, 500, 215, 520)),
        _run("ACCESS", (100, 500, 160, 520)),
    ]

    assert _contents(runs) == ["ACCESS PANEL"]


def test_the_gap_boundary_is_exact() -> None:
    """At 0.3 of a 20-pixel line the limit is 6 pixels: 6 joins and 7 does not. Exact arithmetic,
    because the boundary the setting draws must be the one applied."""
    at_limit = [_run("A", (0, 0, 10, 20)), _run("B", (16, 0, 26, 20))]
    past_limit = [_run("A", (0, 0, 10, 20)), _run("B", (17, 0, 27, 20))]

    assert _contents(at_limit) == ["A B"]
    assert _contents(past_limit) == ["A", "B"]


def test_runs_on_different_lines_are_not_joined() -> None:
    """A different top or bottom is a different line, or a different font: never one phrase."""
    runs = [
        _run("TOP", (100, 500, 160, 520)),
        _run("NEXT", (165, 525, 215, 545)),
        _run("TALLER", (220, 500, 280, 524)),
    ]

    assert _contents(runs) == ["TOP", "TALLER", "NEXT"]


def test_overlapping_runs_are_not_joined() -> None:
    """Two strings printed in one place are not one after the other."""
    runs = [_run("EQ", (100, 500, 140, 520)), _run("EQ", (130, 500, 170, 520))]

    assert _contents(runs) == ["EQ", "EQ"]


def test_a_reviewers_note_is_a_phrase_on_its_own() -> None:
    """Two annotations touching on one line are still two notes the reviewer wrote separately."""
    runs = [
        _run("CHECK", (100, 500, 160, 520), route=MARKUP),
        _run("SINK", (162, 500, 210, 520), route=MARKUP),
    ]

    phrases = group_runs(runs, GROUPING)

    assert [phrase.content for phrase in phrases] == ["CHECK", "SINK"]
    assert {phrase.tag for phrase in phrases} == {PhraseTag.MARKUP}


def test_two_routes_on_one_line_are_never_one_phrase() -> None:
    """The vendor's pasted drawing and the page's own text are read by different routes, and a
    phrase records one route. Touching on one line does not make them one passage."""
    other = uuid4()
    runs = [
        _run("BASE", (100, 500, 160, 520)),
        _run("CABINET", (162, 500, 230, 520), route=STAMP, run=other),
    ]

    assert sorted(_contents(runs)) == ["BASE", "CABINET"]


def test_a_run_without_a_box_is_kept_and_never_joined() -> None:
    """It can still be found; it cannot be shown to sit beside anything."""
    runs = [_run("FIXED", (100, 500, 160, 520)), _run("PANEL", None)]

    assert sorted(_contents(runs)) == ["FIXED", "PANEL"]


def test_a_phrase_is_a_dimension_when_one_of_its_runs_is_attached() -> None:
    runs = [
        _run("5", (100, 500, 110, 520), attached=True),
        _run("FILLER", (115, 500, 175, 520)),
        _run("NOTE", (800, 500, 850, 520)),
    ]

    tags = [phrase.tag for phrase in group_runs(runs, GROUPING)]

    assert tags == [PhraseTag.DIMENSION, PhraseTag.NOTE]


def test_the_gap_has_no_default_and_refuses_what_is_not_exact() -> None:
    """No default: a deployment that has not stated the gap gets an error, not a guess."""
    with pytest.raises(TypeError):
        PhraseGrouping()  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="never a float"):
        PhraseGrouping(gap_line_heights=0.3)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        PhraseGrouping(gap_line_heights=Decimal("-0.1"))
    with pytest.raises(ValueError, match="finite"):
        PhraseGrouping(gap_line_heights=Decimal("NaN"))
    assert PhraseGrouping(gap_line_heights=Decimal(0)).rule.endswith("0 line heights")


def test_a_search_result_has_no_field_that_could_hold_a_number() -> None:
    """**The schema is the guarantee.** A hit is a phrase id and run ids. A text field could carry
    the drawing's digits; a score is a number. Neither exists, and a new field fails here first."""
    hints = typing.get_type_hints(PhraseHit)

    assert [field.name for field in fields(PhraseHit)] == ["phrase_id", "candidate_ids"]
    assert hints == {"phrase_id": UUID, "candidate_ids": tuple[UUID, ...]}

    with pytest.raises(TypeError):
        PhraseHit(phrase_id=uuid4(), candidate_ids=(3,))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PhraseHit(phrase_id=uuid4(), candidate_ids=())


def test_the_indexed_routes_are_exactly_the_exact_text_routes() -> None:
    """Restated here to keep the extraction stack out of the index, and held to the original both
    ways: a reader added there is not indexed until someone adds it here, and a name renamed there
    fails this rather than quietly emptying the index."""
    from app.evidence.sides import MARKUP_ROUTE
    from workflow.stages import EXACT_TEXT_EXTRACTORS

    assert EXACT_TEXT_ROUTES == EXACT_TEXT_EXTRACTORS
    assert MARKUP_ROUTE in EXACT_TEXT_ROUTES


# ---------------------------------------------------------------------------
# Built from stored rows, and searched
# ---------------------------------------------------------------------------


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


class _Sheet:
    """One package revision with one shop drawing of one page, and its extraction's task run."""

    def __init__(self, session: Session, name: str) -> None:
        self.session = session
        project = Project(name=f"package text {name} {uuid4()}")
        session.add(project)
        session.flush()
        package = Package(project_id=project.id)
        session.add(package)
        session.flush()
        revision = PackageRevision(
            package_id=package.id, revision_number=1, state=PackageState.EXTRACTING
        )
        session.add(revision)
        session.flush()
        document = Document(package_id=package.id, kind="shop")
        session.add(document)
        artifact = SourceArtifact(storage_key=f"s/{uuid4()}", sha256="0" * 64, size=1)
        session.add(artifact)
        session.flush()
        version = DocumentVersion(
            document_id=document.id, source_artifact_id=artifact.id, sha256="0" * 64, page_count=1
        )
        session.add(version)
        session.flush()
        session.add(
            PackageRevisionDocument(
                package_revision_id=revision.id,
                package_id=package.id,
                document_id=document.id,
                document_version_id=version.id,
            )
        )
        page = Page(
            document_version_id=version.id,
            index=0,
            content_hash="0" * 64,
            width_pt=Decimal(612),
            height_pt=Decimal(792),
            rotation=0,
            has_vector_text=True,
        )
        session.add(page)
        session.flush()
        workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
        session.add(workflow_run)
        session.flush()
        task_run = TaskRun(
            workflow_run_id=workflow_run.id,
            idempotency_key=str(uuid4()),
            task_type="extract_pages",
            attempt=1,
            outcome="SUCCEEDED",
        )
        session.add(task_run)
        session.flush()
        self.revision_id = revision.id
        self.version_id = version.id
        self.page_id = page.id
        self.task_run_id = task_run.id
        self._runs: dict[str, UUID] = {}

    def run(self, extractor: str) -> UUID:
        if extractor not in self._runs:
            run = ExtractionRun(
                task_run_id=self.task_run_id,
                extractor=extractor,
                extractor_version=f"{extractor}/1",
                config_hash="dpi=300",
                dpi=300,
            )
            self.session.add(run)
            self.session.flush()
            self._runs[extractor] = run.id
        return self._runs[extractor]

    def reading(
        self, text: str, box: tuple[int, int, int, int], *, extractor: str = VECTOR
    ) -> UUID:
        left, top, right, bottom = box
        candidate = ObservationCandidate(
            document_version_id=self.version_id,
            page_id=self.page_id,
            extraction_run_id=self.run(extractor),
            raw_text=text,
            polygon=[[left, top], [right, top], [right, bottom], [left, bottom]],
            ambiguity_flags=[],
        )
        self.session.add(candidate)
        self.session.flush()
        return candidate.id

    def attach(self, candidate_id: UUID, *, attached: bool = True) -> None:
        """An association row for the reading: attached to a line, or refused."""
        self.session.add(
            ObservationAssociation(
                candidate_id=candidate_id,
                extraction_run_id=self.run("extraction.geometry.text_association"),
                start_x="0.1" if attached else None,
                start_y="0.5" if attached else None,
                end_x="0.2" if attached else None,
                end_y="0.5" if attached else None,
                signals=["nearest line"] if attached else [],
                refusal_reason=None if attached else "two lines were equally close",
            )
        )
        self.session.flush()

    def layout_anchor(self, text: str) -> UUID:
        """A layout step's sentence, filed the way the stage files it: a candidate in the vector
        run, owning the crop its layout proposal cites."""
        candidate_id = self.reading(text, (0, 0, 4000, 3000))
        artifact = EvidenceArtifact(
            candidate_id=candidate_id,
            canonical_observation_id=None,
            document_version_id=self.version_id,
            page_id=self.page_id,
            kind="crop",
            storage_key=f"crops/{uuid4()}.png",
            sha256="a" * 64,
            media_type="image/png",
            coordinate_space="image",
        )
        self.session.add(artifact)
        self.session.flush()
        self.session.add(
            LayoutProposal(
                package_revision_id=self.revision_id,
                discriminator_name="layout_question",
                proposed_value="abstained",
                crop_artifact_id=artifact.id,
                model_id="layout-reader-unconfigured",
                prompt_id="layout-discriminator-v1",
            )
        )
        self.session.flush()
        return candidate_id


def _members(session: Session) -> set[UUID]:
    return set(session.scalars(select(TextPhraseMember.candidate_id)))


def _tags(session: Session) -> dict[tuple[UUID, ...], str]:
    """Every stored phrase's runs, in order, and its tag."""
    runs: dict[UUID, list[UUID]] = {}
    for phrase_id, candidate_id in session.execute(
        select(TextPhraseMember.phrase_id, TextPhraseMember.candidate_id).order_by(
            TextPhraseMember.phrase_id, TextPhraseMember.position
        )
    ):
        runs.setdefault(phrase_id, []).append(candidate_id)
    return {tuple(runs[phrase.id]): phrase.tag for phrase in session.scalars(select(TextPhrase))}


def test_nothing_from_another_package_is_ever_returned(session: Session) -> None:
    """**Outcome: each package finds only its own passage**, though both say the same words."""
    first = _Sheet(session, "first")
    second = _Sheet(session, "second")
    mine = first.reading("SCRIBE", (100, 500, 160, 520)), first.reading("TO", (165, 500, 185, 520))
    theirs = second.reading("SCRIBE", (100, 500, 160, 520))
    build_package_phrases(session, first.revision_id, GROUPING)
    build_package_phrases(session, second.revision_id, GROUPING)

    hits = search_package_text(session, first.revision_id, "scribe")

    assert [hit.candidate_ids for hit in hits] == [mine]
    assert theirs not in {run for hit in hits for run in hit.candidate_ids}
    phrase = session.get(TextPhrase, hits[0].phrase_id)
    assert phrase is not None and phrase.package_revision_id == first.revision_id


def test_a_row_from_another_revision_is_refused_not_returned(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces: even if the SQL lost its revision filter, the rows are checked on the way
    out. Simulated by asking the database for one revision and checking against another."""
    import retrieval.package_text as module

    sheet = _Sheet(session, "scoped")
    sheet.reading("OVERHANG", (100, 500, 190, 520))
    build_package_phrases(session, sheet.revision_id, GROUPING)
    current = module._current_build(session, sheet.revision_id)
    unfiltered = module.SEARCH_SQL.replace(":package_revision_id", "phrase.package_revision_id")
    monkeypatch.setattr(module, "_current_build", lambda *_: current)
    monkeypatch.setattr(module, "SEARCH_SQL", unfiltered)
    elsewhere = uuid4()

    with pytest.raises(PackageTextError, match="outside the requested revision"):
        search_package_text(session, elsewhere, "overhang")


def test_model_ocr_and_layout_anchor_text_never_appear(session: Session) -> None:
    """**The mutation this guards: indexing a model's reading.**

    One sheet, the same words read four ways: by the page's own text, by a vision model, by an OCR
    engine, and inside a layout step's sentence. Only the first is a passage.
    """
    sheet = _Sheet(session, "routes")
    exact = (
        sheet.reading("SINK", (100, 500, 150, 520)),
        sheet.reading("BASE", (155, 500, 205, 520)),
    )
    model = sheet.reading("SINK BASE", (100, 500, 205, 520), extractor="bedrock-nova-2-lite")
    ocr = sheet.reading("SINK BASE", (100, 500, 205, 520), extractor="rapidocr")
    anchor = sheet.layout_anchor("layout layout_question: abstained: SINK BASE")

    built = build_package_phrases(session, sheet.revision_id, GROUPING)
    hits = search_package_text(session, sheet.revision_id, "sink base")

    assert [hit.candidate_ids for hit in hits] == [exact]
    assert built.summary()["phrases"] == 1
    assert _members(session) == set(exact)
    assert not {model, ocr, anchor} & _members(session)


def test_two_notes_far_apart_on_a_stored_page_are_two_passages(session: Session) -> None:
    """The issue's case again, end to end: stored rows, built, searched."""
    sheet = _Sheet(session, "far apart")
    filler = (
        sheet.reading("FILLER", (100, 500, 160, 520)),
        sheet.reading("PANEL", (165, 500, 215, 520)),
    )
    strip = (
        sheet.reading("LED", (815, 500, 845, 520)),
        sheet.reading("STRIP", (850, 500, 900, 520)),
    )
    build_package_phrases(session, sheet.revision_id, GROUPING)

    assert [
        hit.candidate_ids for hit in search_package_text(session, sheet.revision_id, "panel")
    ] == [filler]
    assert [
        hit.candidate_ids for hit in search_package_text(session, sheet.revision_id, "led")
    ] == [strip]
    assert search_package_text(session, sheet.revision_id, '"panel led"') == ()


def test_phrases_are_tagged_markup_dimension_or_note(session: Session) -> None:
    sheet = _Sheet(session, "tags")
    note = sheet.reading("BY", (100, 500, 120, 520)), sheet.reading("OWNER", (125, 500, 185, 520))
    dimension = sheet.reading('2"', (100, 700, 120, 720))
    refused = sheet.reading('3"', (100, 900, 120, 920))
    markup = sheet.reading("CHECK OWNER", (100, 1100, 220, 1120), extractor=MARKUP)
    sheet.attach(dimension)
    sheet.attach(refused, attached=False)

    build_package_phrases(session, sheet.revision_id, GROUPING)

    assert _tags(session) == {
        note: "note",
        (dimension,): "dimension",
        (refused,): "note",
        (markup,): "markup",
    }
    owner = search_package_text(session, sheet.revision_id, "owner", tags=[PhraseTag.MARKUP])
    assert [hit.candidate_ids for hit in owner] == [(markup,)]


def test_a_word_glued_to_its_neighbour_is_found_by_its_fragment(session: Session) -> None:
    """`OVERHANGTYP` is one word to the parser, so a word search for `overhang` misses it; the
    fragment half of the search finds it."""
    sheet = _Sheet(session, "glued")
    glued = sheet.reading("OVERHANGTYP", (100, 500, 260, 520))
    sheet.reading("BOX1X2", (100, 700, 220, 720))
    build_package_phrases(session, sheet.revision_id, GROUPING)

    assert [
        hit.candidate_ids for hit in search_package_text(session, sheet.revision_id, "overhang")
    ] == [(glued,)]
    # `_` is a wildcard to `ILIKE`, and would match the `X` in `1X2`. In a query it is a character.
    assert search_package_text(session, sheet.revision_id, "1_2") == ()


def test_a_rebuild_that_decides_the_same_writes_nothing(session: Session) -> None:
    """A redelivered extraction must not double every phrase; a changed decision is a new build,
    and the search reads the newest."""
    sheet = _Sheet(session, "rebuild")
    reading = sheet.reading('5"', (100, 500, 120, 520))
    first = build_package_phrases(session, sheet.revision_id, GROUPING)
    again = build_package_phrases(session, sheet.revision_id, GROUPING)

    assert first.written and not again.written
    assert again.build_id == first.build_id
    assert len(session.scalars(select(TextPhrase)).all()) == 1

    sheet.attach(reading)
    changed = build_package_phrases(session, sheet.revision_id, GROUPING)

    assert changed.written and changed.build_id != first.build_id
    assert len(session.scalars(select(TextPhrase)).all()) == 2
    assert search_package_text(session, sheet.revision_id, "5", tags=[PhraseTag.NOTE]) == ()
    assert (
        len(search_package_text(session, sheet.revision_id, "5", tags=[PhraseTag.DIMENSION])) == 1
    )


def test_only_the_newest_read_of_a_page_is_indexed(session: Session) -> None:
    """Extraction run again reads the same words into a new run. One passage, not two."""
    sheet = _Sheet(session, "re-read")
    sheet.reading("PANTRY", (100, 500, 170, 520))
    newer = ExtractionRun(
        task_run_id=sheet.task_run_id,
        extractor=VECTOR,
        extractor_version=f"{VECTOR}/1",
        config_hash="dpi=150",
        dpi=150,
    )
    session.add(newer)
    session.flush()
    again = ObservationCandidate(
        document_version_id=sheet.version_id,
        page_id=sheet.page_id,
        extraction_run_id=newer.id,
        raw_text="PANTRY",
        polygon=[[50, 250], [85, 250], [85, 260], [50, 260]],
        ambiguity_flags=[],
    )
    session.add(again)
    session.flush()

    build_package_phrases(session, sheet.revision_id, GROUPING)

    assert [
        hit.candidate_ids for hit in search_package_text(session, sheet.revision_id, "pantry")
    ] == [(again.id,)]


def test_a_revision_never_built_has_no_phrases_and_a_blank_query_is_refused(
    session: Session,
) -> None:
    sheet = _Sheet(session, "unbuilt")
    sheet.reading("TALL", (100, 500, 150, 520))

    assert search_package_text(session, sheet.revision_id, "tall") == ()
    with pytest.raises(ValueError, match="non-empty"):
        search_package_text(session, sheet.revision_id, "   ")
    with pytest.raises(ValueError, match="no package revision"):
        build_package_phrases(session, uuid4(), GROUPING)
