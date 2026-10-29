"""The page reviewer's please-check kinds (`likely_problems.REVIEW_KINDS`).

GV's reviewer flags much more than sizes: construction (supports, fillers, panels), drawing
completeness, material, finish and grain callouts, scope (by GV, the GC or the owner) and item
numbers and the title block. The page reviewer's items of those kinds are Likely problems ("please
check and confirm"), never red, and the screen names their kind in words.

Made-up items only; no AI call.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import card_fold, likely_problems, proof
from app.review.assistant.chat_contract import ChatFindingLikelyOut
from app.review.assistant.send_package import register_status


def _review_item(kind: str, **more: Any) -> dict[str, Any]:
    return {
        "id": "F3", "page": 4, "item": "Island top: support", "category": "countertop",
        "architect": "a support under the overhang", "vendor": "no support shown",
        "status": "check", "source": "architect", "check_reason": "pairing_not_certain",
        "created_by": "page_reviewer",
        likely_problems.FIELD: {
            "by": "page_reviewer", "proof": "likely_only", "kind": kind, "sure": "likely",
            "why": "The top overhangs 14 in at the seating side with nothing under it.",
            "words": "Please check and confirm: Island top: support: no support is shown under "
            "the 14 in overhang.",
        },
        **more,
    }  # fmt: skip


@pytest.mark.parametrize("kind", likely_problems.REVIEW_KINDS)
def test_a_review_kind_is_kept_with_who_found_it(kind: str) -> None:
    payload = likely_problems.payload(_review_item(kind))
    assert payload is not None
    assert payload["kind"] == kind
    assert payload["by"] == "page_reviewer"
    assert payload["words"].startswith("Please check and confirm: Island top: support")
    # The screen's contract takes it as it is.
    assert ChatFindingLikelyOut.model_validate(payload).kind == kind


def test_other_marks_still_say_the_whole_drawing_look() -> None:
    """Code steps that lay the same mark with their own name keep today's payload."""
    item = _review_item("count")
    item[likely_problems.FIELD] = {**item[likely_problems.FIELD], "by": "sink_centre"}
    payload = likely_problems.payload(item)
    assert payload is not None and payload["by"] == "whole_drawing_look"
    unknown = _review_item("something_else")
    assert (likely_problems.payload(unknown) or {})["kind"] == "size"


@pytest.mark.parametrize("kind", likely_problems.REVIEW_KINDS)
def test_a_review_kind_is_a_likely_problem_and_never_red(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _review_item(kind)
    assert proof.proof_group(item) == "likely_problem"
    assert register_status(item) == "likely_problem"
    # Even marked Wrong with every code check passing, it is never clouded by itself.
    wrong = {**item, "status": "different"}
    monkeypatch.setattr(proof, "code_proved", lambda f: True)
    assert proof.proof_group(wrong) == "likely_problem"
    assert not proof.proved_wrong(wrong)


@pytest.mark.parametrize("kind", likely_problems.REVIEW_KINDS)
def test_the_card_names_the_kind_in_words(kind: str) -> None:
    key, words = card_fold.question_key(_review_item(kind))
    assert key == f"look:{kind}"
    assert words == likely_problems.KIND_WORDS[kind]
    assert "size differs" not in words


def test_every_review_kind_has_words() -> None:
    assert set(likely_problems.KIND_WORDS) == set(likely_problems.REVIEW_KINDS)
    assert not set(likely_problems.REVIEW_KINDS) & set(likely_problems.KINDS)
