"""The scorecard shows each pair reader its own picture, scores each alone, and keeps to its cap (#907).

Verification for: `eval/experiments/agent_scorecard.py` (`score_crop`'s pictures, `reader_tallies`,
`SpendCap`, `Pacer`) and `scripts/agent_scorecard.py`'s cap.

**The upright turn is the drawing's, never the key's.** The trial turned a crop where the person had
ticked it sideways; production has no person's tick, so neither does the scorecard: a label the file
prints up the page is turned though the key says nothing, and one printed across is not turned though
the key ticks it. No model is called and no client drawing is read.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest

from eval.experiments.agent_scorecard import (
    Arm,
    CropResult,
    Judgement,
    KeyCrop,
    Kind,
    Outcome,
    Pacer,
    PageGeometry,
    Reading,
    SpendCap,
    SpendCapReached,
    build_pages,
    reader_tallies,
    render_markdown,
    score_crop,
)
from evidence.crop import decode_rgb_png
from extraction.agent.observations import RegionFacts
from extraction.glyph_bands import FractionLayout
from extraction.models.nova import ReaderPicture
from extraction.reader import read_page_contents
from extraction.vector_first import upright_png
from storage.local import LocalStore
from tests.eval.test_agent_scorecard import _cut_crop, _read
from tests.workflow.test_association import SETTINGS
from tests.workflow.test_reader_pictures import SHEET
from tests.workflow.test_reading_agent import _settings
from units.measurement import Measurement, Unit
from workflow.reader_pictures import PictureSettings, sharper_box
from workflow.stages import VISION_CROP_CONTEXT_MARGIN_PT

DOCUMENT = UUID("22222222-2222-4222-8222-222222222222")
KEY_DPI, STAGE_DPI, SHARPER_DPI = 600, 300, 900
NOVA, QWEN = "bedrock-nova-2-lite-taught", "bedrock-qwen3-vl-235b"


@dataclass
class _Shown:
    """A pair reader that records each picture it is shown and reads one fixed label."""

    extractor: str
    vendor: str
    picture: ReaderPicture
    shown: list[bytes] = field(default_factory=list)

    def read(
        self, png: bytes, *, stacked_label: bool, stacked_layouts: tuple[FractionLayout, ...]
    ) -> Reading:
        del stacked_label, stacked_layouts
        self.shown.append(png)
        return Reading(
            extractor=self.extractor,
            vendor=self.vendor,
            model_id="qwen.qwen3-vl-235b-a22b" if "qwen" in self.extractor else "amazon.x",
            raw_text='24"',
            value=Measurement(Fraction(24), Unit.INCH, '24"'),
            refusal=None,
            input_tokens=1,
            output_tokens=1,
            calls=1,
        )


def _key_crop(text: str, *, ticked_sideways: bool) -> KeyCrop:
    """The key's crop round one printed label, cut with the stage's margin at the key's 600 dpi."""
    item = next(
        item
        for item in read_page_contents(SHEET, 0, document_version_id=DOCUMENT, dpi=KEY_DPI).texts
        if item.text == text
    )
    margin = int(VISION_CROP_CONTEXT_MARGIN_PT * KEY_DPI / 72)
    xs = [point.x for point in item.image_extent]
    ys = [point.y for point in item.image_extent]
    return KeyCrop(
        crop_id=text,
        page_index=0,
        crop_px=(min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin),
        kind=Kind.SCORED,
        expected=Measurement(Fraction(24), Unit.INCH, '24"'),
        stratum="sideways",
        rotated=ticked_sideways,
        cut_off=False,
    )


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _score(store: LocalStore, crop: KeyCrop) -> tuple[CropResult, _Shown, _Shown]:
    geometry = PageGeometry(
        line_minimum_pt=SETTINGS.line_minimum_pt,
        glyph_maximum_pt=SETTINGS.glyph_maximum_pt,
        glyph_gap_pt=SETTINGS.glyph_gap_pt,
        fraction_bar=SETTINGS.fraction_bar,
    )
    settings = _settings(sharper_dpi=450, primary_reader=NOVA, escalation_reader=QWEN)
    pages = build_pages(
        SHEET,
        [0],
        version_id=DOCUMENT,
        dpi=STAGE_DPI,
        geometry=geometry,
        reach=settings.reach(geometry.glyph_gap_pt),
    )
    qwen = _Shown(QWEN, "Qwen", ReaderPicture.AS_CUT)
    nova = _Shown(NOVA, "Amazon", ReaderPicture.UPRIGHT_SHARPER)
    result = score_crop(
        crop,
        pages[0],
        store=store,
        pair=(qwen, nova),
        readers={},
        settings=settings,
        key_dpi=KEY_DPI,
        margin_pt=VISION_CROP_CONTEXT_MARGIN_PT,
        pictures=PictureSettings(sharper_dpi=SHARPER_DPI),
        run_agent=False,
    )
    return result, qwen, nova


def test_a_label_the_file_prints_sideways_is_turned_though_the_key_does_not_say_so(
    store: LocalStore,
) -> None:
    """**Outcome: Nova 2 Lite is shown the label upright and sharper; Qwen the crop as cut.** The
    key does not tick this crop sideways; the file's own text runs up the page, and that decides."""
    result, qwen, nova = _score(store, _key_crop('24 1/2"', ticked_sideways=False))

    assert result.turn == 90
    (as_cut,) = qwen.shown
    (turned,) = nova.shown
    cut_width, cut_height, _ = decode_rgb_png(as_cut)
    assert cut_height > cut_width, "the label runs up the page in the crop as cut"
    width, height, _ = decode_rgb_png(turned)
    assert (width, height) == (cut_height * 3, cut_width * 3)
    assert decode_rgb_png(upright_png(turned, label_rotation_degrees=270))[:2] == (
        cut_width * 3,
        cut_height * 3,
    )


def test_a_label_the_file_prints_across_is_not_turned_though_the_key_ticks_it(
    store: LocalStore,
) -> None:
    result, qwen, nova = _score(store, _key_crop('36"', ticked_sideways=True))

    assert result.turn == 0
    (as_cut,) = qwen.shown
    (sharper,) = nova.shown
    cut_width, cut_height, _ = decode_rgb_png(as_cut)
    assert decode_rgb_png(sharper)[:2] == (cut_width * 3, cut_height * 3)


def test_the_sharper_picture_is_the_page_area_rendered_not_the_crop_enlarged(
    store: LocalStore,
) -> None:
    """**Rendered, never upscaled.** The picture Nova is shown is the page rendered at 900 dpi over
    the crop's own area — what `ScorecardPage.render_region` draws — not the 300 dpi crop grown."""
    from extraction.rasterise import render_region
    from workflow.stages import MAXIMUM_RENDER_PIXELS, crop_box_px

    crop = _key_crop('36"', ticked_sideways=False)
    result, qwen, nova = _score(store, crop)
    geometry = PageGeometry(
        line_minimum_pt=SETTINGS.line_minimum_pt,
        glyph_maximum_pt=SETTINGS.glyph_maximum_pt,
        glyph_gap_pt=SETTINGS.glyph_gap_pt,
        fraction_bar=SETTINGS.fraction_bar,
    )
    settings = _settings(sharper_dpi=450, primary_reader=NOVA, escalation_reader=QWEN)
    page = build_pages(
        SHEET,
        [0],
        version_id=DOCUMENT,
        dpi=STAGE_DPI,
        geometry=geometry,
        reach=settings.reach(geometry.glyph_gap_pt),
    )[0]
    from eval.experiments.agent_scorecard import _region_px

    box = tuple(_region_px(crop, key_dpi=KEY_DPI, dpi=STAGE_DPI, margin_pt=Decimal(9)))
    polygon = page.polygon(box)  # type: ignore[arg-type]
    assert polygon is not None
    area = sharper_box(
        crop_box_px(page.rendered, polygon, VISION_CROP_CONTEXT_MARGIN_PT),
        base_dpi=STAGE_DPI,
        dpi=SHARPER_DPI,
    )
    expected = render_region(
        SHEET,
        0,
        box_px=area,
        dpi=SHARPER_DPI,
        maximum_pixels=MAXIMUM_RENDER_PIXELS,
        vendor_only=True,
    )
    assert nova.shown == [expected]
    assert result.turn == 0 and qwen.shown


def test_a_pair_scored_alone_runs_no_agent(store: LocalStore) -> None:
    """`run_agent=False`: no agent call is made, and both agent arms are the pair's judgement."""
    result, _qwen, _nova = _score(store, _key_crop('36"', ticked_sideways=False))

    assert result.agent is None
    assert set(result.agent_ran.values()) == {False}
    assert len({result.judgements[arm] for arm in Arm}) == 1


# ---------------------------------------------------------------------------
# Each reader alone
# ---------------------------------------------------------------------------


def _result(kind: Kind, expected: int | None, *pair: Reading) -> CropResult:
    crop = _cut_crop()
    return CropResult(
        crop=KeyCrop(
            crop_id=f"c{len(pair)}{expected}",
            page_index=0,
            crop_px=crop.crop_px,
            kind=kind,
            expected=None if expected is None else Measurement(Fraction(expected), Unit.INCH, ""),
            stratum="plain",
            rotated=False,
            cut_off=False,
        ),
        pair=pair,
        facts=RegionFacts(False, 0, False, None, ()),
        reasons=frozenset(),
        agent=None,
        judgements={arm: Judgement(Outcome.TO_REVIEWER, None) for arm in Arm},
        agent_ran={arm: False for arm in Arm},
    )


def test_each_reader_is_scored_alone_right_wrong_or_no_value() -> None:
    nova, mini = "bedrock-nova-2-lite", "bedrock-ministral-3-3b"
    results = [
        _result(Kind.SCORED, 9, _read(nova, '9"', 9), _read(mini, '37"', 37)),
        _result(Kind.SCORED, 9, _read(nova, None), _read(mini, "abc")),
        _result(Kind.UNREADABLE, None, _read(nova, '5"', 5), _read(mini, None)),
    ]

    tallies = reader_tallies(results)

    assert list(tallies) == [nova, mini]
    assert (tallies[nova].right, tallies[nova].wrong, tallies[nova].no_value) == (1, 0, 1)
    assert (tallies[mini].right, tallies[mini].wrong, tallies[mini].no_value) == (0, 1, 1)
    assert (tallies[nova].valued_unvouched, tallies[mini].valued_unvouched) == (1, 0)
    rendered = render_markdown(results, rates=lambda _m, i, o: i + o, header="# Scorecard")
    assert f"| {nova} | 1 | **0** | 1 | 1 |" in rendered
    assert f"| {mini} | 0 | **1** | 1 | 0 |" in rendered


# ---------------------------------------------------------------------------
# The cap, and one quota per model
# ---------------------------------------------------------------------------


def test_the_cap_refuses_the_call_after_it_is_reached() -> None:
    cap = SpendCap(cap_micros=100, price=lambda model, tokens_in, tokens_out: tokens_in)

    cap.check()
    cap.add("m", 60, 0)
    cap.check()
    cap.add("m", 40, 0)
    with pytest.raises(SpendCapReached, match="cap"):
        cap.check()
    assert (cap.spent_micros, cap.calls, cap.tripped) == (100, 2, True)


def test_a_call_whose_price_is_unknown_is_not_spent_unseen() -> None:
    from eval.experiments.agent_scorecard import ScorecardError

    cap = SpendCap(cap_micros=100, price=lambda *_: None)
    with pytest.raises(ScorecardError, match="no price"):
        cap.add("m", 1, 1)


def test_a_reader_over_its_cap_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """**Enforced before the call, by the reader.** Outcome: once the run has spent its cap, the
    next read raises before the adapter is asked; every call made was added to the spend."""
    from eval.experiments.agent_scorecard import BedrockCropReader
    from evidence.candidate import ObservationCandidate as DomainCandidate
    from evidence.coordinates import ImagePoint
    from extraction.models import nova
    from extraction.models.nova import vision_config_for_extractor

    calls: list[object] = []

    class _Adapter:
        def __init__(self, recorder: object) -> None:
            self.recorder = recorder

        def extract(self, request: nova.NovaRequest) -> DomainCandidate:
            calls.append(request)
            self.recorder.record(  # type: ignore[attr-defined]
                type("Invocation", (), {"input_tokens": 70, "output_tokens": 0, "model_id": "m"})()
            )
            return DomainCandidate(
                candidate_id=request.candidate_id,
                extractor=QWEN,
                extractor_version="m",
                raw_text='9"',
                parsed_value=None,
                unit_guess=None,
                semantic_guess=None,
                page=0,
                polygon=(ImagePoint(0, 0), ImagePoint(1, 0), ImagePoint(1, 1)),
                confidence=None,
                ambiguity_flags=(),
            )

    monkeypatch.setattr(
        nova.NovaAdapter, "from_environment", staticmethod(lambda config, sink: _Adapter(sink))
    )
    config = vision_config_for_extractor(QWEN)
    assert config is not None
    cap = SpendCap(cap_micros=100, price=lambda model, tokens_in, tokens_out: tokens_in)
    reader = BedrockCropReader(
        config, calls_per_minute=60, clock=lambda: 0.0, sleep=lambda _s: None, cap=cap
    )

    reader.read(b"png", stacked_label=False, stacked_layouts=())
    reader.read(b"png", stacked_label=False, stacked_layouts=())
    with pytest.raises(SpendCapReached):
        reader.read(b"png", stacked_label=False, stacked_layouts=())

    assert len(calls) == 2
    assert (cap.spent_micros, cap.calls) == (140, 2)
    assert reader.picture is ReaderPicture.AS_CUT


def test_two_readers_of_one_model_share_its_quota() -> None:
    """Nova 2 Lite's two readers are one model's quota (#716): one pacer spaces both."""
    clock = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    pacer = Pacer(20, clock=lambda: clock[0], sleep=sleep)
    pacer.wait()
    pacer.wait()
    pacer.wait()

    assert slept == [3.0, 3.0]
