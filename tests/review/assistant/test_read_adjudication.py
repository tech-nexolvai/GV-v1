"""The third read of a disputed finding (#1498): when it is made, the crops, the neutral question
and code's majority of three readings. $0: the model and the renderer are fakes (one test draws a
real tiny PDF)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.review.assistant import read_adjudication
from app.review.assistant.finding_checks import trust_level
from app.review.assistant.read_adjudication import (
    ADJUDICATION_DPI,
    ADJUDICATION_MAX_PX,
    ADJUDICATION_MAX_TILES,
    Reading,
    SideCrops,
    adjudicate,
    crop_dpi,
    decide,
    is_tall,
    needs_adjudication,
    render_crops,
    third_read,
)
from tests.review.assistant.test_chat_findings import make_pdf

BOX = {"page": 1, "x0": 100, "y0": 80, "x1": 160, "y1": 96}
TALL_BOX = {"page": 1, "x0": 100, "y0": 80, "x1": 112, "y1": 160}


def _png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


def _size(png: bytes) -> tuple[int, int]:
    size: tuple[int, int] = Image.open(io.BytesIO(png)).size
    return size


def _finding(second: str | None = "disagrees", **changes: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": "F1",
        "item": "Sink base width",
        "architect": "3'-1\"",
        "vendor": '46"',
        "status": "different",
        "arch_box": dict(BOX),
        "vendor_box": dict(BOX, page=2),
        "checks": {"second_read": second},
        "second_read_values": {"architect": '37"', "vendor": '45"'},
    }
    finding.update(changes)
    return finding


def _read(architect: str | None, vendor: str | None, sure: bool = True) -> Reading:
    return Reading(model="sonnet", architect=architect, vendor=vendor, sure=sure, role="third")


def _second(architect: str | None, vendor: str | None, sure: bool = True) -> Reading:
    return Reading(model="haiku", architect=architect, vendor=vendor, sure=sure, role="second")


# ---- when ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("second", "expected"),
    [("disagrees", True), ("unsure", True), ("agrees", False), ("not_run", False), (None, False)],
)
def test_only_a_disagreeing_or_unsure_second_read_is_escalated(
    second: str | None, expected: bool
) -> None:
    assert needs_adjudication(_finding(second)) is expected


def test_a_finding_without_a_box_is_not_escalated() -> None:
    assert not needs_adjudication(_finding("disagrees", arch_box=None, vendor_box=None))


# ---- the majority -----------------------------------------------------------------------------------


def test_the_third_read_siding_with_the_finding_makes_two_of_three() -> None:
    finding = _finding()
    second = _second('37"', '45"')
    assert decide(finding, second, _read("37", '46"'), ["vendor"]) == "agrees_after_adjudication"
    # Inches by code: every printed form of the same number counts.
    assert decide(finding, second, _read("3'-1\"", "3'-10\""), ["architect", "vendor"]) == (
        "agrees_after_adjudication"
    )


def test_both_reads_on_the_same_other_value_outvote_the_finding() -> None:
    assert decide(_finding(), _second('37"', '45"'), _read('37"', "45"), ["vendor"]) == "disagrees"


def test_three_different_readings_are_a_tie() -> None:
    assert decide(_finding(), _second('37"', '45"'), _read('37"', '47"'), ["vendor"]) == "unsure"


def test_a_third_read_that_is_not_sure_does_not_vote_so_one_against_one_is_a_tie() -> None:
    third = _read('37"', '46"', sure=False)
    assert decide(_finding(), _second('37"', '45"'), third, ["vendor"]) == "unsure"
    assert decide(_finding(), _second('37"', '45"'), _read(None, None), ["vendor"]) == "unsure"


def test_an_unsure_second_read_abstains() -> None:
    finding = _finding("unsure")
    second = _second('37"', '45"', sure=False)
    assert decide(finding, second, _read(None, '46"'), ["vendor"]) == "agrees_after_adjudication"
    assert decide(finding, second, _read(None, '45"'), ["vendor"]) == "unsure"
    assert decide(finding, None, _read(None, '46"'), ["vendor"]) == "agrees_after_adjudication"


def test_every_side_must_agree_and_any_side_against_wins() -> None:
    second = _second('37"', '45"')
    both = ["architect", "vendor"]
    assert decide(_finding(), second, _read('37"', '45"'), both) == "disagrees"
    assert decide(_finding(), second, _read('37"', '47"'), both) == "unsure"
    assert decide(_finding(), second, _read('37"', None), both) == "unsure"


def test_a_value_that_is_not_a_number_or_no_side_shown_is_unsure() -> None:
    finding = _finding(vendor="see detail")
    assert decide(finding, _second(None, "x"), _read(None, "see detail"), ["vendor"]) == "unsure"
    assert decide(_finding(), _second('37"', '46"'), _read('37"', '46"'), []) == "unsure"


# ---- the crops --------------------------------------------------------------------------------------


def test_the_dpi_is_300_for_a_small_box_and_lower_for_a_big_one() -> None:
    assert crop_dpi(108, 64) == ADJUDICATION_DPI
    dpi = crop_dpi(1200, 800)
    assert dpi < ADJUDICATION_DPI
    width, height = -(-1200 * dpi // 72), -(-800 * dpi // 72)
    assert max(width, height) <= ADJUDICATION_MAX_PX
    assert -(-width // 28) * -(-height // 28) <= ADJUDICATION_MAX_TILES


def test_a_tall_box_gets_a_turned_copy_and_a_wide_one_does_not(tmp_path: Path) -> None:
    pdf = tmp_path / "set.pdf"
    pdf.write_bytes(b"%PDF fake")
    asked: list[tuple[int, tuple[int, int, int, int], int]] = []

    def render(data: bytes, page: int, box_px: tuple[int, int, int, int], dpi: int) -> bytes:
        asked.append((page, box_px, dpi))
        return _png(box_px[2] - box_px[0], box_px[3] - box_px[1])

    def size(data: bytes, page: int) -> tuple[float, float]:
        return (612.0, 792.0)

    assert is_tall(TALL_BOX) and not is_tall(BOX)
    tall = render_crops(pdf, TALL_BOX, render=render, page_size=size)
    assert tall is not None and tall.turned is not None and tall.dpi == ADJUDICATION_DPI
    width, height = _size(tall.upright)
    assert height > width and _size(tall.turned) == (height, width)
    # Padded by 24 points, at 300 dpi, on page index 0.
    assert asked[0] == (0, (316, 233, 566, 766), 300)
    wide = render_crops(pdf, BOX, render=render, page_size=size)
    assert wide is not None and wide.turned is None
    off_page = render_crops(pdf, dict(BOX, x0=700, x1=760), render=render, page_size=size)
    assert off_page is None


def test_the_real_renderer_draws_the_spot_sharper_than_the_evidence_crop(tmp_path: Path) -> None:
    pdf = tmp_path / "set.pdf"
    pdf.write_bytes(make_pdf(3))
    crops = render_crops(pdf, BOX)
    assert crops is not None and crops.dpi == ADJUDICATION_DPI
    width, height = _size(crops.upright)
    # (60 + 48) x (16 + 48) points at 300 dpi; the evidence crop is drawn at 170 dpi.
    assert (width, height) == (450, 267)
    assert render_crops(pdf, dict(BOX, page=9)) is None


# ---- the third read ------------------------------------------------------------------------------------


class FakeModel:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.sent: list[list[dict[str, Any]]] = []

    def __call__(self, content: list[dict[str, Any]]) -> str:
        self.sent.append(content)
        return self.reply


def test_the_question_is_neutral_and_shows_the_turned_copy() -> None:
    model = FakeModel(json.dumps({"architect": None, "vendor": '46"', "sure": True}))
    crops = {
        "architect": SideCrops(upright=_png(10, 4)),
        "vendor": SideCrops(upright=_png(4, 10), turned=_png(10, 4)),
    }
    read = third_read(crops, "Sink base width", model)
    assert read.vendor == '46"' and read.sure and read.asked == ("architect", "vendor")
    content = model.sent[0]
    assert [block["type"] for block in content] == ["image", "image", "image", "text"]
    words = content[-1]["text"]
    assert "image 3 is the same crop of the vendor's shop drawing turned a quarter turn" in words
    assert "Sink base width" in words and "46" not in words and "3'-1" not in words


def test_a_reply_that_is_not_the_json_is_a_read_of_nothing() -> None:
    read = third_read({"vendor": SideCrops(upright=_png(4, 4))}, "Top", FakeModel("It is 46."))
    assert read.vendor is None and read.sure is False


def test_adjudicate_records_all_three_readings() -> None:
    model = FakeModel(json.dumps({"architect": '37"', "vendor": "3'-10\"", "sure": True}))
    crops = {
        "architect": SideCrops(upright=_png(4, 4)),
        "vendor": SideCrops(upright=_png(4, 10), turned=_png(10, 4)),
    }
    result = adjudicate(_finding(), crops, model, model="sonnet", second_model="haiku")
    assert result.outcome == "agrees_after_adjudication"
    assert result.reads == [
        {
            "role": "first",
            "model": "lead",
            "value_arch": "3'-1\"",
            "value_vendor": '46"',
            "sure": None,
        },
        {
            "role": "second",
            "model": "haiku",
            "value_arch": '37"',
            "value_vendor": '45"',
            "sure": True,
        },
        {
            "role": "third",
            "model": "sonnet",
            "value_arch": '37"',
            "value_vendor": "3'-10\"",
            "sure": True,
            "dpi": 300,
            "turned": True,
        },
    ]


def test_the_model_is_sonnet_unless_the_deployment_names_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(read_adjudication.ADJUDICATION_MODEL_VARIABLE, raising=False)
    assert read_adjudication.adjudication_model() == "sonnet"
    monkeypatch.setenv(read_adjudication.ADJUDICATION_MODEL_VARIABLE, "Opus")
    assert read_adjudication.adjudication_model() == "opus"
    monkeypatch.setenv(read_adjudication.ADJUDICATION_MODEL_VARIABLE, "gpt")
    assert read_adjudication.adjudication_model() == "sonnet"


# ---- trust -----------------------------------------------------------------------------------------------


def test_the_majority_stands_in_for_the_second_read_in_the_trust_level() -> None:
    printed = {"printed_architect": "match", "printed_vendor": "match"}
    disputed = {**printed, "second_read": "disagrees"}
    assert trust_level(disputed) == "to_check"
    assert trust_level({**disputed, "adjudication": "agrees_after_adjudication"}) == "confirmed"
    assert trust_level({**disputed, "adjudication": "disagrees"}) == "to_check"
    # A tie leaves the second read's disagreement standing.
    assert trust_level({**disputed, "adjudication": "unsure"}) == "to_check"
    unsure = {"second_read": "unsure", "lead_checked": True}
    assert trust_level({**unsure, "adjudication": "agrees_after_adjudication"}) == "likely"


def test_the_critic_objecting_is_to_check_and_passing_is_never_confirmed_alone() -> None:
    confirmed = {"printed_architect": "match", "printed_vendor": "match", "second_read": "agrees"}
    assert trust_level({**confirmed, "critic": "objects"}) == "to_check"
    assert trust_level({"second_read": "agrees", "critic": "passed"}) == "likely"
    assert trust_level({"critic": "passed"}) == "to_check"
    assert trust_level({"second_read": "unsure", "critic": "unsure", "lead_checked": True}) == (
        "to_check"
    )
