"""The AI page review's evaluator: a second, independent look at a "different" row (decision 2026-10-16).

Verification for `extraction/ai_review/evaluator.py`: code keeps a difference only when the
evaluator says the same span and reads the same vendor inches; anything else is `unsure` with the
evaluator's reason; the question never carries the first AI's number, reasoning or verdict; a
different model is asked; stored answers are reused; the budget is respected; the close-ups are
cut around both spots. No paid call: the client is a stub. Synthetic numbers only.
"""

from __future__ import annotations

import io
import json
from fractions import Fraction
from typing import Any

import pytest
from PIL import Image

from extraction.ai_review.evaluator import (
    EVALUATOR_MODEL,
    ask_evaluator,
    decide_recheck,
    evaluator_pictures,
    evaluator_question,
    evaluator_schema,
)
from extraction.ai_review.reader import AI_REVIEW_MODEL
from extraction.slot_reader.anthropic import SpendCapExceeded
from extraction.slot_reader.claude_output import _conforms


def _png(size: tuple[int, int] = (120, 90)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(out, format="PNG")
    return out.getvalue()


def _question() -> Any:
    return evaluator_question(
        3, _png(), _png(), architect_text="2'-9\"", measures="floor to counter top"
    )


def _reply(answer: object, stop: str = "end_turn") -> dict[str, Any]:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return {
        "stopReason": stop,
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": 3000, "outputTokens": 400},
    }


class _Client:
    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return self.replies.pop(0)


YES = {
    "architect_from": "floor",
    "architect_to": "counter top",
    "vendor_from": "floor",
    "vendor_to": "counter top",
    "vendor_chain": "",
    "same_thing": "yes",
    "vendor_value": "864 [34]",
    "reason": "both floor to counter top",
}


def _decide(answer: Any, failure: str | None = None) -> Any:
    return decide_recheck(
        architect_inches=Fraction(33),
        first_vendor_inches=Fraction(34),
        first_why="Different number.",
        answer=answer,
        failure=failure,
    )


# --- code decides ---------------------------------------------------------------------------------


def test_a_confirmed_difference_stays_different_and_is_marked_verified() -> None:
    out = _decide(YES)
    assert out.result == "different" and out.verified_by == "evaluator"
    assert out.evaluator_reason == "both floor to counter top"
    assert "Checked again by the evaluator" in out.why


def test_a_different_end_point_demotes_the_row_with_the_evaluators_reason() -> None:
    out = _decide(
        {
            **YES,
            "same_thing": "no",
            "vendor_value": "965 [38]",
            "reason": "vendor runs to splash top",
        }
    )
    assert out.result == "unsure" and out.verified_by is None
    assert out.why == (
        "The evaluator says the vendor line runs to a different point: vendor runs to splash top"
    )


@pytest.mark.parametrize(
    ("answer", "says"),
    [
        ({**YES, "same_thing": "unsure"}, "cannot tell whether both measure the same span"),
        ({**YES, "vendor_value": "889 [35]"}, 'as 35", not 34"'),
        ({**YES, "vendor_value": "838 [33]"}, "not 34"),
        ({**YES, "vendor_value": "864"}, "does not state exact inches"),
    ],
)
def test_anything_short_of_a_full_confirmation_is_unsure(answer: Any, says: str) -> None:
    out = _decide(answer)
    assert out.result == "unsure" and out.verified_by is None and says in out.why


def test_a_vendor_reading_equal_to_the_architects_is_not_kept_as_different() -> None:
    out = decide_recheck(
        architect_inches=Fraction(34),
        first_vendor_inches=Fraction(34),
        first_why="x",
        answer=YES,
        failure=None,
    )
    assert out.result == "unsure"


def test_no_evaluator_answer_never_leaves_a_difference_clouded() -> None:
    out = _decide(None, "the package's AI budget was reached")
    assert out.result == "unsure" and out.verified_by is None and out.answer is None
    assert "could not check this difference (the package's AI budget was reached)" in out.why


# --- the question --------------------------------------------------------------------------------


def test_the_claim_is_neutral_and_the_evaluator_is_a_different_model() -> None:
    question = _question()
    assert question.prompt.endswith(
        "Architect: 2'-9\" measuring floor to counter top. Vendor label in the box: ?"
    )
    assert "sonnet" in EVALUATOR_MODEL and "opus" in AI_REVIEW_MODEL
    client = _Client(_reply(YES))
    read = ask_evaluator(
        question, client=client, document_version_id="v", spend_guard=None, stored=None
    )
    assert read.answer == YES and read.reason is None
    (request,) = client.requests
    assert request["modelId"] == EVALUATOR_MODEL
    content = request["messages"][0]["content"]
    assert len([c for c in content if "image" in c]) == 2
    text = " ".join(c["text"] for c in content if "text" in c)
    # Never the first AI's vendor number, its "why" or its verdict.
    for leaked in ("[34]", "DIFFERENT", "Different number", '34"'):
        assert leaked not in text


def test_the_answer_shape_is_strict() -> None:
    assert _conforms(YES, evaluator_schema())
    assert not _conforms({**YES, "same_thing": "maybe"}, evaluator_schema())
    assert not _conforms({**YES, "extra": "x"}, evaluator_schema())


def test_a_malformed_answer_is_asked_once_more_then_reported() -> None:
    client = _Client(
        _reply("not json"), _reply({"same_thing": "yes", "vendor_value": "1", "reason": "x"})
    )
    read = ask_evaluator(
        _question(), client=client, document_version_id="v", spend_guard=None, stored=None
    )
    assert read.answer is None and read.reason and "malformed" in read.reason
    assert [a.malformed for a in read.attempts] == [True, True]


def test_a_stored_answer_is_reused_and_the_budget_refusal_asks_nothing() -> None:
    client = _Client()
    read = ask_evaluator(
        _question(),
        client=client,
        document_version_id="v",
        spend_guard=None,
        stored=json.dumps(YES),
    )
    assert read.answer == YES and client.requests == []
    assert read.attempts[0].reused_from == "stored"

    class Guard:
        def reserve(self, *_args: object) -> None:
            raise SpendCapExceeded("cap")

    refused = ask_evaluator(
        _question(), client=client, document_version_id="v", spend_guard=Guard(), stored=None
    )
    assert refused.reason == "the package's AI budget was reached" and client.requests == []


# --- the close-ups -------------------------------------------------------------------------------


def _pdf() -> bytes:
    """A one-page 612 x 792 pt PDF with two black lines, written by hand (no extra package)."""
    stream = b"2 w 100 500 m 200 500 l S 400 300 m 400 400 l S"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
    out += b"startxref\n" + str(start).encode() + b"\n%%EOF\n"
    return bytes(out)


def test_both_close_ups_are_cut_around_their_spots_and_outlined() -> None:
    architect, vendor = evaluator_pictures(
        _pdf(),
        0,
        architect_box_pt=["100", "280", "200", "295"],
        vendor_box_px=[810, 850, 950, 890],
        vendor_dpi=200,
        vendor_crop_px=[700, 500, 1600, 2000],
    )
    for png in (architect, vendor):
        image = Image.open(io.BytesIO(png)).convert("RGB")
        assert max(image.size) <= 1600
        assert (255, 0, 255) in {color for _n, color in image.getcolors(1 << 22) or []}
    # The vendor close-up is the box padded, kept inside the vendor's views (left 700, top 500).
    pad = 200 * 200 // 72
    assert Image.open(io.BytesIO(vendor)).size == (950 + pad - 700, 890 + pad - 500)
