"""The same item printed in two sizes on two of the vendor's pages (`same_item_pages`, #1906):
made-up labels and readings, no client data."""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import likely_problems, proof, proof_words, send_package
from app.review.assistant import same_item_pages as sip
from app.review.assistant.chat_trust import judged

FILE = "vendor.pdf"


def _ledger_item(**more: Any) -> dict[str, Any]:
    """The reading ledger's item: the AI read "Base B-30 width" 30" on page 12 and 32" on 15."""
    return {
        "page": 15,
        "vs_page": 12,
        "item": "Base B-30 width (same item, different views)",
        "category": "cabinet",
        "architect": '30"',
        "vendor": '32"',
        "status": "check",
        "why": "The vendor's drawings disagree (same item, different views): ...",
        "verified": False,
        "source": "cross_view",
        "reference_side": "vendor",
        "created_by": "ledger",
        "ledger_ids": ["R1", "R2"],
        "arch_box": {"page": 12, "x0": 100.0, "y0": 100.0, "x1": 130.0, "y1": 112.0},
        "vendor_box": {"page": 15, "x0": 200.0, "y0": 300.0, "x1": 230.0, "y1": 312.0},
        **more,
    }


def _label(n: int, box: list[float], inches: str, **more: Any) -> dict[str, Any]:
    return {
        "id": f"D{n}",
        "text": f'{inches}"',
        "inches": inches,
        "box": box,
        "side": "vendor",
        "route": "text",
        **more,
    }


def _dims(first: dict[str, Any] | None = None, second: dict[str, Any] | None = None) -> Any:
    return {
        "files": [FILE],
        "pages": [
            {
                "file": FILE,
                "page": 12,
                "labels": [first or _label(4, [105.0, 102.0, 125.0, 110.0], "30")],
            },
            {
                "file": FILE,
                "page": 15,
                "labels": [second or _label(9, [205.0, 302.0, 225.0, 310.0], "32")],
            },
        ],
    }


def _page_map(first_round: int | None, second_round: int | None) -> Any:
    def entry(page: int, round_: int | None) -> dict[str, Any]:
        half = {"box": [0.0, 0.0, 600.0, 800.0], "role": "vendor", "round": round_}
        return {"file": FILE, "page": page, "halves": [half]}

    return {"pages": [entry(12, first_round), entry(15, second_round)]}


def test_code_reading_both_labels_makes_a_red_with_no_should_be_picked() -> None:
    made = sip.settle(_ledger_item(), _dims(), _page_map(1, 1), FILE)
    assert made["status"] == "different" and sip.marked(made)
    assert made["dims"]["architect"]["id"] == "D4" and made["dims"]["vendor"]["id"] == "D9"
    assert made["dims"]["architect"]["page"] == 12 and made["dims"]["vendor"]["page"] == 15
    assert "which one is not known" in made["why"]
    words = '30" (page 12) or 32" (page 15): correct one so both pages agree'
    assert proof.should_be_value(made) == words
    assert proof.governs(made).startswith("the vendor's own drawings of the same item disagree")
    assert proof.vendor_own(made) == "pages"
    assert proof_words.reference_kind(made) == "pages"
    assert proof_words.difference_words(made) is None
    sentence = proof_words.sentence(made)
    assert sentence.startswith('Vendor prints 32"') and "page 12" in sentence
    assert "Correct one of them so both pages agree" in sentence
    assert "Change it to" not in sentence
    assert send_package.requires_words(made).startswith(
        "The vendor's own drawings of the same item disagree"
    )


@pytest.mark.usefixtures("read_twice")
def test_both_labels_read_and_printed_where_code_looked_is_clouded() -> None:
    made = sip.settle(_ledger_item(), _dims(), _page_map(None, None), FILE)
    checked = judged(
        {
            **made,
            "id": "F1",
            "checks": {"printed_architect": "match", "printed_vendor": "match"},
            # as the finding book's checks give them (`chat_trust.apply_checks`)
            "inches": {"architect": "30", "vendor": "32", "difference": "2"},
        }
    )
    assert proof.proof_group(checked) == "clouded"


def test_code_unable_to_read_one_label_makes_a_likely_problem() -> None:
    drawn = _label(9, [205.0, 302.0, 225.0, 310.0], "32", route="drawn")
    for dims in (
        _dims(second=drawn),
        _dims(second=_label(9, [205.0, 302.0, 225.0, 310.0], "36")),  # another value there
        _dims(second=_label(9, [400.0, 402.0, 420.0, 410.0], "32")),  # nothing at the spot
        {"pages": []},
    ):
        made = sip.settle(_ledger_item(), dims, _page_map(1, 1), FILE)
        assert made["status"] == "check" and not sip.marked(made)
        assert likely_problems.likely(made) and likely_problems.likely_only(made)
        assert proof.proof_group({**made, "id": "F1"}) == "likely_problem"
        assert "Please check and confirm" in likely_problems.please_check(made)


def test_two_rounds_of_one_drawing_are_a_revision_not_a_mistake() -> None:
    item = _ledger_item()
    assert sip.settle(item, _dims(), _page_map(1, 2), FILE) == item


def test_the_same_page_a_tag_or_another_maker_is_left_as_it_is() -> None:
    same_page = _ledger_item(
        vs_page=None,
        arch_box={"page": 15, "x0": 100.0, "y0": 100.0, "x1": 130.0, "y1": 112.0},
    )
    assert sip.settle(same_page, _dims(), None, FILE) == same_page
    tag = _ledger_item(architect="B-30", vendor="B-33")
    assert sip.settle(tag, _dims(), None, FILE) == tag
    architect = _ledger_item(reference_side="architect")
    assert sip.settle(architect, _dims(), None, FILE) == architect
    consistency = _ledger_item(created_by="consistency", status="different")
    assert sip.settle(consistency, _dims(), None, FILE) == consistency
