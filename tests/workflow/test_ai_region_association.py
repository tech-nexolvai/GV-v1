"""An AI reading attached to the dimension line its region labels, or refused, and why (#918).

Verification for: `workflow/stages.py` — `region_placement`, `RegionReading`, `_exact_text_regions`,
`_held_to_their_regions`, `DatabaseStages._ai_region_inputs` and the association step that takes
them — and, through `workflow/reading_parts.py`, what #913's suggester makes of the attachment.

**The outcomes that matter most:** a region two lines fit is attached to neither, with the reason;
a region whose own characters touch the line, or that lies past its ends, is refused; the reading's
value is never touched; and nothing the AI reader said places the region.

The drawings are hand-built as in `tests/workflow/test_association.py`: a stamp whose appearance maps
to the page by `page = (appearance_x - 50, appearance_y - 450)`, on a 400 × 300 point page, so every
distance can be checked by hand. A label is drawn as glyph-sized strokes, as the client's vendor
draws its numbers. No client drawing or value is used.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.evidence.confirm import confirm_candidate_type
from app.evidence.record import open_extraction_run
from app.models import (
    DrawingView,
    ObservationAssociation,
    ObservationCandidate,
    PackageRevision,
    Page,
)
from app.models.drawing import ViewRole
from app.models.runs import ExtractionRun, TaskRun
from evidence.coordinates import ImagePoint, PdfPoint, StoredPoint
from evidence.polygon import Polygon
from extraction.agent.geometry import NO_PATHS
from extraction.annotations import PageLayers, StackedFraction, read_annotation_layers
from extraction.geometry.containment import DimensionExtent
from extraction.geometry.text_association import (
    AssociationResult,
    DimensionText,
    TextAssociation,
    associate,
)
from extraction.ocr import OcrItem
from storage.local import LocalStore
from tests.extraction.test_annotations import _appearance, _pdf, _stamp
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_association import (
    LOCALIZED,
    SETTINGS,
    _association_vision_reader,
    _revision,
)
from tests.workflow.test_markup_route import _SilentOcr
from units.measurement import Unit
from vocabulary.part_kinds import PartKind
from workflow.parts import confirm_part, live_parts_on, record_part_proposal
from workflow.reading_agent import ReadingAgentSettings
from workflow.reading_parts import (
    PlacedReading,
    ReadingGeometry,
    readings_on,
    suggest_links,
)
from workflow.stages import (
    ASSOCIATION_EXTRACTOR,
    BEYOND_ITS_ENDS,
    PLACED_BY_THE_VENDORS_PATHS,
    TOUCHES_ITS_LINE,
    UNPLACED_EXACT_TEXT,
    UNPLACED_NO_PATHS_READ,
    UNPLACED_NO_REACH,
    UNPLACED_STACKED,
    DatabaseStages,
    RegionReading,
    _exact_text_regions,
    _held_to_their_regions,
    page_transform,
    region_placement,
)
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

DPI = 150
#: #913's stated edge tolerance in the demo (`GV_RUN_EDGE_TOLERANCE`), in stored units.
TOLERANCE = Decimal("0.004")
ACTOR = "reviewer@example.com"
READER = "association-vision-test"
#: The two lengths a label is gathered by, as the reading agent states them.
AGENT = ReadingAgentSettings(
    max_steps=6,
    max_vlm_escalations=0,
    sharper_dpi=2 * DPI,
    primary_reader=READER,
    escalation_reader=None,
    label_gap_pt=Decimal(4),
    maximum_label_pt=Decimal(40),
)


# ---------------------------------------------------------------------------
# Drawings
# ---------------------------------------------------------------------------


def _twelve(x: int, y: int) -> bytes:
    """`12` drawn as two glyph-sized strokes, its lower left at appearance `(x, y)`: the `1` a
    7-point upright, the `2` 4 points wide, 2 points to its right — one run across the page."""
    return (
        f"1 w {x + 1} {y} m {x + 1} {y + 7} l S\n"
        f"1 w {x + 3} {y + 7} m {x + 7} {y + 7} l {x + 7} {y + 4} l {x + 3} {y} l {x + 7} {y} l S\n"
    ).encode()


def _dimension(y: int, start: int, end: int, *, witnesses: tuple[int, int]) -> bytes:
    """A horizontal dimension at appearance `y` from `start` to `end`, its two witness lines at
    `witnesses` crossing it 30 points either side."""
    left, right = witnesses
    return (
        f"1 w {start} {y} m {end} {y} l S\n"
        f"1 w {left} {y - 30} m {left} {y + 30} l S\n"
        f"1 w {right} {y - 30} m {right} {y + 30} l S\n"
    ).encode()


def _sheet(stream: bytes) -> bytes:
    return _pdf(annotations=[_stamp(appearance_object=6)], extra_objects=[_appearance(stream)])


#: **The page-2 shape (#918).** A part between witness lines at page x=100 and x=160; its dimension
#: at page y=100 runs a point past each, page x=99..161, as the client's lines overshoot their
#: witnesses (it is 62 points long: shorter, and the reader would not take it for line-work); its
#: number `12` is printed above it, page x=127..133, y=105..112. The number's box falls far short of
#: the part's ends in stored units, past the tolerance; the line's ends are 0.0025 from the part's,
#: inside it.
FILLER = _sheet(_dimension(550, 149, 211, witnesses=(150, 210)) + _twelve(176, 555))

#: A number standing next to the ticks at its dimension's left end, within a label gap of them —
#: as a narrow filler's number does on the client's drawings — but printed clear of the line: its
#: gathered label reaches the ticks, the number itself does not touch the line.
BESIDE_THE_TICKS = _sheet(
    _dimension(550, 149, 211, witnesses=(150, 210))
    + b"1 w 148 547 m 152 553 l S\n"
    + _twelve(153, 555)
)

#: `FILLER` with its number drawn in red: a mark GV drew inside the pasted drawing, not the vendor's.
IN_RED = _sheet(_dimension(550, 149, 211, witnesses=(150, 210)) + b"1 0 0 RG\n" + _twelve(176, 555))

#: The same number between two dimensions stacked 16 points apart (page y=100 and y=116), both
#: spanning it, one pair of witness lines crossing both: two lines fit, so neither is chosen.
TWO_ROWS = _sheet(
    b"1 w 149 550 m 251 550 l S\n"
    b"1 w 149 566 m 251 566 l S\n"
    b"1 w 150 520 m 150 596 l S\n"
    b"1 w 250 520 m 250 596 l S\n" + _twelve(196, 554)
)

#: Two ticks drawn across the dimension's right end, a run of two glyph-sized strokes as a label's
#: characters are: the shape a gate reader read as `7` on the client's drawing.
TICKS = _sheet(
    _dimension(550, 149, 251, witnesses=(150, 250))
    + b"1 w 246 547 m 249 553 l S\n1 w 251 547 m 254 553 l S\n"
)

#: A number printed past the dimension's right end, page x=212..219 against a line ending at 201.
PAST_THE_END = _sheet(_dimension(550, 149, 251, witnesses=(150, 250)) + _twelve(262, 555))


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _upgrade(engine: Engine) -> None:
    from alembic import command
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    from app.db.session import session_factory

    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(tmp_path: object) -> LocalStore:
    from pathlib import Path

    return LocalStore(root=Path(str(tmp_path)), ticket_secret=b"a secret only this test knows")


def _stages(store: LocalStore, *, agent: ReadingAgentSettings | None = AGENT) -> DatabaseStages:
    return DatabaseStages(
        store,
        dpi=DPI,
        association=SETTINGS,
        ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
        vision_readers=(_association_vision_reader(),),
        reading_agent=agent,
        missing_space=MISSING_SPACE,
    )


class Sheet:
    """One hand-built sheet read by the pipeline with no AI reader, then given AI readings."""

    def __init__(self, session: Session, store: LocalStore, data: bytes) -> None:
        self.session = session
        self.data = data
        self.revision: PackageRevision = _revision(session, store, data=data)
        session.commit()
        DatabaseStages(
            store,
            dpi=DPI,
            association=SETTINGS,
            ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
            vision_readers=(),
            missing_space=MISSING_SPACE,
        ).extract_pages(session, self.revision.id)
        session.commit()
        self.page = session.scalars(select(Page)).one()
        self.task_run = session.scalars(select(TaskRun)).one()
        self.layers: PageLayers = read_annotation_layers(
            data,
            0,
            document_version_id=self.page.document_version_id,
            dpi=DPI,
            line_minimum_pt=SETTINGS.line_minimum_pt,
            glyph_maximum_pt=SETTINGS.glyph_maximum_pt,
            glyph_gap_pt=SETTINGS.glyph_gap_pt,
            fraction_bar=SETTINGS.fraction_bar,
        )
        self.run = open_extraction_run(
            session,
            task_run_id=self.task_run.id,
            extractor=READER,
            extractor_version="test/1",
            config_hash="route=vision;test",
            dpi=DPI,
        )

    def pixels(self, left: str, bottom: str, right: str, top: str) -> list[list[int]]:
        """A page-point box as the pixel polygon a reading records."""
        transform = page_transform(self.page, DPI)
        assert transform is not None
        corners = [
            transform.to_image(PdfPoint(Decimal(x), Decimal(y)))
            for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))
        ]
        return [[point.x, point.y] for point in corners]

    def ai_reading(self, polygon: list[list[int]], value: int | None = 12) -> ObservationCandidate:
        """What an AI reader recorded for a region: its value, at the region's own polygon."""
        row = ObservationCandidate(
            document_version_id=self.page.document_version_id,
            page_id=self.page.id,
            extraction_run_id=self.run.id,
            raw_text="(none)" if value is None else f'{value}"',
            value_numerator=value,
            value_denominator=None if value is None else 1,
            unit=None if value is None else Unit.INCH.value,
            unit_guess=Unit.INCH.value,
            semantic_guess=None,
            polygon=polygon,
            coordinate_space="image",
            confidence=Decimal("0.9"),
            ambiguity_flags=[],
        )
        self.session.add(row)
        self.session.flush()
        return row

    def attach(
        self,
        stages: DatabaseStages,
        rows: Sequence[ObservationCandidate],
        *,
        exact_text: frozenset[tuple[tuple[int, int], ...]] = frozenset(),
        stacked: Sequence[StackedFraction] | None = None,
        layers: PageLayers | None = None,
    ) -> dict[str, int]:
        """Place `rows` by their regions and run the association step on them, as a page does."""
        items, placed, refused = stages._ai_region_inputs(
            page=self.page,
            rows=rows,
            associated=frozenset(),
            exact_text=exact_text,
            layers=self.layers if layers is None else layers,
            stacked_fractions=self.layers.stacked_fractions if stacked is None else stacked,
        )
        stages._associate_page(
            self.session,
            page=self.page,
            task_run_id=self.task_run.id,
            readings=((items, placed),),
            lines=self.layers.drawing_segments,
            placements={row.id: item for item, row in zip(items, placed, strict=True)},
        )
        self.session.flush()
        return dict(refused)

    def decisions(self, row: ObservationCandidate) -> list[ObservationAssociation]:
        return list(
            self.session.scalars(
                select(ObservationAssociation).where(ObservationAssociation.candidate_id == row.id)
            )
        )


LABEL = ("126", "105", "133", "112")
"""The `12` on `FILLER`, in page points: left, bottom, right, top."""


# ---------------------------------------------------------------------------
# Attaching, through the same association the drawing's own text uses
# ---------------------------------------------------------------------------


def test_an_ai_reading_is_attached_to_the_line_its_region_labels(
    session: Session, store: LocalStore
) -> None:
    """Input: an AI reading recorded at the box round `12`, which no other route placed. Outcome:
    attached to the dimension below it, saying first that the place and direction are the region's
    and the vendor's characters', never the AI reader's.

    **The #918 case.** Before this, such a reading was handed to the association only through an OCR
    box whose layout had settled a direction, which on the client's drawing none had."""
    sheet = Sheet(session, store, FILLER)
    row = sheet.ai_reading(sheet.pixels(*LABEL))

    refused = sheet.attach(_stages(store), [row])

    assert refused == {}
    (decision,) = sheet.decisions(row)
    assert decision.refusal_reason is None
    assert decision.signals[0] == PLACED_BY_THE_VENDORS_PATHS.format(direction="across the page")
    assert decision.signals[1:]  # `associate`'s own reasons follow
    assert decision.start_y == decision.end_y
    assert Decimal(decision.start_y or "0") == pytest.approx(
        Decimal(200) / 300, abs=Decimal("0.002")
    )
    ends = sorted(Decimal(value or "0") for value in (decision.start_x, decision.end_x))
    assert ends[0] == pytest.approx(Decimal("0.2475"), abs=Decimal("0.0005"))
    assert ends[1] == pytest.approx(Decimal("0.4025"), abs=Decimal("0.0005"))


def test_the_value_is_never_changed(session: Session, store: LocalStore) -> None:
    """Outcome: the reading's row reads exactly as the reader recorded it after the attachment, the
    attachment row carries no number, and a person confirming it confirms the reader's value."""
    sheet = Sheet(session, store, FILLER)
    row = sheet.ai_reading(sheet.pixels(*LABEL), value=12)
    before = (row.raw_text, row.value_numerator, row.value_denominator, row.unit, row.polygon)

    sheet.attach(_stages(store), [row])
    session.expire_all()

    again = session.get(ObservationCandidate, row.id)
    assert again is not None
    assert (
        again.raw_text,
        again.value_numerator,
        again.value_denominator,
        again.unit,
        again.polygon,
    ) == before
    (decision,) = sheet.decisions(again)
    assert not any("value" in column.name for column in ObservationAssociation.__table__.columns)
    assert decision.candidate_id == row.id


def test_two_lines_either_side_attach_none_and_say_why(session: Session, store: LocalStore) -> None:
    """Input: the number midway between two stacked dimensions that both span it. Outcome: refused,
    the reason naming the ambiguity margin, and both lines kept as what the choice was between.
    **Never the nearer of the two.**"""
    sheet = Sheet(session, store, TWO_ROWS)
    row = sheet.ai_reading(sheet.pixels("147", "104", "154", "111"))

    sheet.attach(_stages(store), [row])

    (decision,) = sheet.decisions(row)
    assert decision.start_x is None and decision.signals == []
    assert decision.refusal_reason is not None
    assert "ambiguity margin" in decision.refusal_reason
    assert decision.candidate_lines is not None and len(decision.candidate_lines) == 2


def test_a_region_whose_characters_touch_the_line_is_refused(
    session: Session, store: LocalStore
) -> None:
    """Input: an AI reading at two ticks drawn across a dimension's end. Outcome: refused — they
    are drawn on the line, not printed beside it — naming the line it would have been attached to.
    """
    sheet = Sheet(session, store, TICKS)
    row = sheet.ai_reading(sheet.pixels("196", "97", "204", "103"), value=7)

    sheet.attach(_stages(store), [row])

    (decision,) = sheet.decisions(row)
    assert decision.refusal_reason == TOUCHES_ITS_LINE
    assert decision.start_x is None
    assert decision.candidate_lines is not None and len(decision.candidate_lines) == 1


def test_a_region_printed_past_its_lines_end_is_refused(
    session: Session, store: LocalStore
) -> None:
    """**#913's rule, held at the association** (admin decision 2026-10-04). Input: a number printed
    past the end of the only line near it. Outcome: refused, not attached to a line it may not label;
    a dimension's number is printed along its own line."""
    sheet = Sheet(session, store, PAST_THE_END)
    row = sheet.ai_reading(sheet.pixels("212", "105", "219", "112"))

    sheet.attach(_stages(store), [row])

    (decision,) = sheet.decisions(row)
    assert decision.refusal_reason == BEYOND_ITS_ENDS


def test_a_number_beside_the_ticks_at_its_lines_end_is_attached(
    session: Session, store: LocalStore
) -> None:
    """Outcome: attached. **The region's own characters decide whether it touches the line**, not
    the whole label gathered from them: here the gathered label takes in the tick at the line's end,
    which crosses the line, while the number itself stands clear of it."""
    sheet = Sheet(session, store, BESIDE_THE_TICKS)
    row = sheet.ai_reading(sheet.pixels("104", "105", "110", "112"))

    sheet.attach(_stages(store), [row])

    (decision,) = sheet.decisions(row)
    assert decision.refusal_reason is None


# ---------------------------------------------------------------------------
# Not placed, and why
# ---------------------------------------------------------------------------


def test_a_region_of_the_files_own_text_is_not_placed_again(
    session: Session, store: LocalStore
) -> None:
    """Outcome: an AI reading at the polygon of a reading the file states exactly is not handed to
    the association — that reading carries its own attachment — and the reason is counted."""
    sheet = Sheet(session, store, FILLER)
    polygon = sheet.pixels(*LABEL)
    row = sheet.ai_reading(polygon)
    stamp_text = replace_polygon(row, polygon)

    refused = sheet.attach(_stages(store), [row], exact_text=_exact_text_regions([stamp_text]))

    assert refused == {UNPLACED_EXACT_TEXT: 1}
    assert sheet.decisions(row) == []


def replace_polygon(row: ObservationCandidate, polygon: list[list[int]]) -> ObservationCandidate:
    """A row of another route at the same polygon. Not persisted: only its polygon is read."""
    return ObservationCandidate(
        document_version_id=row.document_version_id,
        page_id=row.page_id,
        extraction_run_id=row.extraction_run_id,
        raw_text="12",
        polygon=polygon,
        coordinate_space="image",
        ambiguity_flags=[],
    )


def test_without_the_lengths_a_label_is_gathered_by_nothing_is_placed(
    session: Session, store: LocalStore
) -> None:
    """Outcome: with no reading agent stated, nothing gathers a label, so nothing is placed — a
    direction is never defaulted — and the reason names the two settings."""
    sheet = Sheet(session, store, FILLER)
    row = sheet.ai_reading(sheet.pixels(*LABEL))

    refused = sheet.attach(_stages(store, agent=None), [row])

    assert refused == {UNPLACED_NO_REACH: 1}
    assert sheet.decisions(row) == []


def test_a_page_whose_paths_were_not_read_places_nothing(
    session: Session, store: LocalStore
) -> None:
    sheet = Sheet(session, store, FILLER)
    row = sheet.ai_reading(sheet.pixels(*LABEL))
    unread = replace(sheet.layers, geometry_read=False)

    refused = sheet.attach(_stages(store), [row], layers=unread)

    assert refused == {UNPLACED_NO_PATHS_READ: 1}


def test_a_mark_in_colour_is_not_the_vendors_and_places_nothing(
    session: Session, store: LocalStore
) -> None:
    """Outcome: a number drawn in red inside the pasted drawing — GV's own mark — gives the region no
    characters of the vendor's, so nothing places it."""
    sheet = Sheet(session, store, IN_RED)
    row = sheet.ai_reading(sheet.pixels(*LABEL))

    refused = sheet.attach(_stages(store), [row])

    assert refused == {NO_PATHS: 1}
    assert sheet.decisions(row) == []


def test_a_stacked_fraction_in_the_label_places_nothing(
    session: Session, store: LocalStore
) -> None:
    """Outcome: where the bar detector found a stacked fraction in the label, its runs say nothing
    about which way it reads, so it is not placed (the rule a vision crop is held to)."""
    sheet = Sheet(session, store, FILLER)
    polygon = sheet.pixels(*LABEL)
    row = sheet.ai_reading(polygon)
    transform = page_transform(sheet.page, DPI)
    assert transform is not None
    fraction = StackedFraction(
        extent=Polygon(
            points=tuple(transform.to_stored(ImagePoint(x, y)) for x, y in polygon),
            space="stored",
            document_version_id=sheet.page.document_version_id,
            page=0,
        ),
        image_extent=tuple(ImagePoint(x, y) for x, y in polygon),
        layout=None,
    )

    refused = sheet.attach(_stages(store), [row], stacked=[fraction])

    assert refused == {UNPLACED_STACKED: 1}


def test_a_reading_with_no_value_is_not_placed(session: Session, store: LocalStore) -> None:
    sheet = Sheet(session, store, FILLER)
    row = sheet.ai_reading(sheet.pixels(*LABEL), value=None)

    refused = sheet.attach(_stages(store), [row])

    assert refused == {} and sheet.decisions(row) == []


def test_the_association_run_names_the_lengths_a_label_is_gathered_by(
    session: Session, store: LocalStore
) -> None:
    """Outcome: an association made with AI readings placed by gathered labels is its own run, whose
    identity carries both lengths — a re-association under others is another run (#487)."""
    sheet = Sheet(session, store, FILLER)
    row = sheet.ai_reading(sheet.pixels(*LABEL))

    sheet.attach(_stages(store), [row])

    (decision,) = sheet.decisions(row)
    run = session.get(ExtractionRun, decision.extraction_run_id)
    assert run is not None and run.extractor == ASSOCIATION_EXTRACTOR
    assert run.config_hash.endswith(";label_gap<=4;label<=40")
    assert run.config_hash.startswith(f"dpi={DPI};{SETTINGS.config_hash}")


# ---------------------------------------------------------------------------
# What #913's suggester makes of it
# ---------------------------------------------------------------------------


def test_a_page_2_shaped_filler_is_suggested_once_its_ai_reading_is_attached(
    session: Session, store: LocalStore
) -> None:
    """**The #918 outcome.** Input: the narrow part on `FILLER`, confirmed by a person; the AI
    reading of its number, attached by its region and confirmed by a person. Outcome: suggested as
    the part's width by its line, whose ends lie within the tolerance of the part's — though the box
    the number is printed in falls well short of them, so by its region alone it would not be."""
    sheet = Sheet(session, store, FILLER)
    row = sheet.ai_reading(sheet.pixels(*LABEL))
    sheet.attach(_stages(store), [row])
    view = session.scalars(select(DrawingView).where(DrawingView.page_id == sheet.page.id)).one()
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor=ACTOR)
    observation = confirm_candidate_type(
        session, candidate_id=row.id, semantic_type="filler_width", confirmed_by=ACTOR
    )
    assert not hasattr(observation, "reason"), observation
    proposal = record_part_proposal(
        session,
        drawing_view_id=view.id,
        kind=PartKind.FILLER,
        extent=[(Decimal("0.25"), Decimal(Y)), (Decimal("0.40"), Decimal(Y))],
        defining_line=((Decimal("0.25"), Decimal(Y)), (Decimal("0.40"), Decimal(Y))),
        code_as_printed=None,
        code_candidate_id=None,
        reason="added by a person",
        source="test",
        source_version="1",
    )
    confirm_part(session, proposal=proposal, kind=PartKind.FILLER, code=None, actor=ACTOR)

    (reading,) = readings_on(session, view)
    (suggestion,) = suggest_links(
        live_parts_on(session, view.id), [reading], edge_tolerance=TOLERANCE
    )

    assert reading.geometry is ReadingGeometry.LINE
    assert (
        suggestion.reading is not None
        and suggestion.reading.observation_id == reading.observation_id
    )
    by_region = replace(
        reading, geometry=ReadingGeometry.REGION, left=reading.region[0], right=reading.region[2]
    )
    (unattached,) = suggest_links(
        live_parts_on(session, view.id), [by_region], edge_tolerance=TOLERANCE
    )
    assert unattached.reading is None


Y = str(Decimal(200) / 300)
"""The dimension's height on `FILLER`, stored: page y=100 on a 300-point page."""


# ---------------------------------------------------------------------------
# The checks, by themselves: they only ever take an attachment away
# ---------------------------------------------------------------------------

DOCUMENT = uuid4()


def _point(x: str, y: str) -> StoredPoint:
    return StoredPoint(Decimal(x), Decimal(y))


def _line(x1: str, y1: str, x2: str, y2: str) -> DimensionExtent:
    return DimensionExtent(
        start=_point(x1, y1), end=_point(x2, y2), document_version_id=DOCUMENT, page=0
    )


def _region(
    left: str, top: str, right: str, bottom: str, *, degrees: int = 0, drawn: bool = True
) -> RegionReading:
    box = (Decimal(left), Decimal(top), Decimal(right), Decimal(bottom))
    return RegionReading(
        extent=Polygon(
            points=(
                _point(left, top),
                _point(right, top),
                _point(right, bottom),
                _point(left, bottom),
            ),
            space="stored",
            document_version_id=DOCUMENT,
            page=0,
        ),
        rotation_degrees=degrees,
        drawn=box,
        signal="placed by its region",
    )


def _decide(
    regions: dict[UUID, RegionReading], lines: tuple[DimensionExtent, ...]
) -> AssociationResult:
    texts = tuple(
        DimensionText(
            observation_id=key, extent=region.extent, rotation_degrees=region.rotation_degrees
        )
        for key, region in regions.items()
    )
    found = associate(
        texts,
        lines,
        proximity_limit=SETTINGS.proximity_limit,
        ambiguity_margin=SETTINGS.ambiguity_margin,
    )
    return _held_to_their_regions(found, regions)


#: A narrow part's line, overshooting its witness lines, with its number printed above it — and
#: the next dimension in the chain, starting just before the shared witness, as on the client's
#: page 2: its end lies within the ambiguity margin of the number's centre.
OWN = _line("0.4000", "0.5000", "0.4160", "0.5000")
NEIGHBOUR = _line("0.4113", "0.5000", "0.4920", "0.5000")
NUMBER = ("0.4043", "0.4900", "0.4085", "0.4982")


def test_a_number_on_a_narrow_part_alone_on_its_line_is_attached() -> None:
    key = uuid4()

    result = _decide({key: _region(*NUMBER)}, (OWN,))

    (attached,) = result.associated
    assert attached.line == OWN
    assert attached.signals[0] == "placed by its region"


def test_the_same_number_beside_a_chained_neighbour_is_refused_not_given_either() -> None:
    """**What happens on the client's page 2.** The neighbour's end lies within the ambiguity margin
    of the number, so two lines fit and neither is chosen — even though the number is printed between
    only one of their ends. Choosing by that would be a different association from the one the
    drawing's own text uses, and is an admin's decision (#918)."""
    key = uuid4()

    result = _decide({key: _region(*NUMBER)}, (OWN, NEIGHBOUR))

    assert result.associated == ()
    (refused,) = result.unassociated
    assert "ambiguity margin" in refused.reason
    assert set(refused.candidates) == {OWN, NEIGHBOUR}


def test_a_vertical_number_beside_a_vertical_line_is_attached() -> None:
    key = uuid4()
    line = _line("0.5000", "0.3000", "0.5000", "0.6000")

    result = _decide({key: _region("0.4900", "0.4400", "0.4960", "0.4600", degrees=90)}, (line,))

    (attached,) = result.associated
    assert attached.line == line


def test_a_vertical_number_past_its_lines_end_is_refused() -> None:
    key = uuid4()
    line = _line("0.5000", "0.3000", "0.5000", "0.4000")

    result = _decide({key: _region("0.4900", "0.4050", "0.4960", "0.4200", degrees=90)}, (line,))

    (refused,) = result.unassociated
    assert refused.reason == BEYOND_ITS_ENDS and refused.candidates == (line,)


def test_the_checks_leave_every_other_reading_as_associate_decided() -> None:
    """Outcome: a reading the drawing's own text gave — not an AI reading placed by its region — is
    attached exactly as before, signals and all, even printed past its line's end; a refusal stays a
    refusal."""
    own_text = uuid4()
    stray = uuid4()
    texts = (
        DimensionText(
            observation_id=own_text,
            extent=_region("0.5050", "0.4900", "0.5100", "0.4980").extent,
            rotation_degrees=0,
        ),
        DimensionText(
            observation_id=stray,
            extent=_region("0.9000", "0.1000", "0.9100", "0.1100").extent,
            rotation_degrees=0,
        ),
    )
    found = associate(
        texts,
        (OWN, NEIGHBOUR),
        proximity_limit=SETTINGS.proximity_limit,
        ambiguity_margin=SETTINGS.ambiguity_margin,
    )

    held = _held_to_their_regions(found, {})

    assert held == found
    assert [entry.text.observation_id for entry in held.associated] == [own_text]


def test_a_region_whose_own_characters_cross_the_line_is_refused_but_its_label_may_reach_it() -> (
    None
):
    """Outcome: the characters in the region itself decide, not the whole label gathered from them:
    a narrow part's number stands within a label gap of the ticks at its line's ends, and is not
    drawn on the line."""
    key = uuid4()
    on_it = replace(
        _region(*NUMBER),
        drawn=(Decimal("0.4043"), Decimal("0.4990"), Decimal("0.4085"), Decimal("0.5010")),
    )

    (refused,) = _decide({key: on_it}, (OWN,)).unassociated

    assert refused.reason == TOUCHES_ITS_LINE
    assert refused.candidates == (OWN,)


def test_an_attachment_is_not_rebuilt_for_a_reading_with_no_region() -> None:
    key = uuid4()
    association = TextAssociation(
        text=DimensionText(observation_id=key, extent=_region(*NUMBER).extent, rotation_degrees=0),
        line=OWN,
        signals=("a reason",),
    )
    result = AssociationResult(associated=(association,), unassociated=())

    assert _held_to_their_regions(result, {uuid4(): _region(*NUMBER)}) == result


# ---------------------------------------------------------------------------
# The page, end to end
# ---------------------------------------------------------------------------


class _LabelOcr:
    """OCR that sees `12` filling the middle of whatever crop it is shown, with no unit and no
    direction: the box a vision reader is then pointed at. It never reads a number for the page."""

    name = "label-ocr"
    version = "test/1"

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        del rgb
        return (
            OcrItem(
                text="12",
                confidence=Decimal("0.8"),
                image_extent=(
                    ImagePoint(width // 4, height // 4),
                    ImagePoint(3 * width // 4, height // 4),
                    ImagePoint(3 * width // 4, 3 * height // 4),
                    ImagePoint(width // 4, 3 * height // 4),
                ),
            ),
        )


def test_a_page_attaches_its_vision_readings_by_their_regions_and_says_how_many(
    session: Session, store: LocalStore
) -> None:
    """**The wiring.** Input: `FILLER` read by localized OCR, which sees `12` with no direction, and
    by a vision reader. Outcome: the vision reading is attached to the dimension by its region, and
    the page says how many AI readings were placed so and how many were not."""
    revision = _revision(session, store, data=FILLER)
    session.commit()
    results = DatabaseStages(
        store,
        dpi=DPI,
        association=SETTINGS,
        localized_ocr=LOCALIZED,
        ocr_engine=_LabelOcr(),  # type: ignore[arg-type]
        vision_readers=(_association_vision_reader(),),
        reading_agent=AGENT,
        missing_space=MISSING_SPACE,
    ).extract_pages(session, revision.id)
    session.commit()

    (result,) = results
    assert result.payload["route"] == "localized_ocr"
    vision_runs = {
        run.id for run in session.scalars(select(ExtractionRun)) if run.extractor == READER
    }
    vision = [
        row
        for row in session.scalars(select(ObservationCandidate))
        if row.extraction_run_id in vision_runs and row.value_numerator is not None
    ]
    assert vision
    decisions = [
        decision
        for decision in session.scalars(select(ObservationAssociation))
        if decision.candidate_id in {row.id for row in vision}
    ]
    assert decisions and all(decision.refusal_reason is None for decision in decisions)
    assert all(
        decision.signals[0] == PLACED_BY_THE_VENDORS_PATHS.format(direction="across the page")
        for decision in decisions
    )
    placed = result.payload["ai_readings_placed_by_region"]
    assert isinstance(placed, int) and placed == len(decisions)
    assert result.payload["ai_readings_not_placed"] == 0
    assert result.payload["ai_readings_not_placed_reasons"] == []
    # The value is the reader's, exactly: the double reads `24"` whatever it is shown.
    assert {(row.value_numerator, row.value_denominator) for row in vision} == {(24, 1)}


class _DualOcr:
    """OCR that reads a millimetre-and-inch label in every crop, its two halves stacked: the layout
    that settles a direction (`combine_dual_notation`), so the reading is placed by OCR itself."""

    name = "dual-ocr"
    version = "test/1"

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        del rgb, width, height
        return (
            OcrItem(
                text="76",
                confidence=Decimal("0.81"),
                image_extent=(
                    ImagePoint(10, 10),
                    ImagePoint(50, 10),
                    ImagePoint(50, 30),
                    ImagePoint(10, 30),
                ),
            ),
            OcrItem(
                text="[3]",
                confidence=Decimal("0.77"),
                image_extent=(
                    ImagePoint(8, 28),
                    ImagePoint(52, 28),
                    ImagePoint(52, 52),
                    ImagePoint(8, 52),
                ),
            ),
        )


def test_a_vision_reading_placed_by_its_source_is_not_placed_again(
    session: Session, store: LocalStore
) -> None:
    """Outcome: a vision reading whose region OCR's own layout already placed is associated through
    that source, as before #918, and once: it is neither counted nor handed to the association again
    by its region."""
    revision = _revision(session, store, data=FILLER)
    session.commit()
    (result,) = DatabaseStages(
        store,
        dpi=DPI,
        association=SETTINGS,
        localized_ocr=LOCALIZED,
        ocr_engine=_DualOcr(),  # type: ignore[arg-type]
        vision_readers=(_association_vision_reader(),),
        reading_agent=AGENT,
        missing_space=MISSING_SPACE,
    ).extract_pages(session, revision.id)
    session.commit()

    vision_runs = {
        run.id
        for run in session.scalars(select(ExtractionRun))
        if run.extractor == READER and "route=vision" in run.config_hash
    }
    (vision,) = [
        row
        for row in session.scalars(select(ObservationCandidate))
        if row.extraction_run_id in vision_runs
    ]
    decisions = list(
        session.scalars(
            select(ObservationAssociation).where(ObservationAssociation.candidate_id == vision.id)
        )
    )
    assert len(decisions) == 1
    assert not decisions[0].signals or decisions[0].signals[
        0
    ] != PLACED_BY_THE_VENDORS_PATHS.format(direction="across the page")
    assert result.payload["ai_readings_placed_by_region"] == 0


def test_without_association_settings_the_page_says_the_step_did_not_run(
    session: Session, store: LocalStore
) -> None:
    revision = _revision(session, store, data=FILLER)
    session.commit()
    (result,) = DatabaseStages(
        store,
        dpi=DPI,
        ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
        vision_readers=(),
        missing_space=MISSING_SPACE,
    ).extract_pages(session, revision.id)

    assert result.payload["ai_readings_placed_by_region"] is None
    assert result.payload["ai_readings_not_placed"] is None
    assert result.payload["ai_readings_not_placed_reasons"] is None


def test_region_placement_never_reads_the_reader() -> None:
    """A guard on the signature: the placement takes a polygon and the page's facts, and no value,
    text or box an AI reader produced can reach it."""
    import inspect

    parameters = set(inspect.signature(region_placement).parameters)

    assert parameters == {
        "polygon_px",
        "transform",
        "document_version_id",
        "page_index",
        "page_glyphs",
        "stacked_fractions",
        "reach",
    }


def test_placed_readings_are_suggested_only_by_lines_they_are_printed_between() -> None:
    """**#913's rule stays** (admin decision 2026-10-04): a reading the association attached to a
    line it is printed beyond counts by nothing, whatever reader read it."""
    part_left, part_right = Decimal("0.40"), Decimal("0.416")
    from workflow.parts import PlacedPart

    part = PlacedPart(
        item_id=uuid4(),
        kind=PartKind.FILLER,
        view_id=uuid4(),
        left=part_left,
        right=part_right,
        top=Decimal("0.50"),
        bottom=Decimal("0.50"),
    )
    beyond = PlacedReading(
        observation_id=uuid4(),
        page_id=uuid4(),
        region=(Decimal("0.43"), Decimal("0.49"), Decimal("0.44"), Decimal("0.498")),
        geometry=ReadingGeometry.LINE,
        left=Decimal("0.4000"),
        right=Decimal("0.4160"),
        unplaced=None,
    )

    (suggestion,) = suggest_links([part], [beyond], edge_tolerance=TOLERANCE)

    assert suggestion.reading is None
    assert "set aside" in suggestion.said
