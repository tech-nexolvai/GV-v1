"""The reading agent in the worker: its settings, its crops, and one real run (#757, part 2).

Verification for: `workflow/reading_agent.py`, and the agent's run over a page in `workflow/stages.py`.

The one that matters most is `test_a_label_the_crop_cut_is_widened_and_read_whole`: the #641 failure
end to end. A reader's box holds the last two digits of a label; the file's own paths say the crop
cuts it; the agent widens the crop to the label's whole run before any reader is asked, and what is
proposed is the whole label.

The drawing is a real one-page PDF with a stamp, drawn in the made-up font of
`tests/extraction/test_glyph_reader.py`. No client drawing is read and no model is called: the
reader stands in for one, and reads a crop only when it shows the whole label.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import ObservationCandidate
from app.models.runs import ExtractionRun
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.coordinates import ImagePoint, StoredPoint
from evidence.crop import RenderedPage, decode_rgb_png
from evidence.polygon import Polygon
from extraction.agent.tools import RefineCropArguments, Refinement
from extraction.models.context import AssembledContext
from extraction.models.nova import (
    NovaConfig,
    NovaInvocation,
    NovaInvocationOutcome,
    NovaRefusalError,
    NovaRequest,
)
from extraction.ocr import OcrItem
from storage.local import LocalStore
from tests.extraction.test_annotations import _appearance, _pdf, _stamp
from tests.extraction.test_glyph_reader import _row
from tests.workflow.test_association import LOCALIZED, SETTINGS, _revision, _upgrade
from tests.workflow.test_glyph_route import _content
from units.measurement import Unit
from workflow.reading_agent import (
    READING_AGENT_ENV,
    ReadingAgentSettings,
    RegionCrops,
    crop_box_px,
    reading_agent_from_environment,
)
from workflow.stages import VISION_CROP_CONTEXT_MARGIN_PT, DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

ASSOCIATION = replace(SETTINGS, proximity_limit=Decimal("0.9"))
DPI = 150
SHARPER = 300


def _settings(**changes: object) -> ReadingAgentSettings:
    values: dict[str, object] = {
        "max_steps": 6,
        "max_vlm_escalations": 1,
        "sharper_dpi": SHARPER,
        "primary_reader": "reader-a",
        "escalation_reader": "reader-b",
        "label_gap_pt": Decimal(4),
        "maximum_label_pt": Decimal(80),
    }
    values.update(changes)
    return ReadingAgentSettings(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

ENVIRONMENT = {
    READING_AGENT_ENV: "1",
    "GV_AGENT_MAX_STEPS": "6",
    "GV_AGENT_MAX_ESCALATIONS": "1",
    "GV_AGENT_SHARPER_DPI": "450",
    "GV_AGENT_PRIMARY_READER": "bedrock-nova-2-lite",
    "GV_AGENT_ESCALATION_READER": "bedrock-mistral-large-3",
    "GV_AGENT_LABEL_GAP_PT": "2",
    "GV_AGENT_MAX_LABEL_PT": "40",
}


def test_the_agent_is_off_unless_turned_on() -> None:
    assert reading_agent_from_environment({}) is None
    assert reading_agent_from_environment({READING_AGENT_ENV: "0"}) is None


def test_turned_on_without_its_settings_is_refused_naming_every_one() -> None:
    """Outcome: a worker with the agent on and no budget stated does not start — none is defaulted."""
    with pytest.raises(ValueError) as refused:
        reading_agent_from_environment({READING_AGENT_ENV: "1", "GV_AGENT_MAX_STEPS": "6"})

    for name in ("GV_AGENT_SHARPER_DPI", "GV_AGENT_PRIMARY_READER", "GV_AGENT_MAX_LABEL_PT"):
        assert name in str(refused.value)
    assert "GV_AGENT_MAX_STEPS" not in str(refused.value)


def test_a_configured_agent_records_every_setting_in_its_identity() -> None:
    """**Acceptance criterion: the sharper DPI is stated and in the run identity.**"""
    settings = reading_agent_from_environment(ENVIRONMENT)

    assert settings is not None
    assert settings.sharper_dpi == 450
    assert "sharper_dpi=450" in settings.config_hash
    assert "escalation=bedrock-mistral-large-3" in settings.config_text
    assert settings.config_hash != _settings(label_gap_pt=Decimal(3)).config_hash
    assert settings.limits.max_ocr_retries == 0


def test_an_escalation_needs_a_named_reader_that_is_not_the_primary() -> None:
    with pytest.raises(ValueError, match="escalation reader must be named"):
        _settings(escalation_reader=None)
    with pytest.raises(ValueError, match="no escalation reader may be named"):
        _settings(max_vlm_escalations=0)
    with pytest.raises(ValueError, match="not a second witness"):
        _settings(escalation_reader="reader-a")


def test_a_budget_outside_the_guardrails_is_refused() -> None:
    """Outcome: DESIGN_AI §3.2's 6–8 steps, enforced before the first region, not at it."""
    with pytest.raises(ValueError, match="max_steps"):
        _settings(max_steps=12)


# ---------------------------------------------------------------------------
# The crops
# ---------------------------------------------------------------------------

DOCUMENT = UUID(int=7)


def _page(dpi: int) -> RenderedPage:
    """A white page, 4 × 2 inches, with a black square mark at its top-left corner."""
    width, height = 4 * dpi, 2 * dpi
    rows = []
    for y in range(height):
        row = bytearray(b"\xff" * (width * 3))
        if y < dpi // 4:
            row[: (dpi // 4) * 3] = b"\x00" * ((dpi // 4) * 3)
        rows.append(bytes(row))
    return RenderedPage(
        document_version_id=DOCUMENT,
        page_index=0,
        page_content_hash="0" * 64,
        rotation=0,
        render_failed=False,
        width_px=width,
        height_px=height,
        dpi=dpi,
        rgb_bytes=b"".join(rows),
    )


def _polygon(left: str, top: str, right: str, bottom: str) -> Polygon:
    corners = (
        (Decimal(left), Decimal(top)),
        (Decimal(right), Decimal(top)),
        (Decimal(right), Decimal(bottom)),
        (Decimal(left), Decimal(bottom)),
    )
    return Polygon(
        points=tuple(StoredPoint(x=x, y=y) for x, y in corners),
        space="stored",
        document_version_id=DOCUMENT,
        page=0,
    )


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _crops(store: LocalStore, **changes: object) -> RegionCrops:
    values: dict[str, object] = {
        "store": store,
        "render": lambda dpi: _page(dpi),
        "base": _page(72),
        "polygon": _polygon("0.25", "0.25", "0.5", "0.5"),
        "margin_pt": Decimal(1),
        "sharper_dpi": 144,
        "whole_run": _polygon("0.1", "0.25", "0.5", "0.5"),
        "rotation_degrees": 90,
        "stacked": lambda _polygon: False,
    }
    values.update(changes)
    return RegionCrops(**values)  # type: ignore[arg-type]


def _size(crops: RegionCrops, key: str) -> tuple[int, int]:
    width, height, _ = decode_rgb_png(crops.png(key))
    return width, height


def _refine(crops: RegionCrops, refinement: Refinement, key: str = "k") -> object:
    return crops.refine(RefineCropArguments("r", key, refinement))


def _cut_size(rendered: RenderedPage, polygon: Polygon, margin: Decimal) -> tuple[int, int]:
    left, top, right, bottom = crop_box_px(rendered, polygon, margin)
    return right - left, bottom - top


def test_the_whole_run_refinement_crops_to_the_labels_whole_run(store: LocalStore) -> None:
    """**Acceptance criterion.** Outcome: the widened crop is the one cut round the whole run, and a
    second widening is refused rather than repeated."""
    crops = _crops(store)
    first = crops.first()
    assert first is not None

    widened = _refine(crops, Refinement.WHOLE_RUN)

    assert not hasattr(widened, "reason"), widened
    key = widened.crop_artifact_id  # type: ignore[attr-defined]
    assert _size(crops, key) == _cut_size(crops.base, crops.whole_run, Decimal(1))  # type: ignore[arg-type]
    assert _size(crops, key)[0] > _size(crops, first)[0]
    assert hasattr(_refine(crops, Refinement.WHOLE_RUN), "reason")


def test_a_label_the_file_does_not_settle_cannot_be_widened(store: LocalStore) -> None:
    """Outcome: a failure that says why, and the crop left as it was."""
    crops = _crops(store, whole_run=None)
    crops.first()

    result = _refine(crops, Refinement.WHOLE_RUN)

    assert "not settled" in result.reason  # type: ignore[attr-defined]


def test_the_upright_refinement_turns_the_crop_by_the_labels_own_direction(
    store: LocalStore,
) -> None:
    """Outcome: width and height swap, and the mark in the page's top-left corner — which a crop
    from the page's top-left keeps at its top-left — moves to the top-right, as turning a label that
    reads up the page clockwise must move it."""
    crops = _crops(store, polygon=_polygon("0", "0", "0.25", "0.25"), margin_pt=Decimal("0.1"))
    first = crops.first()
    assert first is not None
    before = _size(crops, first)

    turned = _refine(crops, Refinement.UPRIGHT)

    key = turned.crop_artifact_id  # type: ignore[attr-defined]
    width, height, rgb = decode_rgb_png(crops.png(key))
    assert (width, height) == (before[1], before[0])
    assert rgb[(width - 1) * 3 : width * 3] == b"\x00\x00\x00"
    assert rgb[:3] == b"\xff\xff\xff"


def test_an_upright_label_is_not_turned(store: LocalStore) -> None:
    crops = _crops(store, rotation_degrees=0)
    crops.first()

    assert hasattr(_refine(crops, Refinement.UPRIGHT), "reason")


def test_the_sharper_refinement_renders_at_the_stated_resolution(store: LocalStore) -> None:
    """**Acceptance criterion.** Outcome: cut from the 144 dpi render — about twice the 72 dpi crop
    each way — and it keeps the widening already applied."""
    crops = _crops(store)
    crops.first()
    widened = _refine(crops, Refinement.WHOLE_RUN)
    wide = _size(crops, widened.crop_artifact_id)  # type: ignore[attr-defined]

    sharper = _refine(crops, Refinement.SHARPER)

    assert crops.dpi == 144
    size = _size(crops, sharper.crop_artifact_id)  # type: ignore[attr-defined]
    assert size == _cut_size(_page(144), crops.whole_run, Decimal(1))  # type: ignore[arg-type]
    assert abs(size[0] - 2 * wide[0]) <= 2 and abs(size[1] - 2 * wide[1]) <= 2


def test_a_page_too_large_to_render_sharper_fails_the_refinement_and_changes_nothing(
    store: LocalStore,
) -> None:
    crops = _crops(store, render=lambda _dpi: "the page would be 90000000 pixels")
    crops.first()

    result = _refine(crops, Refinement.SHARPER)

    assert "144 dpi" in result.reason  # type: ignore[attr-defined]
    assert crops.dpi == 72


def test_each_request_says_whether_the_crop_it_sends_shows_a_stacked_fraction(
    store: LocalStore,
) -> None:
    """**#735 on the agent's crops.** Outcome: the first crop does not show the fraction; widened to
    the run beside it, it does, and the request for it has to say so."""
    wide = _polygon("0.1", "0.25", "0.5", "0.5")
    crops = _crops(store, whole_run=wide, stacked=lambda polygon: polygon is wide)
    crops.first()
    assert not crops.shows_stacked_fraction

    _refine(crops, Refinement.WHOLE_RUN)

    assert crops.shows_stacked_fraction


# ---------------------------------------------------------------------------
# One real run
# ---------------------------------------------------------------------------

#: A dimension line with `10192"` above it, in appearance space (page = appearance − (50, 450)).
LABEL, _ = _row('10192"', 110, 520, scale=0.9)
SHEET = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[_appearance(b"1 w 105 515 m 205 515 l S\n" + _content(LABEL))],
)


def _whole_label_px() -> int:
    """How wide a crop showing the whole label is at least: the label and the 9 pt margin each side.

    Less two pixels for the crop's own rounding. The crop round the OCR box — the last two digits
    and the same margins — is narrower by the digits it leaves out.
    """
    xs = [x for path in LABEL for x, _ in path.points]
    return int((max(xs) - min(xs) + 2 * VISION_CROP_CONTEXT_MARGIN_PT) * DPI / 72) - 2


@dataclass
class _PartOfTheLabel:
    """An OCR engine that reads the last two digits of whatever it is shown — the #641 crop."""

    text: str = "92"
    name: str = "part-ocr"
    version: str = "part/1"

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        del rgb
        left = width * 11 // 20
        return (
            OcrItem(
                text=self.text,
                confidence=Decimal("0.9"),
                image_extent=(
                    ImagePoint(left, 0),
                    ImagePoint(width - 1, 0),
                    ImagePoint(width - 1, height - 1),
                    ImagePoint(left, height - 1),
                ),
            ),
        )


def _config(extractor: str) -> NovaConfig:
    return NovaConfig(
        model_id=f"{extractor}/1",
        prompt_id="dimension-reader-v1",
        template_id="bounded-crop-v1",
        connect_timeout_seconds=1,
        read_timeout_seconds=1,
        max_attempts=1,
        extractor=extractor,
    )


@dataclass
class _WholeLabelReader:
    """Reads `10192"` from a crop wide enough to show the whole label.

    A narrower one it refuses, or — given `cut_reading` — reads as that, the way a real reader reads
    what the cut crop shows.
    """

    config: NovaConfig
    cut_reading: str | None = None
    widths: list[int] = field(default_factory=list)
    stacked: list[bool] = field(default_factory=list)

    def extract(self, request: NovaRequest, recorder: object) -> DomainCandidate:
        width, _, _ = decode_rgb_png(request.crop)
        self.widths.append(width)
        self.stacked.append(request.stacked_label)
        recorder.record(  # type: ignore[attr-defined]
            NovaInvocation(
                model_id=self.config.model_id,
                prompt_id=self.config.prompt_id,
                template_id=self.config.template_id,
                attempt=1,
                latency_ms=1,
                input_tokens=10,
                output_tokens=2,
                outcome=NovaInvocationOutcome.OK,
                request_id=f"request-{uuid4()}",
                context=AssembledContext(nearby_text=(), nearby_geometry=()),
                bound_pt=Decimal(9),
                injection_attempts=(),
            )
        )
        whole = width >= _whole_label_px()
        if not whole and self.cut_reading is None:
            raise NovaRefusalError("the crop does not show a whole dimension")
        return DomainCandidate(
            candidate_id=request.candidate_id,
            extractor=self.config.extractor,
            extractor_version=self.config.model_id,
            raw_text='10192"' if whole else str(self.cut_reading),
            parsed_value=None,
            unit_guess=Unit.INCH,
            semantic_guess=None,
            page=request.page,
            polygon=(ImagePoint(0, 0), ImagePoint(1, 0), ImagePoint(1, 1)),
            confidence=None,
            ambiguity_flags=(),
        )


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    from app.db.session import session_factory

    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _stages(
    store: LocalStore,
    readers: tuple[_WholeLabelReader, ...],
    agent: ReadingAgentSettings | None,
    *,
    ocr_text: str = "92",
) -> DatabaseStages:
    return DatabaseStages(
        store,
        dpi=DPI,
        association=ASSOCIATION,
        ocr_engine=_PartOfTheLabel(ocr_text),  # type: ignore[arg-type]
        localized_ocr=LOCALIZED,
        vision_readers=readers,
        reading_agent=agent,
    )


def _readers(cut_reading: str | None = None) -> tuple[_WholeLabelReader, _WholeLabelReader]:
    return (
        _WholeLabelReader(_config("reader-a"), cut_reading),
        _WholeLabelReader(_config("reader-b"), cut_reading),
    )


def _agent_rows(session: Session) -> list[tuple[ObservationCandidate, ExtractionRun]]:
    return [
        (row, run)
        for row, run in session.execute(
            select(ObservationCandidate, ExtractionRun).join(
                ExtractionRun, ObservationCandidate.extraction_run_id == ExtractionRun.id
            )
        )
        if "route=bounded_agent" in run.config_hash
    ]


def test_a_label_the_crop_cut_is_widened_and_read_whole(
    session: Session, store: LocalStore
) -> None:
    """**The #641 failure, end to end.** Outcome: the OCR box holds `92`, the file says the label
    is `10192"` and the crop cuts it, so the primary reader is shown the widened crop — never the
    cut one — and `10192"` is proposed, recorded under the primary reader's own name with every
    setting in its run, and the call it cost recorded with it."""
    revision = _revision(session, store, data=SHEET)
    session.commit()
    primary, escalation = _readers()

    results = _stages(store, (primary, escalation), _settings()).extract_pages(session, revision.id)
    session.commit()

    ((row, run),) = _agent_rows(session)
    assert row.raw_text == '10192"'
    assert (row.value_numerator, row.value_denominator, row.unit) == (10192, 1, "in")
    assert run.extractor == "reader-a"
    assert "sharper_dpi=300" in run.config_hash
    # The vision route asked both readers about the cut crop, and both refused it; the agent's one
    # call was the widened crop.
    assert primary.widths[-1] >= _whole_label_px() > max(primary.widths[:-1])
    assert all(width < _whole_label_px() for width in escalation.widths)
    (payload,) = [result.payload for result in results]
    assert (payload["agent_regions"], payload["agent_candidates"]) == (1, 1)
    assert payload["agent_invocations"] == 1


def test_a_rerun_does_not_pay_for_a_region_twice(session: Session, store: LocalStore) -> None:
    """**An interrupted stage reuses what it recorded** (DESIGN_AI §3.2). Outcome: redelivered, the
    widened crop is not read again and the proposal is the same row."""
    revision = _revision(session, store, data=SHEET)
    session.commit()
    primary, escalation = _readers()
    stages = _stages(store, (primary, escalation), _settings())

    stages.extract_pages(session, revision.id)
    session.commit()
    stages.extract_pages(session, revision.id)
    session.commit()

    assert sum(width >= _whole_label_px() for width in primary.widths) == 1
    assert len(_agent_rows(session)) == 1


def test_off_by_default_nothing_changes(session: Session, store: LocalStore) -> None:
    """Outcome: no agent row, and the page says the agent did not run — not that it found nothing."""
    revision = _revision(session, store, data=SHEET)
    session.commit()

    results = _stages(store, _readers(), None).extract_pages(session, revision.id)
    session.commit()

    assert _agent_rows(session) == []
    (payload,) = [result.payload for result in results]
    assert payload["agent_regions"] is None


def test_an_agent_whose_readers_are_not_configured_does_not_start(store: LocalStore) -> None:
    """Outcome: refused at construction, naming the reader — never a silently different one."""
    with pytest.raises(ValueError, match="reader-b"):
        _stages(store, (_WholeLabelReader(_config("reader-a")),), _settings())
    with pytest.raises(ValueError, match="above the stage's 150 dpi"):
        _stages(store, _readers(), _settings(sharper_dpi=DPI))


def test_a_region_three_readers_left_unresolved_is_run_once(
    session: Session, store: LocalStore
) -> None:
    """**One run per region, not per row.** Outcome: OCR and both vision readers read `92` with no
    unit — three unresolved rows at one box — and the agent runs once, not three times."""
    revision = _revision(session, store, data=SHEET)
    session.commit()
    primary, escalation = _readers(cut_reading="92")

    results = _stages(store, (primary, escalation), _settings()).extract_pages(session, revision.id)
    session.commit()

    (payload,) = [result.payload for result in results]
    assert payload["agent_regions"] == 1
    assert sum(width >= _whole_label_px() for width in primary.widths) == 1
    assert len(_agent_rows(session)) == 1


def test_a_request_says_whether_the_crop_it_sends_shows_a_stacked_fraction(
    store: LocalStore,
) -> None:
    """**#735 on the agent's reads.** Outcome: the request carries what the crops say of the crop
    it sends, so the validator refuses a reading of a stacked fraction here as it does elsewhere."""
    from extraction.agent.tools import VlmReadingArguments, VlmRole
    from workflow.stages import _AgentReads

    crops = _crops(store, stacked=lambda _polygon: True)
    key = crops.first()
    assert key is not None
    reader = _WholeLabelReader(_config("reader-a"), cut_reading='3/4"')
    reads = _AgentReads(
        session=None,  # type: ignore[arg-type]
        page_index=0,
        crops=crops,
        readers={VlmRole.PRIMARY: reader},
        open_run=lambda _reader: uuid4(),
    )

    reads.read(VlmReadingArguments("r", key, VlmRole.PRIMARY))

    assert reader.stacked == [True]


def test_the_641_case_goes_to_a_reviewer_with_the_whole_reading_beside_the_cut_ones(
    session: Session, store: LocalStore
) -> None:
    """**The #641 failure as it arrives.** Both vision readers were shown the cut crop and agree on
    `92"`. Outcome: the agent widens the crop and reads `10192"` — which disagrees with them, so it
    looks sharper, asks the second reader, and hands the region over rather than pick. It never
    overrides the two readings; it adds its own, both recorded, for the reviewer to see."""
    revision = _revision(session, store, data=SHEET)
    session.commit()
    primary, escalation = _readers(cut_reading='92"')

    results = _stages(store, (primary, escalation), _settings()).extract_pages(session, revision.id)
    session.commit()

    rows = _agent_rows(session)
    assert sorted((row.raw_text, run.extractor) for row, run in rows) == [
        ('10192"', "reader-a"),
        ('10192"', "reader-b"),
    ]
    (payload,) = [result.payload for result in results]
    assert (payload["agent_proposals"], payload["agent_abstentions"]) == (0, 1)
    assert payload["agent_invocations"] == 2
    # The cut readings are still there, unchanged: nothing was picked.
    cut = [
        row.raw_text
        for row in session.execute(select(ObservationCandidate)).scalars()
        if row.raw_text == '92"'
    ]
    assert cut == ['92"', '92"']


def test_a_cut_label_no_route_read_a_numeral_of_is_set_aside_all_the_same(
    session: Session, store: LocalStore
) -> None:
    """**What #792's rule (a) gives up, pinned so it is never given up by accident.** OCR finds only
    `LED` where the file's own paths say the crop cuts a label, and both vision readers read the cut
    crop as `LED` too. Before #792 the agent widened it and read `10192"`. Now a region no route read a numeral
    in is set aside before its geometry is looked at: a value read there is, by construction, not
    what any route saw at that place. The admin chose this on 2026-10-01. Outcome: the region goes to
    a reviewer unread, and the page counts it."""
    revision = _revision(session, store, data=SHEET)
    session.commit()
    primary, escalation = _readers(cut_reading="LED")

    results = _stages(store, (primary, escalation), _settings(), ocr_text="LED").extract_pages(
        session, revision.id
    )
    session.commit()

    assert _agent_rows(session) == []
    assert all(width < _whole_label_px() for width in primary.widths)
    (payload,) = [result.payload for result in results]
    assert (payload["agent_regions"], payload["agent_regions_without_a_numeral"]) == (0, 1)


def _region_statuses(session: Session) -> list[tuple[str, str, str | None, str | None]]:
    return sorted(
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


def test_an_agreement_the_agent_contradicts_is_marked_conflicting_not_left_standing(
    session: Session, store: LocalStore
) -> None:
    """**No region ends with two agreed values.** OCR and both vision readers read the cut crop as
    `92"` and agree, so the first pass marks it as readers agreeing. The agent reads the whole label,
    `10192"`, twice. Outcome: every row of the region is conflicting — a reviewer decides — where
    before this the `92"` agreement and a `10192"` agreement of the agent's own both stood, and
    automatic typing could have sealed either."""
    revision = _revision(session, store, data=SHEET)
    session.commit()
    primary, escalation = _readers(cut_reading='92"')

    _stages(store, (primary, escalation), _settings(), ocr_text='92"').extract_pages(
        session, revision.id
    )
    session.commit()

    statuses = _region_statuses(session)
    assert {text for text, *_ in statuses} == {'92"', '10192"'}
    assert {(status, lane) for *_, status, lane in statuses} == {("CONFLICTING", "SECOND_READER")}


def test_an_agent_reading_that_agrees_leaves_the_agreement_standing(
    session: Session, store: LocalStore
) -> None:
    """Outcome: the whole label was never cut for the readers here, so the agent's reading agrees
    with theirs and nothing is marked conflicting — the check only ever takes an agreement away."""
    revision = _revision(session, store, data=SHEET)
    session.commit()
    primary, escalation = _readers(cut_reading='10192"')

    _stages(store, (primary, escalation), _settings(), ocr_text='10192"').extract_pages(
        session, revision.id
    )
    session.commit()

    statuses = _region_statuses(session)
    assert {text for text, *_ in statuses} == {'10192"'}
    assert all(status != "CONFLICTING" for *_, status, _lane in statuses)


def test_the_escalation_reader_can_be_a_defined_one_the_vision_route_does_not_run(
    store: LocalStore,
) -> None:
    """**#757 D-A2.** Outcome: the agent's escalation is mistral-large-3, found among the defined
    readers, while the vision route reads with only the configured pair."""
    from extraction.agent.tools import VlmRole
    from extraction.models.nova import MISTRAL_LARGE_3_EXTRACTOR

    stages = _stages(
        store,
        (_WholeLabelReader(_config("reader-a")),),
        _settings(escalation_reader=MISTRAL_LARGE_3_EXTRACTOR),
    )

    assert stages._agent_readers[VlmRole.ESCALATION].config.extractor == MISTRAL_LARGE_3_EXTRACTOR
    assert [reader.config.extractor for reader in stages._vision_readers] == ["reader-a"]


def test_a_contradiction_found_after_the_first_readings_were_saved_still_blocks_the_agreement(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The crash of the first end-to-end run (#790).** In a real run the page's panel step saves the
    first readings before the agent runs, and a saved reading is append-only. Outcome: no crash; the
    agent's readings are marked conflicting; the first readers' `92"` agreement keeps the status it
    was saved with — and automatic typing still refuses to lock it in, because its region holds a
    conflict."""
    from app.evidence.automatic_typing import _second_reader_candidate_ids

    original = DatabaseStages._run_bounded_agent_for_ambiguous_regions

    def saved_first(self: DatabaseStages, session: Session, **kwargs: object) -> object:
        session.flush()
        return original(self, session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(DatabaseStages, "_run_bounded_agent_for_ambiguous_regions", saved_first)
    revision = _revision(session, store, data=SHEET)
    session.commit()
    primary, escalation = _readers(cut_reading='92"')

    _stages(store, (primary, escalation), _settings(), ocr_text='92"').extract_pages(
        session, revision.id
    )
    session.commit()

    rows = list(session.execute(select(ObservationCandidate)).scalars())
    agent = [row for row, _run in _agent_rows(session)]
    assert {row.raw_text for row in agent} == {'10192"'}
    assert {row.corroboration_status for row in agent} == {"CONFLICTING"}
    first = next(
        row for row in rows if row.raw_text == '92"' and row.corroboration_lane == "SECOND_READER"
    )
    assert first.corroboration_status != "CONFLICTING"
    assert _second_reader_candidate_ids(session, first) == ()
