"""Option C (Anant, 2026-10-25): the AI may name the pair, only code proves the ends (`code_ends`).

A red that compares a vendor size with a reference size needs code's geometry to show both
dimensions end at the same places (`same_extent`). Where code cannot tell, two agreeing blind picks
no longer make it red: the item is held back and shown as a Likely problem ("please check"),
never hidden on the Not-compared list. Geometry that shows different ends blocks, as before; a
Right stays Right. Made-up values only.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.review.assistant import proof
from app.review.assistant.chat_trust import judged
from app.review.assistant.full_check_advocate import (
    ENDS_NOT_CONFIRMED,
    STRONG_PICK,
    Check,
    Extent,
    Verdict,
    decide,
)

_SPANS = {"from": "back_of_cabinet", "to": "front_edge"}
_PICK = {
    "pick": "p1.D7",
    "vendor_spans": _SPANS,
    "architect_spans": _SPANS,
    "vendor_ink_black": "yes",
    STRONG_PICK: True,
}
_SAME: tuple[Extent, str] = ("same_extent", "both dimension lines end on the same drawn extent")
_CANNOT: tuple[Extent, str] = ("cannot_tell", "a dimension line's ends were not found")
_APART: tuple[Extent, str] = ("different_extent", "the two dimension lines end at different places")


def _decide(extent: tuple[Extent, str], mode: str = "code_ends") -> Verdict:
    return decide(
        mode=mode,
        checks=[Check(n, "pass") for n in ("ink", "annotation", "roles", "axis", "round")],
        printed=True,
        helper_pick="p1.D7",
        advocate=dict(_PICK),
        extent=extent,
        code_paired=False,
        item="Run: depth",
    )


def _finding(verdict: Verdict, **more: Any) -> dict[str, Any]:
    """A Wrong the helper paired with the reference (made-up 23" against 24"), code reading both
    values at their spots, with the devil's advocate's verdict written as the roster writes it."""
    critic = verdict.critic()
    return judged(
        {
            "id": "F1",
            "page": 2,
            "vs_page": 1,
            "item": "Run: depth",
            "category": "cabinet",
            "status": "different",
            "vendor": '23"',
            "architect": '24"',
            "source": "architect",
            "vendor_box": {"page": 2, "x0": 100, "y0": 100, "x1": 120, "y1": 110},
            "arch_box": {"page": 1, "x0": 100, "y0": 100, "x1": 120, "y1": 110},
            "checks": {
                "printed_architect": "match",
                "printed_vendor": "match",
                "critic": critic["result"],
            },
            "critic": critic,
            "inches": {"architect": "24", "vendor": "23", "difference": "-1"},
            **more,
        }
    )


def test_code_ends_is_the_default() -> None:
    assert Settings.model_fields["assistant_pairing_proof"].default == "code_ends"


def test_code_confirming_the_ends_still_allows_red() -> None:
    verdict = _decide(_SAME)
    assert verdict.result == "passed" and verdict.answers["pairing"] == "proved"
    finding = _finding(verdict)
    assert proof.pairing_proved(finding)
    assert proof.proof_group(finding) == "clouded"
    assert not proof.ends_unconfirmed(finding)


def test_ends_code_cannot_tell_are_a_likely_problem_with_the_please_check_words() -> None:
    verdict = _decide(_CANNOT)
    assert verdict.result == "objects"
    assert verdict.answers["pairing"] == ENDS_NOT_CONFIRMED
    finding = _finding(verdict)
    assert not proof.code_proved(finding)
    assert proof.ends_unconfirmed(finding)
    assert proof.proof_group(finding) == "likely_problem"  # held back, never hidden
    words = proof.likely_words(finding)
    assert "the AI pairs the vendor's 23\" with the reference's 24\"" in words
    assert 'they differ by 1"' in words
    assert "code could not confirm both dimensions measure the same thing" in words
    shown = proof.proof_of(finding)
    assert shown["group"] == "likely_problem" and shown["should_be"] is None  # never an error
    assert shown["reason"] == proof.ends_words(finding)

    # The same picks under the old rule still made it red (what option C changes).
    before = _finding(_decide(_CANNOT, mode="ai_pair_plus_code"))
    assert before["critic"]["answers"]["pairing"] == "proved_by_two_picks"
    assert proof.proof_group(before) == "clouded"


def test_ends_code_shows_apart_stay_not_compared_as_today() -> None:
    for mode in ("code_ends", "ai_pair_plus_code"):
        verdict = _decide(_APART, mode=mode)
        assert verdict.result == "objects" and "different places" in verdict.reason
        finding = _finding(verdict)
        assert not proof.ends_unconfirmed(finding)
        assert proof.proof_group(finding) == "not_compared"


def test_a_held_back_item_that_fails_another_proof_is_not_a_likely_problem() -> None:
    """Only an item code would have clouded on the picks is lifted to a Likely problem: one code
    never read at its spot stays where it was."""
    finding = _finding(_decide(_CANNOT), checks={"critic": "objects"})
    assert not proof.ends_unconfirmed(finding)
    assert proof.proof_group(finding) == "not_compared"


def test_a_right_stays_right() -> None:
    right = _finding(_decide(_CANNOT), status="same", architect='23"')
    assert proof.proof_group(right) == "right"


def test_a_value_not_drawn_to_its_length_is_held_back_and_says_so() -> None:
    """Final plan #3: a value whose drawn length disagrees with its view's scale never feeds a red;
    the please-check line says how long it is drawn."""
    drawn: tuple[Extent, str] = (
        "cannot_tell",
        (
            "the vendor's 23\" is drawn 24\" long at its view's own scale: its number does not "
            "agree with its drawing"
        ),
    )
    finding = _finding(_decide(drawn))
    assert proof.proof_group(finding) == "likely_problem"
    words = proof.ends_words(finding)
    assert words.endswith(
        "code could not confirm both dimensions measure the same thing; the vendor's 23\" is "
        "drawn 24\" long at its view's own scale."
    ), words
