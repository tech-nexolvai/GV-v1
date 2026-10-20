"""The second independent read of a chat finding (chat v3): the neutral request, the strict reply
parsing and code's comparison. $0: the model call is a fake."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from app.review.assistant.second_read import (
    SecondRead,
    compare_read,
    parse_reply,
    question,
    second_read,
)

ARCH_PNG = b"\x89PNG architect crop"
VENDOR_PNG = b"\x89PNG vendor crop"


class FakeModel:
    """Records what it was sent and replies with a fixed text."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.sent: list[list[dict[str, Any]]] = []

    def __call__(self, content: list[dict[str, Any]]) -> str:
        self.sent.append(content)
        return self.reply


def _reply(architect: str | None, vendor: str | None, sure: bool = True) -> str:
    return json.dumps({"architect": architect, "vendor": vendor, "sure": sure})


FINDING = {"item": "Sink base width", "architect": "3'-1\"", "vendor": '46"', "status": "different"}


def test_the_request_holds_both_crops_and_a_neutral_question() -> None:
    model = FakeModel(_reply('37"', '46"'))
    read = second_read({"architect": ARCH_PNG, "vendor": VENDOR_PNG}, "Sink base width", model)
    [content] = model.sent
    assert [block["type"] for block in content] == ["image", "image", "text"]
    assert base64.b64decode(content[0]["source"]["data"]) == ARCH_PNG
    assert base64.b64decode(content[1]["source"]["data"]) == VENDOR_PNG
    assert content[0]["source"]["media_type"] == "image/png"
    text = content[2]["text"]
    assert '<drawing_data source="item">\nSink base width\n</drawing_data>' in text
    assert "image 1 is the architect's drawing" in text
    assert "image 2 is the vendor's shop drawing" in text
    assert "whether you are sure" in text
    assert read == SecondRead(
        architect='37"', vendor='46"', result="unsure", sure=True, asked=("architect", "vendor")
    )


def test_the_question_never_shows_the_first_answer() -> None:
    model = FakeModel(_reply(None, None))
    second_read({"architect": ARCH_PNG, "vendor": VENDOR_PNG}, "Sink base width", model)
    sent = model.sent[0][-1]["text"]
    assert "3'-1" not in sent and "46" not in sent and "different" not in sent


def test_one_crop_only_asks_about_that_drawing() -> None:
    model = FakeModel(_reply(None, '46"'))
    read = second_read({"vendor": VENDOR_PNG}, "Sink base width", model)
    [content] = model.sent
    assert [block["type"] for block in content] == ["image", "text"]
    assert "image 1 is the vendor's shop drawing" in content[1]["text"]
    assert read.asked == ("vendor",)


def test_no_crops_makes_no_call() -> None:
    model = FakeModel(_reply('37"', '46"'))
    read = second_read({"architect": b""}, "Sink base width", model)
    assert model.sent == []
    assert read.result == "unsure" and read.asked == ()


def test_a_long_item_name_is_cut_and_spaces_folded() -> None:
    text = question("Sink   base\nwidth " + "x" * 300, ("architect",))
    assert "Sink base width" in text and "x" * 121 not in text
    assert "\nthis item\n" in question("   ", ("architect",))


def test_a_failing_call_raises_to_the_engine() -> None:
    def broken(content: list[dict[str, Any]]) -> str:
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        second_read({"architect": ARCH_PNG}, "Sink base width", broken)


# ---- parsing -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "architect", "vendor", "sure"),
    [
        (_reply('37"', '46"'), '37"', '46"', True),
        ("```json\n" + _reply("3'-1\"", None, False) + "\n```", "3'-1\"", None, False),
        ("```\n" + _reply('37"', '46"') + "\n```", '37"', '46"', True),
        (_reply("  ", '46"'), None, '46"', True),
    ],
)
def test_replies_are_parsed(
    reply: str, architect: str | None, vendor: str | None, sure: bool
) -> None:
    read = parse_reply(reply)
    assert (read.architect, read.vendor, read.sure) == (architect, vendor, sure)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "The architect shows 37 inches.",
        "[1, 2]",
        json.dumps({"architect": '37"', "vendor": '46"'}),
        json.dumps({"architect": '37"', "vendor": '46"', "sure": "yes"}),
        json.dumps({"architect": 37, "vendor": '46"', "sure": True}),
        json.dumps({"architect": '37"', "vendor": '46"', "sure": True, "note": "x"}),
        'Here: {"architect": "37", "vendor": "46", "sure": true}',
    ],
)
def test_anything_but_the_asked_for_json_is_a_read_of_nothing(reply: str) -> None:
    read = parse_reply(reply)
    assert (read.architect, read.vendor, read.sure, read.result) == (None, None, False, "unsure")


def test_a_reply_that_is_not_text_is_nothing() -> None:
    assert parse_reply(None) == SecondRead(architect=None, vendor=None)  # type: ignore[arg-type]


# ---- comparison ----------------------------------------------------------------------------------


def _read(architect: str | None, vendor: str | None, sure: bool = True) -> SecondRead:
    return SecondRead(architect=architect, vendor=vendor, sure=sure, asked=("architect", "vendor"))


@pytest.mark.parametrize(
    ("read", "result"),
    [
        (_read('37"', '46"'), "agrees"),
        (_read("3'-1\"", "940 [46]"), "agrees"),
        (_read("37 in", "3' - 10\""), "agrees"),
        (_read('38"', '46"'), "disagrees"),
        (_read('37"', '45 1/2"'), "disagrees"),
        (_read('38"', None), "disagrees"),
        (_read('37"', None), "unsure"),
        (_read('37"', "illegible"), "unsure"),
        (_read('37"', '46"', sure=False), "unsure"),
        (_read('38"', '45"', sure=False), "unsure"),
        (_read(None, None), "unsure"),
        (SecondRead(architect='37"', vendor='99"', sure=True, asked=("architect",)), "agrees"),
        (SecondRead(architect='37"', vendor='46"', sure=True, asked=()), "unsure"),
    ],
)
def test_code_compares_the_read_in_inches(read: SecondRead, result: str) -> None:
    compared = compare_read(read, FINDING)
    assert compared.result == result
    assert (compared.architect, compared.vendor) == (read.architect, read.vendor)


def test_a_finding_value_that_is_not_a_number_cannot_agree() -> None:
    finding = {**FINDING, "architect": "not shown"}
    assert compare_read(_read('37"', '46"'), finding).result == "unsure"


def test_end_to_end_with_a_fake_model() -> None:
    model = FakeModel("```json\n" + _reply("3' - 1\"", "46") + "\n```")
    read = second_read({"architect": ARCH_PNG, "vendor": VENDOR_PNG}, FINDING["item"], model)
    assert compare_read(read, FINDING).result == "agrees"
