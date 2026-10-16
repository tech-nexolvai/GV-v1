"""Answers the old guard refused and the chat-assistant style now lets the model write (#1128).

Owner, 2026-10-16: "make sure that ChatGPT style we have to do now; no complicated things." The
model now reads numbers off the drawings, counts, compares and describes in its own words. These
are the earlier refusal cases that are now allowed: numbers and counts, plain descriptions of a
drawing or a difference, yes and no, and record facts the app gives a subject when it renders
them. They stay listed (rather than deleted) so the change is visible case by case.

What is still refused is unchanged by this list: a claimed or advised action (approve, sign off,
confirm, fix, agree, mark), a pass or fail in the model's own words, a sign-off readiness claim,
a recorded result twisted or put on another page (`test_chatgpt_style.py`). Made-up records only.
"""

from __future__ import annotations

from typing import Final

from app.review.assistant.contract import Draft
from app.review.assistant.guard import check
from app.review.assistant.records import ReviewSnapshot

__all__ = ["CHAT_STYLE_ALLOWED", "allowed_now"]

CHAT_STYLE_ALLOWED: Final = frozenset(
    {
        "Only 1 item needs your decision before sign-off.",
        "1 item needs your decision.",
        "Zero items need your decision.",
        "0 items need your decision.",
        "None of the items need you.",
        "Only 2 items need your decision.",
        "3 findings still need a valid reviewer decision or a check rerun after correction. Add a note when required.",
        "{C1.page} has {count.pass}.",
        "{P3.no_countertop} {P9.second_row}",
        "# {C1.page}",
        "{C1.page} & {C1.label}",
        "{C1.page} is about four inches off.",
        "{C1.page} is ½ inch out.",
        "{C1.page} has ٤ pieces.",
        "{C1.page} has ４ pieces.",
        "The second piece is {C1.piece.1}.",
        "Both ends of {C1.page} are open.",
        "No correction is wanted on {C1.page}.",
        "{C3.page} is too long.",
        "{C3.page} has an issue.",
        "{C1.page} lines up.",
        "{C3.page} is off.",
        "{C3.page} is red.",
        "{C1.page} is pending.",
        "The reviewer turned {C3.page} down.",
        "The countertop run A on {C1.page} {C1.outcome}.",
        "{C1.page} is right.",
        "{C3.page} is short.",
        "There is nothing for you to look at on {C1.page}.",
        "The countertop on {C1.page} agrees with the drawing.",
        "The countertop on {C1.page} fits the wall exactly.",
        "The countertop on {C1.page} is longer than the wall.",
        "The countertop on {C1.page} is wider than it ought to be.",
        "The pieces on {C1.page} add up exactly.",
        "The pieces on {C1.page} sum exactly to the printed overall.",
        "The pieces on {C1.page} come to exactly the printed overall.",
        "The vendor drew {C1.page} without any mistakes.",
        "Nothing is missing on {C1.page}.",
        "The countertop on {C1.page} is spotless.",
        "The countertop on {C3.page} is the only countertop that {C3.outcome}.",
        "The countertop on {C1.page} alone {C1.outcome}.",
        "A handful of countertops look this way.",
        "{signoff.status} as soon as you look at the drawing.",
        "After a quick look, {signoff.status}.",
        "On the page you are looking at, {count.fail}.",
        "{C3.page} {C3.outcome}.",
        "Everything on {C3.page} {C3.outcome}.",
        "The countertops on {C3.page} {C3.outcome}.",
        "The whole drawing on {C3.page} {C3.outcome}.",
        "The countertop on {C3.page} approx. {C1.difference}.",
        "The countertop on {C1.page} is the reason. {C2.hold_reason}.",
        "{C2.hold_reason}. That is why {C1.page} {C1.outcome}.",
        "Here is the countertop on {C3.page}. Its values: {C1.printed}; {C1.needed}; {C1.difference}.",
        "You may now go on to the next page after {C1.page}.",
        "This countertop on {C1.page} is out of your hands now.",
        "The vendor has redrawn {C1.page} since then.",
        "The countertop on {C1.page} {C1.outcome}, a small cosmetic gap the shop trims on site.",
        "{C1.difference} is within what the shop trims on site.",
        "{C1.difference} is negligible.",
        "{C1.difference} is tiny; the vendor is basically there.",
        "The architect would be happy with {C1.page}.",
        "The architect agrees with the vendor on {C1.page}.",
        "The reviewer already looked at {C1.page}.",
        "You already looked at {C1.page} last time.",
        "The vendor has addressed {C1.page}.",
        "The countertop on {C1.page} does not exceed anything.",
        "Nothing to worry about.",
        "No worries here.",
        "This package is ship shape.",
        "This package is in good shape.",
        "The package is in decent shape.",
        "The package is tidy.",
        "The package is sign off material.",
        "I recommend asking the vendor to redraw it.",
        "Have the vendor redraw {C1.page}.",
        "Ask the vendor to redraw {C3.page}.",
        "The architect drew {C1.page} the same way.",
        "{C1.page} has a tiny gap only.",
        "A gap like {C1.difference} is usually trimmed on site.",
        "Small gaps like this are common and usually minor.",
        "This is likely a drafting slip by the vendor.",
        "The vendor probably mistyped the overall.",
        "It looks like the vendor forgot the field cut.",
        "The AIs probably misread the overall on {C1.page}.",
        "The others are in order.",
        "Other countertops are without trouble.",
        "Other countertops are without trouble, so only {C1.page} matters.",
        "Yes.",
        "No.",
        "Not yet.",
        "Yes, {signoff.status}.",
        "Yes. {C3.decision}.",
        "No, it is not.",
        "Is it blocked? No.",
        "Not anymore.",
        "Here is the countertop on {C3.page}. Its values: {C1.printed}.",
        "{C3.page}:\n- {C1.printed}",
        "The guess on {R1.page} is twelve and a half.",
        "The guess on {R1.page} is 12 1/2.",
        "{R1.what} on {R1.page} and the countertop on {C1.page} {C1.outcome}.",
    }
)


def allowed_now(text: str, snapshot: ReviewSnapshot, evidence: tuple[str, ...] = ()) -> bool:
    """Whether the case is one the chat style allows; if so, assert the guard accepts it."""
    if text not in CHAT_STYLE_ALLOWED:
        return False
    check(Draft(text=text, evidence=evidence), snapshot)
    return True
