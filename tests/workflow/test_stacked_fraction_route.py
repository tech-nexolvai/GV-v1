"""A vision crop known to show a stacked fraction is refused before a model call (#735, #713).

#541 built the guard and tested it by setting the flag by hand. In production the chain was broken
in three places — the geometry was never measured, the region's flag was never carried, and neither
adapter passed it — so the guard never once ran. The workflow now applies that same deterministic
fact before Bedrock: a crop the local geometry already says must go to review is not a paid call.
These tests go through `DatabaseStages` itself: a real stamp is read, a real crop is cut, and the
stage decides whether the reader should see it.

Verification for: `workflow/stages.py` (`_read_page_by_vision`, `_crop_shows_a_stacked_fraction`).
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.coordinates import ImagePoint
from evidence.polygon import Polygon
from extraction.annotations import StackedFraction
from extraction.models.context import AssembledContext
from extraction.models.nova import NovaConfig, NovaInvocation, NovaInvocationOutcome, NovaRequest
from extraction.ocr import OcrItem
from storage.local import LocalStore
from tests.extraction.test_annotations import STACKED_APPEARANCE, _appearance, _pdf, _stamp
from tests.workflow.test_association import LOCALIZED, SETTINGS, _revision, _upgrade
from units.measurement import Unit
from workflow.stages import DatabaseStages, _crop_shows_a_stacked_fraction, _vision_pre_call_refusal

pytest_plugins = ("tests.app.postgres_fixture",)

#: A line-work run for the label to sit beside: `plan_reads` sets aside a region with no line near it.
DIMENSION_LINE = b"1 w 105 515 m 205 515 l S\n"

STACKED_SHEET = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[_appearance(DIMENSION_LINE + STACKED_APPEARANCE)],
)

#: The same label with its fraction taken away — the two whole-number digits and the inch mark.
PLAIN_SHEET = _pdf(
    annotations=[_stamp(appearance_object=6)],
    extra_objects=[
        _appearance(
            DIMENSION_LINE
            + b"0.2 w 110 520 m 113.6 525.5 l 110 525.5 l S\n"
            + b"114.5 520 m 118.1 525.5 l 114.5 525.5 l S\n"
            + b"125.6 527.7 m 126.1 529.3 l S\n"
            + b"127.2 527.7 m 127.7 529.3 l S\n"
        )
    ],
)


class _WholeCropOcr:
    """Reports one reading covering the whole crop it is handed, so the vision crop is the label's."""

    name = "stacked-route-ocr"
    version = "test/1"

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        assert rgb
        return (
            OcrItem(
                text='28"',
                confidence=Decimal("0.9"),
                image_extent=(
                    ImagePoint(0, 0),
                    ImagePoint(width - 1, 0),
                    ImagePoint(width - 1, height - 1),
                    ImagePoint(0, height - 1),
                ),
            ),
        )


@dataclass
class _RecordingVisionReader:
    """A vision reader double that keeps every request it is sent."""

    config: NovaConfig
    requests: list[NovaRequest] = field(default_factory=list)

    def extract(self, request: NovaRequest, recorder: object) -> DomainCandidate:
        self.requests.append(request)
        recorder.record(  # type: ignore[attr-defined]
            NovaInvocation(
                model_id=self.config.model_id,
                prompt_id=self.config.prompt_id,
                template_id=self.config.template_id,
                attempt=1,
                latency_ms=1,
                input_tokens=1,
                output_tokens=1,
                outcome=NovaInvocationOutcome.OK,
                request_id=f"request-{request.candidate_id}",
                context=AssembledContext(nearby_text=(), nearby_geometry=()),
                bound_pt=Decimal(9),
                injection_attempts=(),
            )
        )
        return DomainCandidate(
            candidate_id=request.candidate_id,
            extractor=self.config.extractor,
            extractor_version=self.config.model_id,
            raw_text='28"',
            parsed_value=None,
            unit_guess=Unit.INCH,
            semantic_guess=None,
            page=request.page,
            polygon=(ImagePoint(0, 0), ImagePoint(1, 0), ImagePoint(1, 1), ImagePoint(0, 1)),
            confidence=None,
            ambiguity_flags=(),
        )


def _reader() -> _RecordingVisionReader:
    return _RecordingVisionReader(
        NovaConfig(
            model_id="stacked-route-vision/v1",
            prompt_id="dimension-reader-v1",
            template_id="bounded-crop-v1",
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_attempts=1,
            extractor="stacked-route-vision",
        )
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


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _vision_result_for(
    session: Session, store: LocalStore, data: bytes
) -> tuple[list[NovaRequest], dict[str, object]]:
    revision = _revision(session, store, data=data)
    session.commit()
    reader = _reader()
    (result,) = DatabaseStages(
        store,
        dpi=150,
        association=replace(SETTINGS, proximity_limit=Decimal("0.9")),
        ocr_engine=_WholeCropOcr(),
        localized_ocr=LOCALIZED,
        vision_readers=(reader,),
    ).extract_pages(session, revision.id)
    session.commit()
    return reader.requests, dict(result.payload)


def _requests_for(session: Session, store: LocalStore, data: bytes) -> list[NovaRequest]:
    requests, _ = _vision_result_for(session, store, data)
    return requests


def test_a_crop_that_shows_a_stacked_fraction_is_skipped_before_the_model_call(
    session: Session, store: LocalStore
) -> None:
    """A real stamp, the real reader, a real crop: the guaranteed rejection is not a paid call."""
    requests, payload = _vision_result_for(session, store, STACKED_SHEET)

    assert requests == []
    assert payload["vision_invocations"] == 0
    assert payload["vision_candidates"] == 0
    refusals = payload["vision_refusals"]
    assert isinstance(refusals, list)
    assert any("stacked_fraction_requires_review" in str(refusal) for refusal in refusals)


def test_the_same_label_without_its_fraction_is_not(session: Session, store: LocalStore) -> None:
    """The control: the flag comes from the drawing, not from every crop being marked."""
    requests, payload = _vision_result_for(session, store, PLAIN_SHEET)

    assert requests, "no region reached the vision reader, so this test proves nothing"
    assert not any(request.stacked_label for request in requests)
    assert payload["vision_invocations"] == len(requests)


# ---------------------------------------------------------------------------
# Whether a crop shows a fraction: its pixels against the fraction's
# ---------------------------------------------------------------------------


def _fraction(left: int, top: int, right: int, bottom: int) -> StackedFraction:
    from uuid import uuid4

    from evidence.coordinates import StoredPoint

    return StackedFraction(
        extent=Polygon(
            points=(
                StoredPoint(x=Decimal("0.1"), y=Decimal("0.1")),
                StoredPoint(x=Decimal("0.2"), y=Decimal("0.1")),
                StoredPoint(x=Decimal("0.2"), y=Decimal("0.2")),
                StoredPoint(x=Decimal("0.1"), y=Decimal("0.2")),
            ),
            space="stored",
            document_version_id=uuid4(),
            page=0,
        ),
        image_extent=(
            ImagePoint(left, top),
            ImagePoint(right, top),
            ImagePoint(right, bottom),
            ImagePoint(left, bottom),
        ),
    )


def test_a_fraction_inside_the_crop_is_shown() -> None:
    assert _crop_shows_a_stacked_fraction((100, 100, 200, 200), [_fraction(120, 120, 140, 160)])


def test_a_fraction_half_inside_the_crop_is_shown() -> None:
    """A model reads whatever it is shown, and a numerator at the edge can still be promoted into a
    whole number. Any part counts."""
    assert _crop_shows_a_stacked_fraction((100, 100, 200, 200), [_fraction(190, 150, 230, 190)])


def test_a_fraction_touching_the_crop_edge_is_shown() -> None:
    assert _crop_shows_a_stacked_fraction((100, 100, 200, 200), [_fraction(200, 150, 230, 190)])


def test_a_fraction_elsewhere_on_the_page_is_not() -> None:
    assert not _crop_shows_a_stacked_fraction((100, 100, 200, 200), [_fraction(300, 300, 340, 360)])


def test_no_fractions_nothing_shown() -> None:
    assert not _crop_shows_a_stacked_fraction((100, 100, 200, 200), [])


def test_stacked_crop_has_a_pre_call_refusal() -> None:
    """The model call would be rejected after it returns, so the workflow can skip it before."""

    assert (
        _vision_pre_call_refusal((100, 100, 200, 200), [_fraction(120, 120, 140, 160)])
        == "stacked_fraction_requires_review"
    )


def test_unstacked_crop_has_no_pre_call_refusal() -> None:
    assert _vision_pre_call_refusal((100, 100, 200, 200), []) is None
