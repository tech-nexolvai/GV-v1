"""The drawing's own text is not attached to the wrong dimension line (#926).

Verification for: `workflow/stages.py` — `_own_text_held_to_their_lines`, `between_its_ends`, and
`DatabaseStages._associate_page`'s `own_text`, which `_read_document` fills with the page text, the
pasted drawings' font text and the vendor's CAD notes.

**The three kinds found on `AI_Set_2`, each built from invented geometry of the same shape:**

- a narrow filler's number attached to the next cabinet's line, its own line too short to be offered:
  printed past that line's end, or within it beside the filler's short line;
- a note's number standing past the end of a panel's edge;
- a label printed sideways across a narrow panel, attached to the panel's long edge while a shorter
  stroke stands nearer.

**And the attachments that must be kept:** a number between its line's ends with nothing shorter as
near; a number set in a break in its line, which touches it (#918's touch check is not applied to the
file's own text); a shorter stroke further than the ambiguity margin; and every reading that is not
the file's own text, whatever it does.

The sheet at the end is hand-built as in `tests/workflow/test_association.py`: a stamp whose
appearance maps to the page by `page = (appearance_x - 50, appearance_y - 450)`, on a 400 × 300
point page, its labels set as font text in the stamp. No client drawing or value is used.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import ObservationAssociation, ObservationCandidate
from app.models.runs import ExtractionRun
from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon
from extraction.geometry.containment import DimensionExtent
from extraction.geometry.text_association import (
    AssociationResult,
    CannotAssociate,
    DimensionText,
    associate,
)
from storage.local import LocalStore
from tests.extraction.test_glyph_bands import GEOMETRY as FRACTION_BAR
from tests.extraction.test_reader import MISSING_SPACE
from tests.extraction.test_reader import _pdf as _content_pdf
from tests.extraction.test_stamp_text import _drawing
from tests.workflow.test_association import SETTINGS as ASSOCIATION_SETTINGS
from tests.workflow.test_association import _association_vision_reader, _revision
from tests.workflow.test_markup_route import _SilentOcr
from workflow.association import AssociationSettings
from workflow.stages import (
    ASSOCIATION_EXTRACTOR,
    ASSOCIATION_EXTRACTOR_VERSION,
    OWN_TEXT_BEYOND_ITS_ENDS,
    OWN_TEXT_SHORTER_LINE,
    DatabaseStages,
    _own_text_held_to_their_lines,
    between_its_ends,
)

pytest_plugins = ("tests.app.postgres_fixture",)

LIMIT = Decimal("0.05")
MARGIN = Decimal("0.005")
DOCUMENT = uuid4()


def _point(x: str, y: str) -> StoredPoint:
    return StoredPoint(Decimal(x), Decimal(y))


def _line(x1: str, y1: str, x2: str, y2: str) -> DimensionExtent:
    return DimensionExtent(
        start=_point(x1, y1), end=_point(x2, y2), document_version_id=DOCUMENT, page=0
    )


def _text(left: str, top: str, right: str, bottom: str, *, degrees: int = 0) -> DimensionText:
    return DimensionText(
        observation_id=uuid4(),
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
    )


def _held(
    texts: tuple[DimensionText, ...],
    offered: tuple[DimensionExtent, ...],
    shorter: tuple[DimensionExtent, ...] = (),
    *,
    own: frozenset[UUID] | None = None,
) -> tuple[AssociationResult, AssociationResult]:
    """`associate`'s own result, and the same held to its lines as the page's own text."""
    found = associate(texts, offered, proximity_limit=LIMIT, ambiguity_margin=MARGIN)
    held = _own_text_held_to_their_lines(
        found,
        frozenset(text.observation_id for text in texts) if own is None else own,
        offered=offered,
        shorter=shorter,
        proximity_limit=LIMIT,
        ambiguity_margin=MARGIN,
    )
    return found, held


# ---------------------------------------------------------------------------
# The three kinds of wrong attachment
# ---------------------------------------------------------------------------

#: The next cabinet's dimension along a chain, and a narrow filler's own line beside its end: drawn
#: as a dimension line is, but 0.0092 of the page long — under a 0.01 minimum span, so never offered.
CABINET = _line("0.3000", "0.5000", "0.4024", "0.5000")
FILLER = _line("0.4000", "0.5000", "0.4092", "0.5000")


def test_a_narrow_fillers_number_past_the_next_lines_start_is_refused() -> None:
    """**Kind 1, at a chain's end.** Input: the filler's number printed above its own short line,
    reaching past the start of the cabinet's line, the only one offered. Outcome: `associate` takes
    the cabinet's line; held to it, the number is not wholly between its ends, so it is refused,
    naming that line."""
    cabinet = _line("0.3000", "0.5000", "0.4000", "0.5000")
    number = _text("0.2950", "0.4880", "0.3010", "0.4970")

    found, held = _held((number,), (cabinet,))

    assert found.associated and found.associated[0].line == cabinet
    assert held.associated == ()
    (refused,) = held.unassociated
    assert refused.reason == OWN_TEXT_BEYOND_ITS_ENDS
    assert refused.candidates == (cabinet,)


def test_a_narrow_fillers_number_beside_its_short_line_is_refused() -> None:
    """**Kind 1, inside the next cabinet's span.** Input: the filler's number printed just before
    its own short line, wholly between the cabinet line's ends, so the ends say nothing. Outcome:
    offered as well, the short line is within the ambiguity margin of the cabinet's, so the choice is
    not made and the number is attached to neither; both lines are kept as what it was between."""
    number = _text("0.3940", "0.4900", "0.3985", "0.4975")

    found, held = _held((number,), (CABINET,), (FILLER,))

    assert found.associated and found.associated[0].line == CABINET
    assert held.associated == ()
    (refused,) = held.unassociated
    assert refused.reason == OWN_TEXT_SHORTER_LINE
    assert refused.candidates[0] == CABINET
    assert FILLER in refused.candidates


def test_a_notes_number_past_the_end_of_a_panels_edge_is_refused() -> None:
    """**Kind 2.** Input: the number that opens a note (an appliance's size, say) standing just
    past the end of a panel's edge the detector took for a dimension. Outcome: refused — it is not
    between the line's ends."""
    panel_edge = _line("0.3000", "0.5000", "0.4000", "0.5000")
    note_number = _text("0.4050", "0.4950", "0.4090", "0.5020")

    found, held = _held((note_number,), (panel_edge,))

    assert found.associated and found.associated[0].line == panel_edge
    (refused,) = held.unassociated
    assert refused.reason == OWN_TEXT_BEYOND_ITS_ENDS


def test_a_sideways_label_beside_a_panels_long_edge_is_refused() -> None:
    """**Kind 3.** Input: a label printed up the page inside a narrow panel, beside the panel's long
    edge, with a shorter stroke drawn as a dimension line is standing nearer. Outcome: offered too,
    the shorter stroke would take it, so the long edge is not taken to be its line either; both are
    kept as what it was between."""
    long_edge = _line("0.5000", "0.3000", "0.5000", "0.7000")
    shorter = _line("0.4930", "0.4950", "0.4930", "0.5030")
    label = _text("0.4880", "0.4900", "0.4920", "0.5100", degrees=90)

    found, held = _held((label,), (long_edge,), (shorter,))

    assert found.associated and found.associated[0].line == long_edge
    (refused,) = held.unassociated
    assert refused.reason == OWN_TEXT_SHORTER_LINE
    assert refused.candidates == (long_edge, shorter)


# ---------------------------------------------------------------------------
# The attachments that are kept
# ---------------------------------------------------------------------------


def test_a_number_between_its_lines_ends_with_nothing_shorter_near_is_kept() -> None:
    """Outcome: the attachment `associate` made, unchanged, signals and all."""
    number = _text("0.3400", "0.4900", "0.3600", "0.4975")

    found, held = _held((number,), (CABINET,), (FILLER,))

    assert held == found
    assert held.associated[0].line == CABINET


def test_a_number_set_in_a_break_in_its_line_is_kept_though_it_touches_it() -> None:
    """**#918's touch check is not applied to the file's own text.** Input: a number whose box the
    line passes through — `associate`'s inline placement, a dimension's number set in a break in its
    line. Outcome: kept. On `AI_Set_2` five of the six numbers that check would have refused were
    right."""
    number = _text("0.3400", "0.4960", "0.3600", "0.5040")

    found, held = _held((number,), (CABINET,))

    assert held == found
    assert len(held.associated) == 1


def test_a_shorter_stroke_further_than_the_margin_takes_nothing_away() -> None:
    """Input: the number printed over the middle of the cabinet's line, the filler's short line at
    its far end. Outcome: re-associated with the short line offered too, the cabinet's line is still
    chosen, so the attachment stands."""
    number = _text("0.3400", "0.4900", "0.3600", "0.4975")
    far = _line("0.4300", "0.5000", "0.4390", "0.5000")

    found, held = _held((number,), (CABINET,), (far,))

    assert held == found


def test_a_vertical_number_between_its_lines_ends_is_kept() -> None:
    line = _line("0.5000", "0.3000", "0.5000", "0.6000")
    number = _text("0.4900", "0.4400", "0.4960", "0.4600", degrees=90)

    found, held = _held((number,), (line,))

    assert held == found and held.associated[0].line == line


def test_every_other_reading_is_left_as_associate_decided() -> None:
    """Outcome: a reading that is not the file's own text — the reviewer's markup, an OCR or AI
    reading — keeps its attachment exactly, even printed past its line's end or beside a shorter
    stroke; a refusal stays a refusal. #918 holds AI readings to their regions on its own."""
    beyond = _text("0.4050", "0.4950", "0.4090", "0.5020")
    beside_the_filler = _text("0.3940", "0.4900", "0.3985", "0.4975")
    stray = _text("0.9000", "0.1000", "0.9100", "0.1100")

    found, held = _held(
        (beyond, beside_the_filler, stray),
        (_line("0.3000", "0.5000", "0.4024", "0.5000"),),
        (FILLER,),
        own=frozenset(),
    )

    assert held == found
    assert len(found.associated) == 2 and len(found.unassociated) == 1


def test_only_the_files_own_text_is_held() -> None:
    """Input: two numbers past the same line's end, one the file's own text and one not. Outcome:
    the file's own is refused; the other is attached exactly as before."""
    panel_edge = _line("0.3000", "0.5000", "0.4000", "0.5000")
    own = _text("0.4050", "0.4950", "0.4090", "0.5020")
    other = _text("0.4050", "0.4850", "0.4090", "0.4920")

    found, held = _held((own, other), (panel_edge,), own=frozenset({own.observation_id}))

    assert [entry.text for entry in held.associated] == [other]
    assert held.associated[0] == next(e for e in found.associated if e.text == other)
    assert [entry.text for entry in held.unassociated] == [own]


def test_a_refusal_stays_a_refusal() -> None:
    stray = _text("0.9000", "0.1000", "0.9100", "0.1100")

    found, held = _held((stray,), (CABINET,), (FILLER,))

    assert held == found
    assert isinstance(held.unassociated[0], CannotAssociate)


def test_with_no_shorter_stroke_only_the_ends_are_asked() -> None:
    """Outcome: with nothing shorter on the page, a number between its line's ends is kept and one
    past them is refused — the second check has nothing to ask."""
    inside = _text("0.3400", "0.4900", "0.3600", "0.4975")
    past = _text("0.4050", "0.4950", "0.4090", "0.5020")
    panel_edge = _line("0.3000", "0.5000", "0.4000", "0.5000")

    _, held = _held((inside, past), (panel_edge,))

    assert [entry.text for entry in held.associated] == [inside]
    assert [entry.reason for entry in held.unassociated] == [OWN_TEXT_BEYOND_ITS_ENDS]


# ---------------------------------------------------------------------------
# Between the ends, the one rule for every reading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("box", "between"),
    [
        (("0.3000", "0.4900", "0.4000", "0.4975"), True),  # exactly end to end
        (("0.2999", "0.4900", "0.3500", "0.4975"), False),  # a hair before the start
        (("0.3500", "0.4900", "0.4001", "0.4975"), False),  # a hair past the end
    ],
)
def test_between_its_ends_holds_exactly_along_a_horizontal_line(
    box: tuple[str, str, str, str], between: bool
) -> None:
    line = _line("0.4000", "0.5000", "0.3000", "0.5000")  # drawn right to left

    assert between_its_ends(_text(*box).extent, line) is between


@pytest.mark.parametrize(
    ("box", "between"),
    [
        (("0.4900", "0.3000", "0.4960", "0.6000"), True),
        (("0.4900", "0.2999", "0.4960", "0.4000"), False),
        (("0.4900", "0.5000", "0.4960", "0.6001"), False),
    ],
)
def test_between_its_ends_holds_exactly_along_a_vertical_line(
    box: tuple[str, str, str, str], between: bool
) -> None:
    """Along a vertical line only the height counts: the box may stand to either side of it."""
    line = _line("0.5000", "0.6000", "0.5000", "0.3000")

    assert between_its_ends(_text(*box, degrees=90).extent, line) is between


# ---------------------------------------------------------------------------
# The page, end to end: the pasted drawing's font text, held by the stage
# ---------------------------------------------------------------------------

#: Lengths chosen so the shapes below are checkable by hand. Strokes are line-work from 6 points, as
#: in the demo, and a dimension is offered from 0.05 of the page — 20 points across this 400-point
#: page — so the filler's 12-point line below is drawn as a dimension and never offered.
SETTINGS = AssociationSettings(
    line_minimum_pt=Decimal(6),
    glyph_maximum_pt=Decimal(6),
    glyph_gap_pt=Decimal(4),
    proximity_limit=Decimal("0.05"),
    ambiguity_margin=Decimal("0.005"),
    witness_tolerance=Decimal("0.01"),
    minimum_span=Decimal("0.05"),
    straightness=Decimal("0.0005"),
    crossing_margin=Decimal("0.001"),
    fraction_bar=FRACTION_BAR,
)


def _label(x: float, y: float, text: bytes) -> bytes:
    """Font text at 6 points, its baseline starting at appearance `(x, y)`."""
    return b"BT /F1 6 Tf 1 0 0 1 %.1f %.1f Tm (%s) Tj ET\n" % (x, y, text)


#: **A cabinet's dimension with its number between its ends, and a note's number past its end.** At
#: page y=110 from page x=100 to x=200 (stored 0.25..0.5), its witness lines crossing it 30 points
#: either side; `24"` over its middle; `7`, the number opening a note, from page x=206.
#:
#: **A cabinet's dimension running into a narrow filler's, at page y=170.** The cabinet's from page
#: x=100 to x=202, ending inside the filler's span as the client's lines do; the filler's own from
#: x=196 to x=208, 12 points, its witness lines at x=199 and x=205; the filler's `3"` printed just
#: before them, between the cabinet line's ends.
SHEET = _drawing(
    b"1 w 150 560 m 250 560 l S\n"
    b"1 w 150 530 m 150 590 l S\n"
    b"1 w 250 530 m 250 590 l S\n"
    b"1 w 150 620 m 252 620 l S\n"
    b"1 w 150 600 m 150 650 l S\n"
    b"1 w 246 620 m 258 620 l S\n"
    b"1 w 249 600 m 249 640 l S\n"
    b"1 w 255 600 m 255 640 l S\n"
    + _label(195, 563, b'24"')
    + _label(256, 563, b"7")
    + _label(242, 623, b'3"')
)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    from alembic import command
    from app.db.session import session_factory
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path, ticket_secret=b"a secret only this test knows")


def _decisions(session: Session) -> dict[str, ObservationAssociation]:
    rows = session.execute(
        select(ObservationCandidate.raw_text, ObservationAssociation).join(
            ObservationAssociation,
            ObservationAssociation.candidate_id == ObservationCandidate.id,
        )
    ).all()
    return {text: decision for text, decision in rows}


def test_the_stage_holds_the_pasted_drawings_text_to_its_lines(
    session: Session, store: LocalStore
) -> None:
    """**The wiring.** Input: `SHEET`, read with no AI reader; its labels are the pasted drawing's
    font text. Outcome: `24"` is attached to its cabinet's line; `7`, past that line's end, is
    refused; the filler's `3"` is refused, the cabinet's line and the filler's own short one kept as
    what it was between. Every decision is recorded by the association run's second version."""
    revision = _revision(session, store, data=SHEET)
    session.commit()
    DatabaseStages(
        store,
        dpi=150,
        association=SETTINGS,
        ocr_engine=_SilentOcr(),
        vision_readers=(),
        missing_space=MISSING_SPACE,
    ).extract_pages(session, revision.id)
    session.commit()

    decisions = _decisions(session)

    assert set(decisions) == {'24"', "7", '3"'}
    kept = decisions['24"']
    assert kept.refusal_reason is None
    assert kept.start_y == kept.end_y
    assert Decimal(kept.start_y or "0") == pytest.approx(Decimal(190) / 300, abs=Decimal("0.002"))
    assert decisions["7"].refusal_reason == OWN_TEXT_BEYOND_ITS_ENDS
    assert decisions["7"].start_x is None
    filler = decisions['3"']
    assert filler.refusal_reason == OWN_TEXT_SHORTER_LINE
    assert filler.candidate_lines is not None and len(filler.candidate_lines) >= 2
    spans = sorted(abs(Decimal(line[2]) - Decimal(line[0])) for line in filler.candidate_lines)
    assert spans[0] < SETTINGS.minimum_span <= spans[-1]
    runs = {
        session.get(ExtractionRun, decision.extraction_run_id) for decision in decisions.values()
    }
    assert {(run.extractor, run.extractor_version) for run in runs if run is not None} == {
        (ASSOCIATION_EXTRACTOR, ASSOCIATION_EXTRACTOR_VERSION)
    }
    assert ASSOCIATION_EXTRACTOR_VERSION.endswith("/2")


#: **The page's own text, in its content stream**, on a 300-point page: a dimension at page y=200
#: from x=100 to x=200, its witness lines crossing it 30 points either side; `12` over its middle and
#: `7` standing just past its end. Read by a vision reader too, whose readings take those boxes.
PAGE_TEXT = _content_pdf(
    b"1 w 100 200 m 200 200 l S\n"
    b"1 w 100 170 m 100 230 l S\n"
    b"1 w 200 170 m 200 230 l S\n"
    b"BT /F1 10 Tf 1 0 0 1 140 203 Tm (12) Tj ET\n"
    b"BT /F1 10 Tf 1 0 0 1 204 203 Tm (7) Tj ET\n",
    box=b"[0 0 300 300]",
)


def test_a_vision_reading_of_the_pages_own_text_is_held_to_the_same_line(
    session: Session, store: LocalStore
) -> None:
    """**A second reading of the same box is held with it.** Input: `PAGE_TEXT`, with a vision
    reader reading each text box; each vision reading is associated through the box it was shown
    (`_vision_association_inputs`). Outcome: `12` and its vision reading are attached to the
    dimension; `7` and its vision reading are both refused, so the box refused for the text is not
    left attached through its second reading."""
    revision = _revision(session, store, data=PAGE_TEXT)
    session.commit()
    DatabaseStages(
        store,
        dpi=150,
        association=ASSOCIATION_SETTINGS,
        ocr_engine=_SilentOcr(),
        vision_readers=(_association_vision_reader(),),
        missing_space=MISSING_SPACE,
    ).extract_pages(session, revision.id)
    session.commit()

    rows = session.execute(
        select(ObservationCandidate, ExtractionRun.extractor, ObservationAssociation)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .join(
            ObservationAssociation,
            ObservationAssociation.candidate_id == ObservationCandidate.id,
        )
        .where(ExtractionRun.extractor != ASSOCIATION_EXTRACTOR)
    ).all()
    by_box: dict[tuple[tuple[int, int], ...], list[tuple[str, ObservationAssociation]]] = {}
    for candidate, extractor, decision in rows:
        box = tuple((int(x), int(y)) for x, y in candidate.polygon)
        by_box.setdefault(box, []).append((extractor, decision))
    texts = {
        candidate.raw_text: tuple((int(x), int(y)) for x, y in candidate.polygon)
        for candidate, extractor, _ in rows
        if extractor != "association-vision-test"
    }

    assert set(texts) == {"12", "7"}
    kept, refused = by_box[texts["12"]], by_box[texts["7"]]
    assert {extractor for extractor, _ in kept} == {"pdfplumber", "association-vision-test"}
    assert all(decision.refusal_reason is None for _, decision in kept)
    assert {extractor for extractor, _ in refused} == {"pdfplumber", "association-vision-test"}
    assert all(decision.refusal_reason == OWN_TEXT_BEYOND_ITS_ENDS for _, decision in refused)
