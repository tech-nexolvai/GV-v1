from __future__ import annotations

import json
import re
import time
from decimal import Decimal
from uuid import uuid4

import pytest

from evidence.coordinates import PageTransform, StoredPoint
from evidence.polygon import Polygon
from extraction.form_reader.agreement import compare_page_answers
from extraction.form_reader.bedrock import build_converse_request, read_page
from extraction.form_reader.locator import locate_box
from extraction.form_reader.mapping import map_page_to_fields
from extraction.form_reader.parser import parse_dimension, validate_page_answer
from extraction.form_reader.pricing import require_priced_readers
from extraction.form_reader.runner import (
    ThreadLocalConverseClients,
    ThreadSafeAttemptRecorder,
    read_pages_parallel,
)
from extraction.form_reader.schema import (
    CountertopForm,
    FormDimension,
    NormalizedBox,
    PageFormAnswer,
)
from workflow.form_reader import FormPageImage, read_form_pages


def _dimension(
    text: str | None = '4"',
    *,
    whole: int | None = 4,
    numerator: int | None = None,
    denominator: int | None = 1,
    stacked: bool = False,
    combined: bool = False,
    readable: bool = True,
    kind: str = "unknown",
    position: int = 1,
) -> FormDimension:
    return FormDimension(
        text=text,
        whole=whole,
        numerator=numerator,
        denominator=denominator,
        stacked=stacked,
        kind=kind,
        combined=combined,
        readable=readable,
        box=NormalizedBox(x0=100, y0=100, x1=200, y1=200),
        position=position,
    )


def _polygon(x0: str, y0: str, x1: str, y1: str, version, page: int = 0) -> Polygon:
    return Polygon(
        points=(
            StoredPoint(Decimal(x0), Decimal(y0)),
            StoredPoint(Decimal(x1), Decimal(y0)),
            StoredPoint(Decimal(x1), Decimal(y1)),
            StoredPoint(Decimal(x0), Decimal(y1)),
        ),
        space="stored",
        document_version_id=version,
        page=page,
    )


def _transform() -> PageTransform:
    page = (Decimal(0), Decimal(0), Decimal(72), Decimal(72))
    return PageTransform(dpi=1000, rotation=0, media_box=page, crop_box=page)


def _answer(*, overall: FormDimension | None, scope: str | None, chain=()) -> PageFormAnswer:
    return PageFormAnswer(
        page_index=2,
        countertops=[
            CountertopForm(
                overall=overall,
                overall_scope=scope,
                chain=list(chain),
            )
        ],
    )


def test_exact_value_is_parsed_from_printed_text_not_model_components() -> None:
    parsed = parse_dimension(_dimension())
    assert parsed.reason is None
    assert parsed.value is not None
    assert parsed.value.exact == 4

    mismatch = parse_dimension(_dimension('5"', whole=4))
    assert mismatch.value is None
    assert mismatch.reason == "reader-components-disagree-with-printed-text"


def test_prompt_v5_array_box_and_component_strings_validate_with_request_page_metadata() -> None:
    answer = validate_page_answer(
        {
            "countertops": [
                {
                    "view_title": "",
                    "overall_scope": "run",
                    "overall": {
                        "text": '4"',
                        "position": 0,
                        "whole": "4",
                        "numerator": "",
                        "denominator": "",
                        "stacked": False,
                        "kind": "unknown",
                        "combined": False,
                        "readable": True,
                        "box": [100, 100, 200, 200],
                    },
                    "chain": [],
                }
            ],
            "notes": "",
        },
        page_index=2,
    )
    assert answer.page_index == 2
    assert answer.countertops[0].overall is not None
    assert answer.countertops[0].overall.whole == 4
    assert answer.countertops[0].overall.box == NormalizedBox(x0=100, y0=100, x1=200, y1=200)


def test_model_cannot_override_request_page_metadata() -> None:
    with pytest.raises(ValueError, match="request metadata"):
        validate_page_answer({"page_index": 99, "countertops": []}, page_index=2)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"combined": True}, "combined"),
        ({"text": '4"+2"'}, "combined"),
        ({"stacked": True}, "stacked"),
        ({"readable": False}, "unreadable"),
        ({"text": None}, "unreadable"),
    ],
)
def test_uncertain_and_special_values_stay_with_the_person(kwargs, reason) -> None:
    assert parse_dimension(_dimension(**kwargs)).reason == reason


def test_one_overlapping_drawn_region_is_the_snapped_polygon() -> None:
    version = uuid4()
    drawn = _polygon("0.12", "0.12", "0.18", "0.18", version)
    located = locate_box(
        NormalizedBox(x0=100, y0=100, x1=200, y1=200),
        width_px=1000,
        height_px=1000,
        transform=_transform(),
        document_version_id=version,
        page_index=0,
        regions=(("glyph-run", drawn),),
    )
    assert located.quality == "snapped"
    assert located.snapped_source == "glyph-run"
    assert located.polygon == drawn


def test_no_overlap_keeps_the_model_box_as_approximate_placement_only() -> None:
    version = uuid4()
    far_away = _polygon("0.6", "0.6", "0.7", "0.7", version)
    located = locate_box(
        NormalizedBox(x0=100, y0=100, x1=200, y1=200),
        width_px=1000,
        height_px=1000,
        transform=_transform(),
        document_version_id=version,
        page_index=0,
        regions=(("unrelated-run", far_away),),
    )
    assert located.quality == "approximate"
    assert located.snapped_source is None
    assert located.polygon != far_away


def test_multiple_distinct_overlaps_remain_approximate() -> None:
    version = uuid4()
    first = _polygon("0.12", "0.12", "0.16", "0.16", version)
    second = _polygon("0.16", "0.16", "0.19", "0.19", version)
    located = locate_box(
        NormalizedBox(x0=100, y0=100, x1=200, y1=200),
        width_px=1000,
        height_px=1000,
        transform=_transform(),
        document_version_id=version,
        page_index=0,
        regions=(("first", first), ("second", second)),
    )
    assert located.quality == "approximate"
    assert located.snapped_source is None


def test_cross_page_region_cannot_be_used_as_a_snap_candidate() -> None:
    version = uuid4()
    other_page = _polygon("0.12", "0.12", "0.18", "0.18", version, page=1)
    with pytest.raises(ValueError, match="document version, page"):
        locate_box(
            NormalizedBox(x0=100, y0=100, x1=200, y1=200),
            width_px=1000,
            height_px=1000,
            transform=_transform(),
            document_version_id=version,
            page_index=0,
            regions=(("other-page", other_page),),
        )


def test_exact_printed_text_agreement_corroborates_but_has_no_field_authority() -> None:
    left = _dimension('4"', position=0)
    right = _dimension('4"', position=0)
    result = compare_page_answers(
        _answer(overall=left, scope="run"),
        _answer(overall=right, scope="run"),
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )
    assert len(result) == 1
    assert result[0].state == "corroborated"
    assert result[0].exact_inches == 4
    assert not hasattr(result[0], "field_key")
    assert result[0].qwen_dimension is right


def test_mapping_turns_only_agreed_one_countertop_overall_and_matching_chain_kinds_into_form_proposals() -> (
    None
):
    left_overall = _dimension('40"', whole=40, position=0, kind="unknown")
    right_overall = _dimension('40"', whole=40, position=0, kind="unknown")
    left_chain = (
        _dimension('12"', whole=12, kind="cabinet", position=1),
        _dimension('2"', whole=2, kind="filler", position=2),
    )
    right_chain = tuple(item.model_copy(deep=True) for item in left_chain)
    first = _answer(overall=left_overall, scope="run", chain=left_chain)
    second = _answer(overall=right_overall, scope="run", chain=right_chain)
    comparisons = compare_page_answers(
        first,
        second,
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )

    mapping = map_page_to_fields(comparisons, first_countertop_count=1, second_countertop_count=1)

    assert [(item.field_key, item.position) for item in mapping.proposals] == [
        ("SHOP:countertop_overall_width", 0),
        ("SHOP:cabinet_width", 0),
        ("SHOP:filler_width", 0),
    ]
    assert mapping.questions == ()


def test_mapping_keeps_disagreements_wrong_kinds_and_multi_countertop_pages_review_only() -> None:
    one = _dimension('40"', whole=40, position=0)
    first = _answer(
        overall=one,
        scope="run",
        chain=(_dimension('12"', whole=12, kind="cabinet", position=1),),
    )
    second = _answer(
        overall=_dimension('41"', whole=41, position=0),
        scope="run",
        chain=(_dimension('12"', whole=12, kind="filler", position=1),),
    )
    comparisons = compare_page_answers(
        first,
        second,
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )

    mapping = map_page_to_fields(comparisons, first_countertop_count=1, second_countertop_count=1)

    assert mapping.proposals == ()
    assert len(mapping.questions) == 2
    assert mapping.questions[0].review_reason == "readers differ"
    assert mapping.questions[1].review_reason == "readers differ on the part type"

    multiple = first.model_copy(deep=True)
    multiple.countertops.append(multiple.countertops[0])
    multi_comparisons = compare_page_answers(
        multiple,
        multiple,
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )
    multi_mapping = map_page_to_fields(
        multi_comparisons, first_countertop_count=2, second_countertop_count=2
    )
    assert multi_mapping.proposals == ()
    assert multi_mapping.questions
    assert all("multiple countertops" in item.review_reason for item in multi_mapping.questions)


def test_mapping_keeps_left_to_right_cabinet_slots_when_an_earlier_reading_needs_review() -> None:
    left = _answer(
        overall=_dimension('40"', whole=40, position=0),
        scope="run",
        chain=(
            _dimension('12"', whole=12, kind="cabinet", position=1),
            _dimension('20"', whole=20, kind="cabinet", position=2),
        ),
    )
    right = _answer(
        overall=_dimension('40"', whole=40, position=0),
        scope="run",
        chain=(
            _dimension('13"', whole=13, kind="cabinet", position=1),
            _dimension('20"', whole=20, kind="cabinet", position=2),
        ),
    )
    comparisons = compare_page_answers(
        left,
        right,
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )

    mapping = map_page_to_fields(comparisons, first_countertop_count=1, second_countertop_count=1)

    assert len(mapping.questions) == 1
    assert [(item.field_key, item.position) for item in mapping.proposals] == [
        ("SHOP:countertop_overall_width", 0),
        ("SHOP:cabinet_width", 1),
    ]


def test_location_is_not_required_and_boxes_cannot_change_the_agreed_value() -> None:
    left = _dimension('4"', position=0)
    right = _dimension('4"', position=0).model_copy(
        update={"box": NormalizedBox(x0=700, y0=700, x1=800, y1=800)}
    )
    missing_box = right.model_copy(update={"box": None})
    with_box = compare_page_answers(
        _answer(overall=left, scope="run"),
        _answer(overall=right, scope="run"),
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )[0]
    without_box = compare_page_answers(
        _answer(overall=left, scope="run"),
        _answer(overall=missing_box, scope="run"),
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )[0]
    assert with_box.state == without_box.state == "corroborated"
    assert with_box.value == without_box.value


def test_two_readers_from_one_maker_cannot_corroborate() -> None:
    answer = _answer(overall=_dimension('4"', position=0), scope="run")
    with pytest.raises(ValueError, match="known, distinct makers"):
        compare_page_answers(
            answer,
            answer,
            first_maker="moonshotai.kimi-k3",
            second_maker="us.moonshotai.kimi-k3",
        )


def test_different_exact_values_go_to_person() -> None:
    result = compare_page_answers(
        _answer(overall=_dimension('4"', position=0), scope="run"),
        _answer(overall=_dimension('5"', whole=5, position=0), scope="run"),
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )
    assert result[0].state == "review_required"
    assert result[0].reason == "readers-differ"
    assert result[0].value is None


@pytest.mark.parametrize(
    ("dimension", "reason"),
    [
        (_dimension('4"+2"', whole=4, combined=True), "combined"),
        (_dimension('4 1/2"', whole=4, numerator=1, denominator=2, stacked=True), "stacked"),
    ],
)
def test_combined_or_stacked_agreement_goes_to_person(dimension, reason) -> None:
    result = compare_page_answers(
        _answer(overall=None, scope=None, chain=(dimension,)),
        _answer(overall=None, scope=None, chain=(dimension,)),
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )
    assert result[1].state == "review_required"
    assert result[1].reason == reason


def test_non_run_overall_is_not_corroborated() -> None:
    result = compare_page_answers(
        _answer(overall=_dimension('4"', position=0), scope="wall"),
        _answer(overall=_dimension('4"', position=0), scope="wall"),
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )
    assert result[0].state == "review_required"
    assert result[0].reason == "scope-not-run"


def test_overall_spanning_an_appliance_space_stays_with_the_person() -> None:
    overall = _dimension('39 1/2"', whole=39, numerator=1, denominator=2, position=0)
    chain = (
        _dimension('13 1/8"', whole=13, numerator=1, denominator=8, kind="cabinet"),
        _dimension('24"', whole=24, denominator=1, kind="appliance_space"),
        _dimension('21 3/4"', whole=21, numerator=3, denominator=4, kind="cabinet"),
    )
    result = compare_page_answers(
        _answer(overall=overall, scope="run", chain=chain),
        _answer(overall=overall, scope="run", chain=chain),
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )
    assert result[0].state == "review_required"
    assert result[0].reason == "appliance-span"


def test_topology_mismatch_does_not_shift_chain_members() -> None:
    first = _dimension('4"')
    second = _dimension('5"', whole=5)
    result = compare_page_answers(
        _answer(overall=None, scope=None, chain=(first, second)),
        _answer(overall=None, scope=None, chain=(first,)),
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )
    assert [item.reason for item in result] == [
        "one-reader-missing",
        "reader-topology-differs",
        "one-reader-missing",
    ]
    assert all(item.state == "review_required" for item in result)


def test_wrong_chain_position_goes_to_person_instead_of_shifting_values() -> None:
    left = _dimension('4"')
    right = _dimension('4"')
    right = right.model_copy(update={"position": 2})
    result = compare_page_answers(
        _answer(overall=None, scope=None, chain=(left,)),
        _answer(overall=None, scope=None, chain=(right,)),
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )
    assert result[1].state == "review_required"
    assert result[1].reason == "reader-topology-differs"


def test_wrong_overall_position_goes_to_person() -> None:
    overall = _dimension('4"').model_copy(update={"position": 1})
    result = compare_page_answers(
        _answer(overall=overall, scope="run"),
        _answer(overall=overall, scope="run"),
        first_maker="us.moonshotai.kimi-k3",
        second_maker="qwen.qwen3-vl-235b-a22b",
    )
    assert result[0].state == "review_required"
    assert result[0].reason == "reader-topology-differs"


def test_bedrock_request_uses_structured_output_for_kimi_only() -> None:
    png = b"\x89PNG\r\n\x1a\nfixture"
    kimi = build_converse_request(
        model_id="us.moonshotai.kimi-k3", page_png=png, page_index=3, max_tokens=4096
    )
    qwen = build_converse_request(
        model_id="qwen.qwen3-vl-235b-a22b", page_png=png, page_index=3, max_tokens=4096
    )
    assert kimi["outputConfig"]["textFormat"]["type"] == "json_schema"
    assert "outputConfig" not in qwen
    assert kimi["messages"][0]["content"][0]["image"]["format"] == "png"


class _FakeConverse:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0

    def converse(self, **_kwargs):
        reply = self.replies[self.calls]
        self.calls += 1
        return {
            "output": {"message": {"content": [{"text": reply}]}},
            "usage": {"inputTokens": 100, "outputTokens": 20},
        }


def test_malformed_answer_is_reasked_once_and_both_calls_are_recorded() -> None:
    valid = (
        '{"countertops":[{"view_title":"","overall_scope":"run",'
        '"overall":{"position":0,"text":"4\\"","whole":"4",'
        '"numerator":"","denominator":"","stacked":false,"kind":"unknown",'
        '"combined":false,"readable":true,"box":[100,100,200,200]},'
        '"chain":[]}],"notes":""}'
    )
    client = _FakeConverse(["not-json", valid])
    attempts = []
    answer = read_page(
        client,
        model_id="qwen.qwen3-vl-235b-a22b",
        page_png=b"\x89PNG\r\n\x1a\nfixture",
        page_index=2,
        max_tokens=4096,
        record_attempt=attempts.append,
    )
    assert answer.page_index == 2
    assert client.calls == 2
    assert [attempt.malformed for attempt in attempts] == [True, False]


def test_provider_value_error_is_not_mistaken_for_malformed_output() -> None:
    class ProviderError:
        calls = 0

        def converse(self, **_kwargs):
            self.calls += 1
            raise ValueError("provider rejected request")

    client = ProviderError()
    attempts = []
    with pytest.raises(ValueError, match="provider rejected request"):
        read_page(
            client,
            model_id="qwen.qwen3-vl-235b-a22b",
            page_png=b"\x89PNG\r\n\x1a\nfixture",
            page_index=2,
            max_tokens=4096,
            record_attempt=attempts.append,
        )
    assert client.calls == 1
    assert len(attempts) == 1
    assert attempts[0].failure_kind == "ValueError"
    assert not attempts[0].malformed


def test_parallel_page_results_apply_in_fixed_page_then_reader_order() -> None:
    class SlowClient:
        def converse(self, **kwargs):
            prompt = kwargs["messages"][0]["content"][1]["text"]
            page_index = int(re.search(r"Internal page index: (\d+)", prompt).group(1))
            time.sleep((4 - page_index) * 0.005)
            return {
                "output": {"message": {"content": [{"text": '{"countertops":[],"notes":""}'}]}},
                "usage": {"inputTokens": 8, "outputTokens": 4},
            }

    class Rates:
        def rate_for(self, model_id):
            return (
                object()
                if model_id in {"us.moonshotai.kimi-k3", "qwen.qwen3-vl-235b-a22b"}
                else None
            )

    reader_ids = ("us.moonshotai.kimi-k3", "qwen.qwen3-vl-235b-a22b")
    clients = ThreadLocalConverseClients(SlowClient)
    recorder = ThreadSafeAttemptRecorder()
    result = read_pages_parallel(
        [(2, b"\x89PNG\r\n\x1a\npage"), (1, b"\x89PNG\r\n\x1a\npage")],
        reader_ids=reader_ids,
        clients=clients,
        rates=Rates(),
        calls_per_minute={model: 6000 for model in reader_ids},
        max_concurrent_calls=4,
        max_tokens=4096,
        max_throttle_retries=0,
        retry_backoff_seconds=0.01,
        record_attempt=recorder.record,
    )
    assert [(item.page_index, item.model_id) for item in result] == [
        (1, "us.moonshotai.kimi-k3"),
        (1, "qwen.qwen3-vl-235b-a22b"),
        (2, "us.moonshotai.kimi-k3"),
        (2, "qwen.qwen3-vl-235b-a22b"),
    ]
    assert len(recorder.snapshot()) == 4


def test_unpriced_reader_pair_is_refused_before_a_provider_call() -> None:
    class Rates:
        def rate_for(self, model_id):
            return object() if model_id == "us.moonshotai.kimi-k3" else None

    with pytest.raises(ValueError, match="no stated price.*qwen.qwen3-vl-235b-a22b"):
        require_priced_readers(
            ("us.moonshotai.kimi-k3", "qwen.qwen3-vl-235b-a22b"),
            Rates(),
        )


def test_parallel_runner_checks_prices_before_requesting_a_provider_client() -> None:
    class MissingRates:
        def rate_for(self, _model_id):
            return None

    class NoClient:
        def for_current_thread(self):
            raise AssertionError("unpriced reader must not create/use a client")

    reader_ids = ("us.moonshotai.kimi-k3", "qwen.qwen3-vl-235b-a22b")
    with pytest.raises(ValueError, match="no stated price"):
        read_pages_parallel(
            ((0, b"\x89PNG\r\n\x1a\npage"),),
            reader_ids=reader_ids,
            clients=NoClient(),
            rates=MissingRates(),
            calls_per_minute={model: 60 for model in reader_ids},
            max_concurrent_calls=2,
            max_tokens=1024,
            max_throttle_retries=0,
            retry_backoff_seconds=0.01,
            record_attempt=lambda _attempt: None,
        )


def test_approximate_qwen_location_does_not_change_agreement() -> None:
    class Client:
        def converse(self, **_kwargs):
            return {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": (
                                    '{"countertops":[{"view_title":"","overall_scope":"run",'
                                    '"overall":{"position":0,"text":"4\\"","whole":"4",'
                                    '"numerator":"","denominator":"1","stacked":false,'
                                    '"kind":"unknown","combined":false,"readable":true,'
                                    '"box":[100,100,200,200]},"chain":[]}],"notes":""}'
                                )
                            }
                        ]
                    }
                },
                "usage": {"inputTokens": 20, "outputTokens": 10},
            }

    class Rates:
        def rate_for(self, _model_id):
            return object()

    page = FormPageImage(
        page_index=2,
        png=b"\x89PNG\r\n\x1a\nfixture",
        width_px=1000,
        height_px=1000,
        document_version_id=uuid4(),
        page_id=uuid4(),
        transform=_transform(),
        regions=(),
        gv_mark_checker=lambda _polygon: False,
    )
    recorder = ThreadSafeAttemptRecorder()
    located = read_form_pages(
        (page,),
        reader_ids=("us.moonshotai.kimi-k3", "qwen.qwen3-vl-235b-a22b"),
        clients=ThreadLocalConverseClients(Client),
        rates=Rates(),
        calls_per_minute={
            "us.moonshotai.kimi-k3": 6000,
            "qwen.qwen3-vl-235b-a22b": 6000,
        },
        max_concurrent_calls=2,
        max_tokens=1024,
        max_throttle_retries=0,
        retry_backoff_seconds=0.01,
        record_attempt=recorder.record,
    )
    assert len(located) == 1
    assert located[0].comparison.state == "corroborated"
    assert located[0].location is not None
    assert located[0].location.quality == "approximate"


@pytest.mark.parametrize(
    ("box", "mark_result", "expected_reason"),
    [
        ([100, 100, 200, 200], True, "gv-mark"),
        ([100, 100, 200, 200], None, "gv-mark-unchecked"),
        (None, False, "label-location-unknown"),
    ],
)
def test_form_reader_never_maps_agreement_without_a_clean_located_label(
    box, mark_result, expected_reason
) -> None:
    class Client:
        def converse(self, **_kwargs):
            box_json = "null" if box is None else json.dumps(box)
            return {
                "output": {
                    "message": {
                        "content": [
                            {
                                "text": (
                                    '{"countertops":[{"view_title":"","overall_scope":"run",'
                                    '"overall":{"position":0,"text":"4\\"","whole":"4",'
                                    '"numerator":"","denominator":"1","stacked":false,'
                                    '"kind":"unknown","combined":false,"readable":true,'
                                    f'"box":{box_json}'
                                    '},"chain":[]}],"notes":""}'
                                )
                            }
                        ]
                    }
                },
                "usage": {"inputTokens": 20, "outputTokens": 10},
            }

    class Rates:
        def rate_for(self, _model_id):
            return object()

    page = FormPageImage(
        page_index=2,
        png=b"\x89PNG\r\n\x1a\nfixture",
        width_px=1000,
        height_px=1000,
        document_version_id=uuid4(),
        page_id=uuid4(),
        transform=_transform(),
        regions=(),
        gv_mark_checker=lambda _polygon: mark_result,
    )
    located = read_form_pages(
        (page,),
        reader_ids=("us.moonshotai.kimi-k3", "qwen.qwen3-vl-235b-a22b"),
        clients=ThreadLocalConverseClients(Client),
        rates=Rates(),
        calls_per_minute={
            "us.moonshotai.kimi-k3": 6000,
            "qwen.qwen3-vl-235b-a22b": 6000,
        },
        max_concurrent_calls=2,
        max_tokens=1024,
        max_throttle_retries=0,
        retry_backoff_seconds=0.01,
        record_attempt=lambda _attempt: None,
    )
    assert len(located) == 1
    assert located[0].comparison.state == "review_required"
    assert located[0].comparison.value is None
    assert located[0].comparison.reason == expected_reason
    mapping = map_page_to_fields(
        (located[0].comparison,), first_countertop_count=1, second_countertop_count=1
    )
    assert mapping.proposals == ()
    assert len(mapping.questions) == 1
    if expected_reason == "gv-mark":
        assert (
            mapping.questions[0].review_reason
            == "GV's markup is on this label — check the vendor's own number"
        )
