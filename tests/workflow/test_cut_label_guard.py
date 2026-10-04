"""The agreement gate does not confirm a label cut off at its crop's edge (#919).

Verification for: `workflow/stages.py` (`cut_label_refusal`, `_CutLabelGuard`,
`agreement_refusal_reasons` and `DatabaseStages._apply_cross_route_corroboration`).

**The admin's decision, 2026-10-04.** A label cut off at its crop's edge is never confirmed by
agreement; it is only pre-filled for a person. With the Qwen3-VL + Nova 2 Lite pair both readers
agreed on a label running past the edge of the crop they were shown (#907). The test is the one the
gate replay measured (#851), moved into production so the two cannot disagree; it only ever takes a
confirmation away.

The drawings are made up here, or are the made-up `10192"` sheet of
`tests/workflow/test_reading_agent.py`. No model is called and no client drawing is read.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.evidence.record import open_extraction_run
from app.models import ObservationCandidate
from app.models.runs import ExtractionRun
from evidence.coordinates import ImagePoint, PageTransform, StoredPoint
from evidence.crop import RenderedPage
from evidence.polygon import Polygon
from extraction.agent.geometry import LabelReach
from extraction.annotations import PathSegment, SegmentKind, VectorPath
from extraction.ocr import OcrItem
from storage.local import LocalStore
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_association import LOCALIZED, SETTINGS, _revision, _upgrade
from tests.workflow.test_reading_agent import (
    DPI,
    SHEET,
    _config,
    _PartOfTheLabel,
    _WholeLabelReader,
)
from workflow.reading_agent import ReadingAgentSettings
from workflow.stages import (
    CUT_LABEL_REASON,
    CUT_LABEL_UNCHECKED_REASON,
    CUT_LABEL_UNSTATED_REASON,
    DatabaseStages,
    _AgentPageOutcome,
    _AgreementGuard,
    _CutLabelGuard,
    _MixedFractionGuard,
    agreement_refusal_reasons,
    cut_label_refusal,
)

pytest_plugins = ("tests.app.postgres_fixture",)

#: How a label is gathered here: the demo's lengths (`scripts/demo.sh`), stated, never defaulted.
REACH = LabelReach(label_gap_pt=Decimal(4), maximum_label_pt=Decimal(40), glyph_gap_pt=Decimal(4))


def label_lengths(primary_reader: str, *, sharper_dpi: int = 600) -> ReadingAgentSettings:
    """The reading agent, on with `primary_reader` and no second reader, for its label lengths: what
    gathers a label from the drawing's glyph paths, so what lets the gate see a cut (#919)."""
    return ReadingAgentSettings(
        max_steps=6,
        max_vlm_escalations=0,
        sharper_dpi=sharper_dpi,
        primary_reader=primary_reader,
        escalation_reader=None,
        label_gap_pt=REACH.label_gap_pt,
        maximum_label_pt=REACH.maximum_label_pt,
    )


def stated_geometry(primary_reader: str, *, sharper_dpi: int = 600) -> dict[str, object]:
    """What a stage needs to see a cut (#919): the association settings, which read the drawing's
    glyph paths, and the reading agent's label lengths, which gather a label from them.

    For the tests of other rules whose readers must be confirmed: without these, nothing rules a
    cut out, and the gate confirms no agreement. The agent asks no second reader, and looks only
    where its own trigger sends it.
    """
    return {
        "association": SETTINGS,
        "reading_agent": label_lengths(primary_reader, sharper_dpi=sharper_dpi),
    }


def _never_rendered() -> RenderedPage | None:
    raise AssertionError("the answer does not turn on the crop, so the page is never rendered")


def whole_labels() -> _CutLabelGuard:
    """The guard for a page whose drawing holds no glyph path: nothing on it can be cut.

    For the tests of other rules that call `_apply_cross_route_corroboration` directly."""
    return _CutLabelGuard(reach=REACH, transform=None, glyphs=(), render=_never_rendered)


# ---------------------------------------------------------------------------
# The test the replay measured: the label gathered from the drawing's own characters
# ---------------------------------------------------------------------------

DOCUMENT = UUID("44444444-4444-4444-8444-444444444444")

#: A 200 × 100 pt page at 72 dpi, so a pixel is a point and only the y axis turns over.
TRANSFORM = PageTransform(
    dpi=72,
    rotation=0,
    media_box=(Decimal(0), Decimal(0), Decimal(200), Decimal(100)),
    crop_box=(Decimal(0), Decimal(0), Decimal(200), Decimal(100)),
)


def _rendered() -> RenderedPage:
    return RenderedPage(
        document_version_id=DOCUMENT,
        page_index=0,
        page_content_hash="0" * 64,
        rotation=0,
        render_failed=False,
        width_px=200,
        height_px=100,
        dpi=72,
        rgb_bytes=bytes(200 * 100 * 3),
    )


def _character(left: int) -> VectorPath:
    """One character drawn as a path, as the client's are: a stroke 3 pt across, 40–46 pt up."""
    return VectorPath(
        segments=(
            PathSegment(SegmentKind.MOVE, (Decimal(left), Decimal(40)), False),
            PathSegment(SegmentKind.LINE, (Decimal(left + 3), Decimal(46)), False),
        ),
        stroked=True,
        filled=False,
        stroke_colour=(0, 0, 0, 255),
        fill_colour=None,
    )


#: A four-character label, 40–55 pt across and 54–60 px down: each character 1 pt from the next.
GLYPHS = tuple(_character(left) for left in (40, 44, 48, 52))

#: A region round the label's last character only, and one round the whole label. The readers'
#: crop adds 9 pt (9 px at 72 dpi) round each: 44–67 px across cuts the label's start off; 27–68 px
#: holds all of it.
LAST_CHARACTER = ((53, 52), (58, 52), (58, 62), (53, 62))
WHOLE_LABEL = ((36, 52), (59, 52), (59, 62), (36, 62))


def _polygon(points: tuple[tuple[int, int], ...]) -> Polygon:
    """A region's polygon on the 200 × 100 px rendering, as `stored_polygon` makes one."""
    return Polygon(
        points=tuple(StoredPoint(x=Decimal(x) / 200, y=Decimal(y) / 100) for x, y in points),
        space="stored",
        document_version_id=DOCUMENT,
        page=0,
    )


def _refusal(region: tuple[tuple[int, int], ...], **changes: object) -> str | None:
    arguments: dict[str, object] = {
        "rendered": _rendered(),
        "polygon": _polygon(region),
        "transform": TRANSFORM,
        "reach": REACH,
        "page_glyphs": GLYPHS,
    }
    arguments.update(changes)
    return cut_label_refusal([list(point) for point in region], **arguments)  # type: ignore[arg-type]


def test_a_crop_round_part_of_a_label_cuts_it_off() -> None:
    """**The #641 crop.** The region holds the last character; the label it belongs to, gathered
    from the drawing's own characters, runs past the crop's left edge."""
    assert _refusal(LAST_CHARACTER) == CUT_LABEL_REASON


def test_a_crop_round_the_whole_label_does_not() -> None:
    assert _refusal(WHOLE_LABEL) is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"reach": None}, CUT_LABEL_UNSTATED_REASON),
        ({"page_glyphs": None}, CUT_LABEL_UNCHECKED_REASON),
        ({"rendered": None}, CUT_LABEL_UNCHECKED_REASON),
        ({"polygon": None}, CUT_LABEL_UNCHECKED_REASON),
        ({"transform": None}, CUT_LABEL_UNCHECKED_REASON),
    ],
    ids=["no-label-lengths", "paths-unreadable", "not-rendered", "not-placed", "no-transform"],
)
def test_a_crop_that_cannot_be_checked_holds_the_agreement_back(
    changes: dict[str, object], reason: str
) -> None:
    """**Stricter where it cannot check.** The whole label, which the full check confirms, held
    back for want of any one thing the check needs: nothing else rules a cut out."""
    assert _refusal(WHOLE_LABEL, **changes) == reason


def test_a_page_with_no_glyph_paths_has_nothing_to_cut() -> None:
    """**The measured limit.** A label set in font text has no characters for the geometry to
    gather — `label_geometry` finds none in its region — so nothing is cut; on a page with no glyph
    paths at all, nothing else is needed to say so."""
    assert (
        _refusal(LAST_CHARACTER, page_glyphs=(), rendered=None, polygon=None, transform=None)
        is None
    )


# ---------------------------------------------------------------------------
# The page's guard: rendered only when the answer turns on the crop, each refusal counted once
# ---------------------------------------------------------------------------


def _region(points: tuple[tuple[int, int], ...]) -> ObservationCandidate:
    return ObservationCandidate(
        document_version_id=DOCUMENT, polygon=[list(point) for point in points], raw_text='12"'
    )


@dataclass
class _Renders:
    """The page's rendering, counted: the guard is to ask for it once at most."""

    page: RenderedPage | None
    asked: int = 0

    def __call__(self) -> RenderedPage | None:
        self.asked += 1
        return self.page


def test_the_guard_counts_one_refusal_per_region_with_its_reason() -> None:
    """Asked twice about one region — the stage asks again once the reading agent has looked — it
    is one refusal; the whole label is none. The page is rendered once for both."""
    renders = _Renders(_rendered())
    guard = _CutLabelGuard(reach=REACH, transform=TRANSFORM, glyphs=GLYPHS, render=renders)

    assert guard.holds_back(_region(LAST_CHARACTER))
    assert guard.holds_back(_region(LAST_CHARACTER))
    assert not guard.holds_back(_region(WHOLE_LABEL))

    assert len(guard.refused) == 1
    assert guard.reasons(20) == [f"1 × {CUT_LABEL_REASON}"]
    assert renders.asked == 1


@pytest.mark.parametrize(
    ("reach", "glyphs", "refused"),
    [(None, GLYPHS, True), (REACH, (), False), (REACH, None, True)],
    ids=["no-label-lengths", "no-glyph-paths", "paths-unreadable"],
)
def test_nothing_is_rendered_where_the_answer_does_not_turn_on_the_crop(
    reach: LabelReach | None, glyphs: tuple[VectorPath, ...] | None, refused: bool
) -> None:
    guard = _CutLabelGuard(reach=reach, transform=TRANSFORM, glyphs=glyphs, render=_never_rendered)

    assert guard.holds_back(_region(WHOLE_LABEL)) is refused


def test_a_page_that_cannot_be_rendered_holds_every_agreement_back() -> None:
    guard = _CutLabelGuard(reach=REACH, transform=TRANSFORM, glyphs=GLYPHS, render=_Renders(None))

    assert guard.holds_back(_region(WHOLE_LABEL))
    assert guard.reasons(20) == [f"1 × {CUT_LABEL_UNCHECKED_REASON}"]


# ---------------------------------------------------------------------------
# In the stage: two readers agree on the #641 sheet's label, shown cut or whole
# ---------------------------------------------------------------------------


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
def store() -> Iterator[LocalStore]:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


@dataclass
class _WholeOcr:
    """An OCR engine that reads `text` across the whole of whatever crop it is shown."""

    text: str
    name: str = "whole-ocr"
    version: str = "whole/1"

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        del rgb
        return (
            OcrItem(
                text=self.text,
                confidence=Decimal("0.9"),
                image_extent=(
                    ImagePoint(0, 0),
                    ImagePoint(width - 1, 0),
                    ImagePoint(width - 1, height - 1),
                    ImagePoint(0, height - 1),
                ),
            ),
        )


def _agent_held_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """**Only the first pass judges here.** The reading agent would look again at a cut label and
    read it whole; held out, the readers' agreement is what the gate is asked about, alone."""

    def nothing(self: DatabaseStages, session: Session, **kwargs: object) -> _AgentPageOutcome:
        del self, session, kwargs
        return _AgentPageOutcome()

    monkeypatch.setattr(DatabaseStages, "_run_bounded_agent_for_ambiguous_regions", nothing)


def _read(
    session: Session,
    store: LocalStore,
    ocr: object,
    reading: str,
) -> tuple[list[tuple[str, str, str | None, str | None]], dict[str, object]]:
    """Read the sheet with the OCR box and both readers' reading; every row, and the page result."""
    revision = _revision(session, store, data=SHEET)
    session.commit()
    readers = (
        _WholeLabelReader(_config("reader-a"), reading),
        _WholeLabelReader(_config("reader-b"), reading),
    )
    (result,) = DatabaseStages(
        store,
        dpi=DPI,
        ocr_engine=ocr,  # type: ignore[arg-type]
        localized_ocr=LOCALIZED,
        vision_readers=readers,
        missing_space=MISSING_SPACE,
        **stated_geometry("reader-a", sharper_dpi=2 * DPI),  # type: ignore[arg-type]
    ).extract_pages(session, revision.id)
    session.commit()
    rows = sorted(
        (
            (row.raw_text, run.extractor, row.corroboration_status, row.corroboration_lane)
            for row, run in session.execute(
                select(ObservationCandidate, ExtractionRun).join(
                    ExtractionRun, ObservationCandidate.extraction_run_id == ExtractionRun.id
                )
            )
        ),
        key=str,
    )
    return rows, dict(result.payload)


def test_two_readers_agreeing_on_a_cut_label_do_not_confirm_it(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Done when, first line.** OCR's box holds the last two digits of `10192"`, and both readers,
    shown the crop round it, read `92"` with it: three readers agree on part of a label. No reading
    takes the second-reader lane, so none can be sealed; each is a pre-fill a person ticks, and the
    page result counts the refusal and says why."""
    _agent_held_out(monkeypatch)

    rows, payload = _read(session, store, _PartOfTheLabel('92"'), '92"')

    assert rows == [
        ('92"', "part-ocr", None, None),
        ('92"', "reader-a", None, None),
        ('92"', "reader-b", None, None),
    ]
    assert payload["agreement_refusals"] == 1
    assert payload["agreement_refusal_reasons"] == [f"1 × {CUT_LABEL_REASON}"]


def test_the_same_agreement_on_a_whole_label_is_confirmed(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Done when, second line.** OCR's box holds the whole label, so the readers' crop does too;
    the same three readers agreeing on `10192"` confirm it, as before."""
    _agent_held_out(monkeypatch)

    rows, payload = _read(session, store, _WholeOcr('10192"'), '10192"')

    assert rows == [
        ('10192"', "reader-a", "RAW_CANDIDATE", "SECOND_READER"),
        ('10192"', "reader-b", "RAW_CANDIDATE", "SECOND_READER"),
        ('10192"', "whole-ocr", "RAW_CANDIDATE", "SECOND_READER"),
    ]
    assert payload["agreement_refusals"] == 0
    assert payload["agreement_refusal_reasons"] == []


def test_a_disagreement_on_a_cut_label_is_still_a_conflict(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Only stricter.** Readers who disagree are a conflict for a reviewer whatever the crop
    shows; the guard is never asked, so it cannot turn a conflict into a pre-fill."""
    _agent_held_out(monkeypatch)

    def never(self: _CutLabelGuard, region: ObservationCandidate) -> str | None:
        raise AssertionError("the guard is asked only about an agreement")

    monkeypatch.setattr(_CutLabelGuard, "_reason", never)

    rows, payload = _read(session, store, _PartOfTheLabel('92"'), '93"')

    assert {(status, lane) for *_, status, lane in rows} == {("CONFLICTING", "SECOND_READER")}
    assert payload["agreement_refusals"] == 0


def test_a_page_whose_drawing_could_not_be_read_confirms_no_agreement(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Stricter where it cannot check.** The page's drawing does not parse, so none of its
    characters are known and no crop on it can be shown to hold a whole label: the same readers
    agreeing on the whole label stay pre-fills, and the page says why."""
    _agent_held_out(monkeypatch)

    def unreadable(
        self: DatabaseStages, session: Session, **kwargs: object
    ) -> tuple[list[object], None]:
        del self, session, kwargs
        return [], None

    monkeypatch.setattr(DatabaseStages, "_read_page_markup", unreadable)

    rows, payload = _read(session, store, _WholeOcr('10192"'), '10192"')

    assert {text for text, *_ in rows} == {'10192"'}
    assert {(status, lane) for *_, status, lane in rows} == {(None, None)}
    assert payload["agreement_refusal_reasons"] == [f"1 × {CUT_LABEL_UNCHECKED_REASON}"]


def test_the_cut_label_s_refusal_is_the_page_s_only_reason_once_the_agent_has_looked(
    session: Session, store: LocalStore
) -> None:
    """**With the reading agent on, as in the demo.** It widens the cut crop and reads `10192"`,
    agreeing with the readers who were shown it cut. The region is still the one whose crop cut
    the label, so the agreement stays a pre-fill: counted once, though the gate was asked twice."""
    rows, payload = _read(session, store, _PartOfTheLabel('10192"'), '10192"')

    assert {text for text, *_ in rows} == {'10192"'}
    assert {(status, lane) for *_, status, lane in rows} == {(None, None)}
    assert payload["agent_regions"] == 1
    assert payload["agreement_refusals"] == 1
    assert payload["agreement_refusal_reasons"] == [f"1 × {CUT_LABEL_REASON}"]


# ---------------------------------------------------------------------------
# Beside #901's guard: one reason per refusal, and the counts add up
# ---------------------------------------------------------------------------


class _Says(_AgreementGuard):
    """A guard that holds back the regions it is told to, and records every region it is asked."""

    def __init__(self, reasons: dict[tuple[tuple[int, int], ...], str]) -> None:
        super().__init__()
        self._reasons = reasons
        self.asked: list[tuple[tuple[int, int], ...]] = []

    def _reason(self, region: ObservationCandidate) -> str | None:
        key = tuple((int(x), int(y)) for x, y in region.polygon)
        self.asked.append(key)
        return self._reasons.get(key)


MARKED = ((10, 10), (20, 10), (20, 20), (10, 20))
CUT = ((30, 10), (40, 10), (40, 20), (30, 20))
PLAIN = ((50, 10), (60, 10), (60, 20), (50, 20))


def _agreeing_pairs(session: Session, store: LocalStore) -> list[ObservationCandidate]:
    """Two readers of different routes agreeing on `24"` in each of three regions, unsaved."""
    revision = _revision(session, store, data=SHEET)
    session.commit()
    DatabaseStages(
        store,
        dpi=DPI,
        ocr_engine=_PartOfTheLabel('92"'),  # type: ignore[arg-type]
        association=SETTINGS,
        localized_ocr=LOCALIZED,
        vision_readers=(),
        missing_space=MISSING_SPACE,
    ).extract_pages(session, revision.id)
    template = session.execute(select(ObservationCandidate)).scalars().one()
    first = session.get(ExtractionRun, template.extraction_run_id)
    assert first is not None
    runs = [
        open_extraction_run(
            session,
            task_run_id=first.task_run_id,
            extractor=extractor,
            extractor_version="test/1",
            config_hash="test",
            dpi=first.dpi,
        )
        for extractor in ("first-route", "second-route")
    ]
    rows = [
        ObservationCandidate(
            document_version_id=template.document_version_id,
            page_id=template.page_id,
            extraction_run_id=run.id,
            raw_text='24"',
            value_numerator=24,
            value_denominator=1,
            unit="in",
            unit_guess="in",
            semantic_guess=None,
            polygon=[list(point) for point in region],
            coordinate_space="image",
            confidence=None,
            ambiguity_flags=[],
        )
        for region in (MARKED, CUT, PLAIN)
        for run in runs
    ]
    session.add_all(rows)
    return rows


def _lanes(rows: list[ObservationCandidate]) -> dict[tuple[tuple[int, int], ...], set[object]]:
    lanes: dict[tuple[tuple[int, int], ...], set[object]] = {}
    for row in rows:
        key = tuple((int(x), int(y)) for x, y in row.polygon)
        lanes.setdefault(key, set()).add(row.corroboration_lane)
    return lanes


def test_a_gv_mark_and_a_cut_label_are_each_refused_once_and_counted_together(
    session: Session, store: LocalStore
) -> None:
    """**#901's guard first, then this one.** A region the GV-mark guard holds back is never asked
    about its cut — had it been, the cut guard here would hold it back too — so each refusal has one
    reason, and the page's count is the two guards' counts added. The region neither holds back is
    confirmed. Asked again, as the stage asks once the reading agent has looked, a guard answers a
    region it refused from what it already decided; a region it let through is asked again."""
    rows = _agreeing_pairs(session, store)
    gv_mark = _Says({MARKED: "a GV mark"})
    cut_label = _Says({MARKED: "a cut label", CUT: "a cut label"})

    for _ in range(2):
        DatabaseStages._apply_cross_route_corroboration(
            session,
            page_index=0,
            candidates=rows,
            gv_mark=gv_mark,  # type: ignore[arg-type]
            cut_label=cut_label,  # type: ignore[arg-type]
            mixed_fraction=_MixedFractionGuard(),
        )

    assert _lanes(rows) == {MARKED: {None}, CUT: {None}, PLAIN: {"SECOND_READER"}}
    assert MARKED not in cut_label.asked
    assert (gv_mark.asked, cut_label.asked) == ([MARKED, CUT, PLAIN, CUT], [CUT, PLAIN])
    assert (set(gv_mark.refused), set(cut_label.refused)) == ({MARKED}, {CUT})
    assert agreement_refusal_reasons((gv_mark, cut_label), 20) == [
        "1 × a GV mark",
        "1 × a cut label",
    ]


def test_the_page_lists_the_most_frequent_reason_first_up_to_its_limit() -> None:
    gv_mark = _Says({MARKED: "a GV mark"})
    cut_label = _Says({CUT: "a cut label", PLAIN: "a cut label"})
    for guard in (gv_mark, cut_label):
        for region in (MARKED, CUT, PLAIN):
            guard.holds_back(_region(region))

    assert agreement_refusal_reasons((gv_mark, cut_label), 20) == [
        "2 × a cut label",
        "1 × a GV mark",
    ]
    assert agreement_refusal_reasons((gv_mark, cut_label), 1) == ["2 × a cut label"]
