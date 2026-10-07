"""The readers are told the drawing set's product in one plain line (#994).

Verification for `extraction/product_context.py` and its use by the form reader and the slot (crop)
reader. Three properties:

- with a product, every request carries exactly ``This drawing set was submitted for: <product>.``;
- the system text (and so the private guidance file) is unchanged, and so is the crop question;
- the prompt id the attempt is recorded under names the product, and with no product the request
  and the id are exactly what they were before #994.

No network, no credentials, no client values.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from extraction.form_reader.bedrock import (
    AttemptUsage,
    MalformedFormAnswer,
    build_converse_request,
    read_page,
)
from extraction.form_reader.prompt_v5 import (
    BUILT_IN_PROMPT,
    PROMPT_ID,
    page_prompt,
    prompt_from_guidance_file,
)
from extraction.product_context import product_context_line, with_product
from extraction.slot_reader.bedrock import (
    CROP_PROMPT,
    CROP_PROMPT_ID,
    CropJob,
    build_crop_request,
    crop_prompt_id,
    read_crops_parallel,
)
from extraction.slot_reader.walls import WALL_PROMPT, WALL_PROMPT_ID
from tests.workflow.test_slot_reader import (
    TEXTS,
    FakeReaders,
    crops_to_texts,
    named_sheet,
    runtime,
    slot_page,
)
from vocabulary.semantic_types import ProductType
from workflow.slot_reader import read_slot_pages

PAGE_PNG = b"\x89PNG\r\n\x1a\nfixture"
LINE = "This drawing set was submitted for: countertop."
QWEN = "qwen.qwen3-vl-235b-a22b"
KIMI = "us.moonshotai.kimi-k3"


def _texts(request: Mapping[str, Any]) -> list[str]:
    return [item["text"] for item in request["messages"][0]["content"] if "text" in item]


# ---------------------------------------------------------------------------
# The line and the identity
# ---------------------------------------------------------------------------


def test_the_line_is_one_plain_sentence_naming_the_vocabulary_value() -> None:
    assert product_context_line(ProductType.COUNTERTOP) == LINE
    assert product_context_line(ProductType.CABINET) == (
        "This drawing set was submitted for: cabinet."
    )


def test_the_prompt_id_names_the_product_and_is_unchanged_without_one() -> None:
    assert with_product("form-reader-v5", None) == "form-reader-v5"
    assert with_product("form-reader-v5", ProductType.COUNTERTOP) == (
        "form-reader-v5+product=countertop"
    )
    with pytest.raises(ValueError, match="already names a product"):
        with_product("form-reader-v5+product=countertop", ProductType.CABINET)


# ---------------------------------------------------------------------------
# The form reader
# ---------------------------------------------------------------------------


def test_a_form_request_without_a_product_is_exactly_as_before() -> None:
    request = build_converse_request(
        model_id=QWEN, page_png=PAGE_PNG, page_index=2, max_tokens=100, prompt=BUILT_IN_PROMPT
    )

    assert BUILT_IN_PROMPT.for_product(None) is BUILT_IN_PROMPT
    assert _texts(request) == [
        "Read the attached page image. Internal page index: 2. Return JSON only."
    ]
    assert page_prompt(2) == _texts(request)[0]
    assert BUILT_IN_PROMPT.prompt_id == PROMPT_ID


def test_a_form_request_carries_the_product_line_and_keeps_the_system_text() -> None:
    prompt = BUILT_IN_PROMPT.for_product(ProductType.COUNTERTOP)

    request = build_converse_request(
        model_id=QWEN, page_png=PAGE_PNG, page_index=2, max_tokens=100, prompt=prompt
    )

    assert _texts(request) == [
        f"{LINE}\nRead the attached page image. Internal page index: 2. Return JSON only."
    ]
    assert request["system"] == [{"text": BUILT_IN_PROMPT.system_text}]
    assert request["messages"][0]["content"][0]["image"]["format"] == "png"
    assert prompt.prompt_id == "form-reader-v5+product=countertop"
    assert prompt.template_id == BUILT_IN_PROMPT.template_id
    with pytest.raises(ValueError, match="already names a product"):
        prompt.for_product(ProductType.CABINET)


def test_the_private_guidance_is_sent_unchanged_and_its_id_names_the_product(
    tmp_path: Path,
) -> None:
    """The measured guidance lives outside the repository; telling the product must not touch it."""
    guidance = tmp_path / "guidance.txt"
    guidance.write_text("Invented guidance for a test.", encoding="utf-8")
    private = prompt_from_guidance_file(guidance)

    told = private.for_product(ProductType.COUNTERTOP)

    assert told.system_text == private.system_text
    assert told.prompt_id == f"{private.prompt_id}+product=countertop"
    assert guidance.read_text(encoding="utf-8") == "Invented guidance for a test."


class _Malformed:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        self.requests.append(kwargs)
        return {"output": {"message": {"content": [{"text": "no json here"}]}}}


def test_every_form_attempt_is_recorded_under_the_product_prompt_id() -> None:
    client = _Malformed()
    attempts: list[AttemptUsage] = []

    with pytest.raises(MalformedFormAnswer):
        read_page(
            client,
            model_id=QWEN,
            page_png=PAGE_PNG,
            page_index=0,
            max_tokens=100,
            record_attempt=attempts.append,
            prompt=BUILT_IN_PROMPT.for_product(ProductType.COUNTERTOP),
        )

    assert [attempt.prompt_id for attempt in attempts] == ["form-reader-v5+product=countertop"] * 2
    assert all(_texts(request)[0].startswith(LINE) for request in client.requests)


# ---------------------------------------------------------------------------
# The slot (crop) reader
# ---------------------------------------------------------------------------


def test_a_crop_request_without_a_product_is_exactly_as_before() -> None:
    request = build_crop_request(model_id=QWEN, crop_png=PAGE_PNG, max_tokens=100)

    assert _texts(request) == [CROP_PROMPT]
    assert crop_prompt_id() == CROP_PROMPT_ID == "slot-crop-v1"


def test_a_crop_request_carries_the_product_line_before_the_unchanged_question() -> None:
    request = build_crop_request(
        model_id=KIMI, crop_png=PAGE_PNG, max_tokens=100, product=ProductType.COUNTERTOP
    )

    content = request["messages"][0]["content"]
    assert "image" in content[0]
    assert _texts(request) == [LINE, CROP_PROMPT]
    assert crop_prompt_id(ProductType.COUNTERTOP) == "slot-crop-v1+product=countertop"


class _Readers:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def for_current_thread(self) -> _Readers:
        return self

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        self.requests.append(kwargs)
        payload = {
            "text": '12"',
            "stacked": False,
            "combined": False,
            "readable": True,
            "no_dimension": False,
        }
        return {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }


class _Rates:
    def rate_for(self, model_id: str) -> object | None:
        return object()


def test_parallel_crop_reads_carry_the_product_and_record_it() -> None:
    readers = _Readers()
    attempts: list[AttemptUsage] = []

    read_crops_parallel(
        [CropJob("a", QWEN, 0, PAGE_PNG), CropJob("a", KIMI, 0, PAGE_PNG)],
        clients=readers,
        rates=_Rates(),
        calls_per_minute={QWEN: 6000, KIMI: 6000},
        max_concurrent_calls=2,
        max_tokens=100,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        record_attempt=attempts.append,
        product=ProductType.COUNTERTOP,
    )

    assert all(_texts(request) == [LINE, CROP_PROMPT] for request in readers.requests)
    assert {attempt.prompt_id for attempt in attempts} == {"slot-crop-v1+product=countertop"}


class _RecordingFakeReaders(FakeReaders):
    """The slot-reader test's perfect reader, also keeping every request it was sent."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sent: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        self.sent.append(kwargs)
        return super().converse(**kwargs)


def test_the_slot_reader_runtime_tells_every_reader_and_records_the_product() -> None:
    """End to end through `read_slot_pages`: the readings are the same, the requests carry the
    line, and the run's identity and prompt id name the product."""
    page = slot_page(named_sheet())
    lookup = crops_to_texts(page, TEXTS)
    plain_readers = _RecordingFakeReaders(lambda _model, png: lookup[png])
    told_readers = _RecordingFakeReaders(lambda _model, png: lookup[png])
    plain = runtime(plain_readers)
    told = dataclasses.replace(runtime(told_readers), product=ProductType.COUNTERTOP)
    attempts: list[AttemptUsage] = []

    (without,) = read_slot_pages([page], runtime=plain, record_attempt=lambda _a: None)
    (with_line,) = read_slot_pages([page], runtime=told, record_attempt=attempts.append)

    # Label crops carry the product line; the wall question (#992) is a separately measured prompt
    # and is sent unchanged, so its identity stays its own.
    told_crops = [r for r in told_readers.sent if WALL_PROMPT not in _texts(r)]
    told_walls = [r for r in told_readers.sent if WALL_PROMPT in _texts(r)]
    plain_crops = [r for r in plain_readers.sent if WALL_PROMPT not in _texts(r)]
    plain_walls = [r for r in plain_readers.sent if WALL_PROMPT in _texts(r)]
    assert told_crops and all(_texts(request) == [LINE, CROP_PROMPT] for request in told_crops)
    assert all(_texts(request) == [CROP_PROMPT] for request in plain_crops)
    assert [_texts(r) for r in told_walls] == [_texts(r) for r in plain_walls]
    assert all(LINE not in _texts(request) for request in told_walls)
    assert with_line.mapping.proposals == without.mapping.proposals
    assert {attempt.prompt_id for attempt in attempts} <= {
        "slot-crop-v1+product=countertop",
        WALL_PROMPT_ID,
    }
    assert "slot-crop-v1+product=countertop" in {attempt.prompt_id for attempt in attempts}
    assert told.prompt_id == "slot-crop-v1+product=countertop"
    assert f"prompt=slot-crop-v1+product=countertop+{WALL_PROMPT_ID};" in told.config_hash
    assert plain.prompt_id == "slot-crop-v1"
    assert f"prompt=slot-crop-v1+{WALL_PROMPT_ID};" in plain.config_hash
