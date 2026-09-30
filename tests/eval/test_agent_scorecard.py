"""The reading agent's scorecard: what each arm hands a reviewer, and how it is counted (#757).

Verification for: `eval/experiments/agent_scorecard.py`.

The one that matters most is `test_the_641_case_goes_to_a_reviewer`: two readers agreeing on the part
of a label a crop showed, and the agent reading the whole of it, is a disagreement a reviewer
decides — never a confirmed value — exactly as the stage now judges it (#771).

No model is called and no client drawing is read: the drawing is the made-up one of
`tests/workflow/test_reading_agent.py`, and the readers stand in for models.
"""

from __future__ import annotations

import csv
import json
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
    Judgement,
    KeyCrop,
    Kind,
    Outcome,
    PageGeometry,
    Reading,
    ScorecardError,
    build_pages,
    judge,
    load_key,
    render_markdown,
    score_crop,
    tally,
    vendor_of,
)
from evidence.crop import decode_rgb_png
from extraction.agent.tools import VlmRole
from extraction.agent.trigger import AmbiguityReason
from storage.local import LocalStore
from tests.workflow.test_association import SETTINGS
from tests.workflow.test_reading_agent import LABEL, SHEET, _settings
from units.measurement import Measurement, Unit
from workflow.stages import VISION_CROP_CONTEXT_MARGIN_PT


def _inches(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, f'{value}"')


#: The readers' real model ids: agreement is judged by vendor, read from the id (#775).
MODEL_IDS = {
    "bedrock-nova-2-lite": "amazon.nova-2-lite-v1:0",
    "bedrock-ministral-3-3b": "mistral.ministral-3-3b-instruct",
    "bedrock-mistral-large-3": "mistral.mistral-large-3-675b-instruct",
}


def _read(extractor: str, text: str | None, value: int | None = None) -> Reading:
    vendor = "Amazon" if "nova" in extractor else "Mistral"
    return Reading(
        extractor=extractor,
        vendor=vendor,
        model_id=MODEL_IDS[extractor],
        raw_text=text,
        value=None if value is None else _inches(value),
        refusal=None if text is not None else "refused",
        input_tokens=100,
        output_tokens=10,
        calls=1,
    )


NOVA, MINI, LARGE = "bedrock-nova-2-lite", "bedrock-ministral-3-3b", "bedrock-mistral-large-3"


# ---------------------------------------------------------------------------
# What a reviewer is handed
# ---------------------------------------------------------------------------


def test_the_pair_agreeing_is_confirmed_by_two_vendors() -> None:
    judgement = judge((_read(NOVA, '12"', 12), _read(MINI, '12"', 12)))

    assert judgement.outcome is Outcome.CONFIRMED and judgement.value == _inches(12)
    assert judgement.vendors == frozenset({"Amazon", "Mistral"})


def test_the_pair_disagreeing_goes_to_a_reviewer_with_nothing_chosen() -> None:
    judgement = judge((_read(NOVA, '12"', 12), _read(MINI, '13"', 13)))

    assert judgement.outcome is Outcome.TO_REVIEWER and judgement.conflict
    assert judgement.value is None


def test_one_value_with_nothing_against_it_is_proposed_not_confirmed() -> None:
    judgement = judge((_read(NOVA, '12"', 12), _read(MINI, None)))

    assert judgement.outcome is Outcome.PROPOSED and judgement.value == _inches(12)


def test_the_641_case_goes_to_a_reviewer() -> None:
    """**#641, as the stage now judges it (#771).** Outcome: the pair's agreement on the cut part and
    the agent's two readings of the whole are a conflict — not two confirmed values."""
    judgement = judge(
        (_read(NOVA, '92"', 92), _read(MINI, '92"', 92)),
        looks=(_read(NOVA, '10192"', 10192), _read(LARGE, '10192"', 10192)),
    )

    assert judgement.outcome is Outcome.TO_REVIEWER and judgement.conflict


def test_an_agent_reading_that_agrees_with_one_pair_reading_confirms_it() -> None:
    """Outcome: the second pass groups the agent's reading with the pair's unconfirmed one — a new
    look adds a candidate, and agreement between different readers confirms (DESIGN_AI §3.2)."""
    judgement = judge((_read(NOVA, None), _read(MINI, '12"', 12)), looks=(_read(NOVA, '12"', 12),))

    assert judgement.outcome is Outcome.CONFIRMED
    assert judgement.agreeing == (MINI, NOVA)


def test_a_pair_reading_the_parser_refused_blocks_an_agreement_but_not_a_proposal() -> None:
    """Outcome: the stage groups every row of the region, so an unparsed row keeps the agent's and
    the pair's values from confirming each other; the agent's proposal is still handed over."""
    look = _read(NOVA, '12"', 12)
    judgement = judge(
        (_read(NOVA, "see detail"), _read(MINI, '12"', 12)), looks=(look,), proposal=look
    )

    assert judgement.outcome is Outcome.PROPOSED and judgement.value == _inches(12)


def test_two_readers_of_one_vendor_agreeing_confirm_nothing() -> None:
    """**The #757 scorecard's one confirmed wrong value, fixed (#775).** Ministral 3B and
    mistral-large-3 both read `2'` for a `2"` label. Outcome: two Mistral models agreeing is one
    vendor's reading — proposed for a person to confirm, never confirmed on its own."""
    judgement = judge((_read(MINI, "2'", 24),), looks=(_read(LARGE, "2'", 24),))

    assert judgement.outcome is Outcome.PROPOSED
    assert judgement.value == _inches(24)


def test_nothing_read_is_handed_over_empty() -> None:
    assert judge((_read(NOVA, None), _read(MINI, None))) == Judgement(Outcome.TO_REVIEWER, None)


def test_vendors_come_from_the_model_id() -> None:
    assert vendor_of("us.amazon.nova-2-lite-v1:0") == "Amazon"
    assert vendor_of("mistral.mistral-large-3-675b-instruct") == "Mistral"
    with pytest.raises(ScorecardError):
        vendor_of("someone.else-1")


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def _write_key(folder: Path, rows: list[dict[str, str]], *, annotator: str = "a person") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "sheet.pdf").write_bytes(SHEET)
    observations = [
        {
            "semantic_type": "CT007",
            "source": "SHOP",
            "value": {"exact": row["exact"], "unit": "in", "raw_text": row["value"]},
            "page": int(row["page"]),
            "polygon": [int(row[k]) for k in ("left_px", "top_px", "right_px", "bottom_px")],
            "item_id": row["crop_id"],
        }
        for row in rows
        if row.get("exact")
    ]
    (folder / "answer_key.json").write_text(
        json.dumps(
            {
                "id": "synthetic-agent-key",
                "product_type": "cabinet",
                "arch": "sheet.pdf",
                "shop": "sheet.pdf",
                "ground_truth": {
                    "observations": observations,
                    "matches": [],
                    "expected_findings": [],
                },
                "provenance": {
                    "annotator": annotator,
                    "annotated_on": "2026-10-01",
                    "documents": [
                        {
                            "source": "SHOP",
                            "document_version_id": "11111111-1111-4111-8111-111111111111",
                            "content_hash": "sha256:" + "a" * 64,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    columns = [
        "crop_id",
        "page",
        "left_px",
        "top_px",
        "right_px",
        "bottom_px",
        "stratum",
        "value",
        "unreadable",
        "not_a_single_value",
        "rotated",
        "note",
    ]
    with (folder / "crops.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return folder


def _row(crop_id: str, **changes: str) -> dict[str, str]:
    values = {
        "crop_id": crop_id,
        "page": "1",
        "left_px": "10",
        "top_px": "10",
        "right_px": "400",
        "bottom_px": "300",
        "stratum": "dimension_label",
        "value": "",
        "unreadable": "",
        "not_a_single_value": "",
        "rotated": "",
        "note": "",
    }
    values.update(changes)
    return values


def test_the_key_is_read_with_every_crop_kind(tmp_path: Path) -> None:
    folder = _write_key(
        tmp_path / "key",
        [
            _row("k0", value='12"', exact="12"),
            _row("k1", unreadable="x", note="cut off; shows part of it"),
            _row("k2", not_a_single_value="x", stratum="compound"),
            _row("k3", value='3/4"', exact="3/4", stratum="rotated"),
        ],
    )

    crops = load_key(folder)

    assert [crop.kind for crop in crops] == [
        Kind.SCORED,
        Kind.UNREADABLE,
        Kind.COMPOUND,
        Kind.SCORED,
    ]
    assert crops[0].expected == Measurement(Fraction(12), Unit.INCH, '12"')
    assert crops[1].cut_off and crops[1].expected is None
    assert crops[3].rotated


def test_a_key_a_machine_verified_is_refused(tmp_path: Path) -> None:
    folder = _write_key(
        tmp_path / "key", [_row("k0", value='12"', exact="12")], annotator="self-verified"
    )

    with pytest.raises(ScorecardError, match="a person read"):
        load_key(folder)


def test_a_scored_crop_missing_from_the_answer_key_is_refused(tmp_path: Path) -> None:
    folder = _write_key(tmp_path / "key", [_row("k0", value='12"')])

    with pytest.raises(ScorecardError, match="k0"):
        load_key(folder)


# ---------------------------------------------------------------------------
# One crop, end to end
# ---------------------------------------------------------------------------

KEY_DPI = 600
STAGE_DPI = 150
DOCUMENT = UUID("11111111-1111-4111-8111-111111111111")
PAGE_HEIGHT_PT = 300


def _label_box_pt() -> tuple[Decimal, Decimal, Decimal, Decimal]:
    xs = [x - 50 for path in LABEL for x, _ in path.points]
    ys = [y - 450 for path in LABEL for _, y in path.points]
    return (min(xs), min(ys), max(xs), max(ys))


def _cut_crop() -> KeyCrop:
    """The #641 crop: round the last two digits and the inch mark, cut as the stage cuts one."""
    paths = LABEL[3:]
    left = min(x for path in paths for x, _ in path.points) - 50
    right = max(x for path in paths for x, _ in path.points) - 50
    bottom = min(y for path in paths for _, y in path.points) - 450
    top = max(y for path in paths for _, y in path.points) - 450
    scale = Fraction(KEY_DPI, 72)
    margin = Fraction(VISION_CROP_CONTEXT_MARGIN_PT) * scale
    return KeyCrop(
        crop_id="k0",
        page_index=0,
        crop_px=(
            int(Fraction(left) * scale - margin),
            int(Fraction(PAGE_HEIGHT_PT - top) * scale - margin),
            int(Fraction(right) * scale + margin) + 1,
            int(Fraction(PAGE_HEIGHT_PT - bottom) * scale + margin) + 1,
        ),
        kind=Kind.SCORED,
        expected=Measurement(Fraction(10192), Unit.INCH, '10192"'),
        stratum="dimension_label",
        rotated=False,
        cut_off=True,
    )


def _whole_label_px() -> int:
    left, _, right, _ = _label_box_pt()
    return int((right - left + 2 * VISION_CROP_CONTEXT_MARGIN_PT) * STAGE_DPI / 72) - 2


@dataclass
class _Reader:
    """Reads `10192"` from a crop wide enough to show the whole label; refuses any narrower one."""

    extractor: str
    vendor: str = "Amazon"
    widths: list[int] = field(default_factory=list)

    def read(self, png: bytes, *, stacked_label: bool) -> Reading:
        del stacked_label
        width, _, _ = decode_rgb_png(png)
        self.widths.append(width)
        whole = width >= _whole_label_px()
        return Reading(
            extractor=self.extractor,
            vendor=self.vendor,
            model_id=MODEL_IDS[self.extractor],
            raw_text='10192"' if whole else None,
            value=Measurement(Fraction(10192), Unit.INCH, '10192"') if whole else None,
            refusal=None if whole else "the crop does not show a whole dimension",
            input_tokens=100,
            output_tokens=10,
            calls=1,
        )


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def test_a_cut_crop_the_pair_refuses_is_widened_by_the_agent_and_proposed_whole(
    store: LocalStore,
) -> None:
    """**End to end, on the stage's own geometry.** Outcome: the pair refuses the cut crop, so alone
    it hands the reviewer nothing; the file says the crop cut the label, so the agent — as built —
    widens it, the primary reads `10192"`, and that is proposed, right."""
    geometry = PageGeometry(
        line_minimum_pt=SETTINGS.line_minimum_pt,
        glyph_maximum_pt=SETTINGS.glyph_maximum_pt,
        glyph_gap_pt=SETTINGS.glyph_gap_pt,
        fraction_bar=SETTINGS.fraction_bar,
    )
    settings = _settings(sharper_dpi=300, primary_reader=NOVA, escalation_reader=LARGE)
    pages = build_pages(
        SHEET,
        [0],
        version_id=DOCUMENT,
        dpi=STAGE_DPI,
        geometry=geometry,
        reach=settings.reach(geometry.glyph_gap_pt),
    )
    nova, mini, large = _Reader(NOVA), _Reader(MINI, "Mistral"), _Reader(LARGE, "Mistral")

    result = score_crop(
        _cut_crop(),
        pages[0],
        store=store,
        pair=(nova, mini),
        readers={VlmRole.PRIMARY: nova, VlmRole.ESCALATION: large},
        settings=settings,
        key_dpi=KEY_DPI,
        margin_pt=VISION_CROP_CONTEXT_MARGIN_PT,
    )

    assert result.facts.cut_at_edge
    assert AmbiguityReason.LABEL_CUT_AT_EDGE in result.reasons
    assert result.judgements[Arm.PAIR].outcome is Outcome.TO_REVIEWER
    assert result.agent is not None
    assert result.agent.steps == ("refine-whole_run", "vlm-primary")
    for arm in (Arm.AGENT, Arm.AGENT_EVERYWHERE):
        assert result.judgements[arm].outcome is Outcome.PROPOSED
        assert result.judgements[arm].value == Measurement(Fraction(10192), Unit.INCH, '10192"')
    assert large.widths == []

    counts = tally([result], Arm.AGENT)
    assert (counts.proposed_right, counts.agent_runs, counts.calls) == (1, 1, 3)
    assert tally([result], Arm.PAIR).to_reviewer == 1


def test_the_scorecard_puts_wrong_readings_first() -> None:
    """**Acceptance criterion: wrong readings are reported first.**"""
    crop = _cut_crop()
    from eval.experiments.agent_scorecard import CropResult
    from extraction.agent.observations import RegionFacts

    wrong = Judgement(Outcome.CONFIRMED, _inches(92), agreeing=(MINI, NOVA), vendors=frozenset())
    result = CropResult(
        crop=crop,
        pair=(_read(NOVA, '92"', 92), _read(MINI, '92"', 92)),
        facts=RegionFacts(False, 0, False, None, ()),
        reasons=frozenset(),
        agent=None,
        judgements={arm: wrong for arm in Arm},
        agent_ran={arm: False for arm in Arm},
    )

    rendered = render_markdown([result], rates=lambda _m, i, o: i + o, header="# Scorecard")

    header = next(line for line in rendered.splitlines() if line.startswith("| Arm | **"))
    assert header.index("Confirmed but wrong") < header.index("Confirmed right")
    assert "| pair alone | **1** | **0** |" in rendered


def test_a_throttle_is_waited_out_and_never_scored_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The quota is not the model** (#716). Outcome: the first call is throttled, the reader waits
    and asks again, and the reading counts — with both calls it cost."""
    from eval.experiments.agent_scorecard import BedrockCropReader
    from evidence.candidate import ObservationCandidate as DomainCandidate
    from evidence.coordinates import ImagePoint
    from extraction.models import nova
    from extraction.models.nova import NovaRetryExhaustedError, vision_config_for_extractor

    class _Adapter:
        def __init__(self, recorder: object) -> None:
            self.recorder = recorder
            self.calls = 0

        def extract(self, request: nova.NovaRequest) -> DomainCandidate:
            self.calls += 1
            self.recorder.record(  # type: ignore[attr-defined]
                type("Invocation", (), {"input_tokens": 50, "output_tokens": 5})()
            )
            if self.calls == 1:
                raise NovaRetryExhaustedError("ThrottlingException")
            return DomainCandidate(
                candidate_id=request.candidate_id,
                extractor=NOVA,
                extractor_version="m",
                raw_text='12"',
                parsed_value=None,
                unit_guess=Unit.INCH,
                semantic_guess=None,
                page=0,
                polygon=(ImagePoint(0, 0), ImagePoint(1, 0), ImagePoint(1, 1)),
                confidence=None,
                ambiguity_flags=(),
            )

    monkeypatch.setattr(
        nova.NovaAdapter, "from_environment", staticmethod(lambda config, sink: _Adapter(sink))
    )
    slept: list[float] = []
    config = vision_config_for_extractor(NOVA)
    assert config is not None
    reader = BedrockCropReader(
        config, calls_per_minute=60, waits_seconds=(30,), clock=lambda: 0.0, sleep=slept.append
    )

    reading = reader.read(b"png", stacked_label=False)

    assert reading.raw_text == '12"' and reading.value == _inches(12)
    assert reading.refusal is None
    assert (reading.calls, reading.input_tokens) == (2, 100)
    assert 30 in slept
    assert reading.vendor == "Amazon"
