"""The assistant's guard (#1128): the model never writes a number, an outcome or a decision.

An answer is a template; every fact is a placeholder code fills from the records. The guard checks
that the model's own words cannot carry a fact. Three sets of cases:

- every probe the two independent reviews wrote (`review_probes.py`), in the old free-text form:
  all refused;
- the same attacks rewritten in placeholder form (misattribution, negation, unlisted outcome,
  decision and sign-off words, homoglyphs, hidden characters, markup, unknown placeholders):
  all refused;
- realistic answers a model following the prompt would write (`REALISTIC`): these must pass, and
  the false-rejection rate is asserted (aim: at most 10 %).

Synthetic records (`synthetic.py`): C1 = page 4, needs correction; C2 = page 7, needs your decision
(held); C3 = page 9, looks right, decision carried over; F1 = page 5, waiting on a value; page 3
has no countertop; page 9 has a second countertop row not checked; sign-off is not possible.
"""

from __future__ import annotations

import pytest

from app.review.assistant.contract import Draft
from app.review.assistant.guard import GuardRejected, check, forbidden_words, placeholder_keys
from app.review.assistant.placeholders import record_fields, render
from tests.review.assistant.review_probes import REVIEW_PROBES
from tests.review.assistant.synthetic import Inputs, Readiness, snapshot

SNAPSHOT = snapshot()
READY = snapshot(
    Inputs(readiness=Readiness(can_approve=True, blocking_findings=0, blocking_finding_ids=()))
)


def _refused(draft: Draft, code: str | None = None) -> None:
    with pytest.raises(GuardRejected) as raised:
        check(draft, SNAPSHOT)
    if code is not None:
        assert str(raised.value) == code


# ---- every review probe, in the old free-text form, is refused ---------------------------------


@pytest.mark.parametrize(("text", "citations", "evidence"), REVIEW_PROBES)
def test_every_review_probe_is_refused(
    text: str, citations: tuple[str, ...], evidence: tuple[str, ...]
) -> None:
    del citations  # the model no longer names citations; code builds them from placeholders
    with pytest.raises(GuardRejected):
        check(Draft(text=text, evidence=evidence), SNAPSHOT)


# ---- the same attacks in placeholder form ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        # one sentence, two records: a fact cannot sit beside another record
        ("{C1.page} and {C3.page} are {C3.outcome}.", "two-records-in-one-sentence"),
        ("{C1.page}, {C3.outcome}.", "two-records-in-one-sentence"),
        ("{C1.page} (like {C3.page}) is {C1.outcome}.", "two-records-in-one-sentence"),
        ("{C1.page} has {count.pass}.", "package-fact-beside-a-record"),
        ("{C1.outcome} on {P9.page}.", "another-page-beside-a-record"),
        ("{P3.no_countertop} {P9.second_row}", "two-pages-in-one-sentence"),
        # unknown placeholders
        ("{C9.outcome}.", "unknown-placeholder"),
        ("{C1.colour}.", "unknown-placeholder"),
        ("{C2.printed}.", "unknown-placeholder"),  # C2 has no printed value
        ("{C1.piece.9}.", "unknown-placeholder"),
        ("{count.anything}.", "unknown-placeholder"),
        ("{P12.page}.", "unknown-placeholder"),
        ("{C1.page.}", "marker-or-brace-in-text"),
        ("{ C1.page }", "marker-or-brace-in-text"),
        # markers, links, markup
        ("{C1.outcome} [[0]].", "marker-or-brace-in-text"),
        ("{C1.outcome} [[ 0 ]].", "marker-or-brace-in-text"),
        ("See https://example.com about {C1.page}.", "link-or-markup"),
        ("See example.com/x about {C1.page}.", "link-or-markup"),
        ("{C1.outcome} [click](x).", "link-or-markup"),
        ("<b>{C1.outcome}</b>", "link-or-markup"),
        ("&lt;script&gt; {C1.page}", "link-or-markup"),
        ("**{C1.outcome}**", "symbol-in-text"),
        ("# {C1.page}", "symbol-in-text"),
        ("{C1.page} & {C1.label}", "symbol-in-text"),
        # numbers in any form
        ("{C1.page} is about four inches off.", "refused"),
        ("{C1.page} is ½ inch out.", "number-in-text"),
        ("{C1.page} has ٤ pieces.", "number-in-text"),
        ("{C1.page} has ４ pieces.", "number-in-text"),
        ("The second piece is {C1.piece.1}.", "refused"),
        ("Both ends of {C1.page} are open.", "refused"),
        # outcome, judgement and negation words
        ("{C1.page} looks right.", "refused"),
        ("{C1.page} is fine.", "refused"),
        ("{C1.page} checks out.", "refused"),
        ("{C1.page} is good.", "refused"),
        ("Nothing is wrong with {C1.page}.", "refused"),
        ("No correction is wanted on {C1.page}.", "refused"),
        ("{C3.page} is out of spec.", "refused"),
        ("{C3.page} is too long.", "refused"),
        ("{C3.page} has an issue.", "refused"),
        ("{C1.page} is accurate and valid.", "refused"),
        ("{C1.page} lines up.", "refused"),
        ("{C1.page} is spot on.", "refused"),
        ("{C3.page} is off.", "refused"),
        ("{C3.page} is red.", "refused"),
        ("{C1.page} is pending.", "refused"),
        # decisions and sign-off
        ("{C1.page} has been cleared.", "refused"),
        ("You can approve {C3.page}.", "refused"),
        ("{C3.page} is {C3.outcome}, so you should approve it.", "refused"),
        ("I have updated {C2.page}.", "refused"),
        ("The reviewer turned {C3.page} down.", "refused"),
        ("{C3.page} should be sent back to the vendor.", "refused"),
        ("The review is complete.", "refused"),
        ("No decisions are outstanding.", "refused"),
        ("You are free to sign.", "refused"),
        ("The package can be released.", "refused"),
        ("Every item has a decision.", "refused"),
        ("There is nothing more to do.", "refused"),
        ("Go ahead.", "refused"),
        # disguises
        ("{C1.page} l\u043e\u043eks fine.", "non-latin-letter"),
        ("{C1.page} pa\u200bsses.", "hidden-character"),
        ("{C1.page} \u202eenif si", "hidden-character"),
        ("{C1.page} p-a-s-s-e-s.", "refused"),
        ("{C1.page} PASSES.", "refused"),
        ("{C1.page} passeś.", "refused"),
        ("{C1.page} is OK'd.", "refused"),
    ],
)
def test_placeholder_form_attacks_are_refused(text: str, code: str) -> None:
    # The first rule that refuses it may differ from the one named; any refusal is the point.
    del code
    _refused(Draft(text=text))


def test_a_queue_button_only_for_a_record_that_needs_the_reviewer() -> None:
    _refused(
        Draft(
            text="The countertop on {C3.page} {C3.outcome}.", actions=(("open_queue_item", "C3"),)
        ),
        "action-record-needs-nothing",
    )
    check(
        Draft(
            text="The countertop on {C1.page} {C1.outcome}.",
            actions=(("open_queue_item", "C1"),),
        ),
        SNAPSHOT,
    )


@pytest.mark.parametrize(
    ("draft", "code"),
    [
        (Draft(text="See it.", evidence=("C9",)), "evidence-unknown-record"),
        (Draft(text="See it.", evidence=("drawings",)), "evidence-unknown-record"),
        (Draft(text="Open it.", actions=(("open_page", "P12"),)), "action-unknown-page"),
        (Draft(text="Open it.", actions=(("open_queue_item", "F7"),)), "action-unknown-record"),
        (Draft(text="Open it.", actions=(("approve", "C1"),)), "action-unknown-kind"),
        (Draft(text="  "), "empty-text"),
    ],
)
def test_references_must_exist(draft: Draft, code: str) -> None:
    _refused(draft, code)


# ---- wall and layout words stay usable ---------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "On {C1.page} the wall is on the left and the open end is on the right.",
        "On {C1.page} the right end and the left-hand wall are on the drawing.",
        "On {C1.page} the short return and the long side sit against walls.",
        "The rightmost piece on {C1.page} is {C1.piece.3}.",
    ],
)
def test_directional_wall_words_pass(text: str) -> None:
    check(Draft(text=text), SNAPSHOT)


@pytest.mark.parametrize("text", ["{C1.page} is right.", "{C3.page} is short."])
def test_right_and_short_as_judgements_are_refused(text: str) -> None:
    _refused(Draft(text=text))


# ---- realistic answers a model following the prompt writes -------------------------------------

REALISTIC: list[Draft] = [
    Draft(
        "The countertop on {C1.page} {C1.outcome}. {C1.printed}; {C1.needed}; {C1.difference}. "
        "{C1.reason}. {C1.needs_you}.",
        evidence=("C1",),
        actions=(("open_queue_item", "C1"),),
    ),
    Draft("The countertop on {C1.page} {C1.outcome}: {C1.printed}, but {C1.needed}."),
    Draft(
        "The countertop on {C1.page} {C1.outcome}. The vendor's printed length and the length "
        "the pieces and field cuts call for are different: {C1.printed}; {C1.needed}."
    ),
    Draft("{C1.reason}. On the drawing, {C1.printed} and {C1.needed}."),
    Draft(
        "{signoff.status}.\n- The countertop on {C1.page} {C1.outcome}\n- The countertop on "
        "{C2.page} {C2.outcome}\n- The {F1.check} check on {F1.page} {F1.outcome}",
        evidence=("blockers",),
    ),
    Draft("{signoff.status}. Open the queue to work through them."),
    Draft("{count.no_countertop_pages}:\n- {P3.no_countertop}", evidence=("no_countertop_pages",)),
    Draft("{P3.no_countertop}. There is no countertop to measure on that sheet."),
    Draft(
        "The countertop on {C2.page} {C2.outcome}. {C2.hold_reason}. Open it in the queue to look "
        "at it on the drawing.",
        actions=(("open_queue_item", "C2"),),
    ),
    Draft("{C2.hold_reason}. Because of that, nothing was read on {C2.page}."),
    Draft("The countertop on {C2.page} {C2.outcome}. {C2.architect}."),
    Draft("The countertop on {C3.page} {C3.outcome}. {C3.printed}; {C3.needed}. {C3.decision}."),
    Draft("{C3.decision}. The countertop on {C3.page} {C3.outcome}."),
    Draft("The countertop called {C3.label} on {C3.page} {C3.outcome}."),
    Draft("{P9.second_row}. Only the countertop line that was read is in the results."),
    Draft("{count.rows_not_checked}:\n- {P9.second_row}", evidence=("rows_not_checked",)),
    Draft("The {F1.check} check on {F1.page} {F1.outcome}. {F1.reason}. {F1.needs_you}."),
    Draft("{F1.reason}. Enter the missing value to let the check run."),
    Draft("The walls on {C1.page}: {C1.walls}."),
    Draft("The countertop on {C1.page} has these pieces: {C1.pieces}. {C1.field_cut}."),
    Draft(
        "On {C1.page}, {C1.piece.2}, which sits between the cabinets on the left and right ends."
    ),
    Draft("{C1.check} on {C1.page} uses {C1.tolerance}."),
    Draft("{count.countertops} were read in this review."),
    Draft("{count.fail}."),
    Draft("{count.pass}. {count.needs_you}."),
    Draft("The records do not say who drew the cabinets. Open {P4.page} on the drawing to look."),
    Draft("I can't find that in this review's records. Try opening {P4.page} on the drawing."),
    Draft(
        "The countertop on {C1.page} {C1.outcome}. Open it on the drawing to compare the printed "
        "overall."
    ),
    Draft(
        "The countertop on {C2.page} {C2.outcome}. The readers chose different lines, so nothing "
        "was read."
    ),
    Draft("The countertop on {C1.page} {C1.outcome}, with {C1.difference}."),
    Draft("{signoff.status}. Start with {C1.page}, then look at the others in the queue."),
]


def test_realistic_answers_are_rarely_refused() -> None:
    refused = []
    for draft in REALISTIC:
        try:
            check(draft, SNAPSHOT)
        except GuardRejected as rejected:
            refused.append((draft.text, str(rejected)))
    rate = len(refused) / len(REALISTIC)
    assert rate <= 0.10, refused


@pytest.mark.parametrize("draft", REALISTIC[:5])
def test_realistic_answers_render_with_code_written_facts_and_citations(draft: Draft) -> None:
    rendered = render(draft.text, SNAPSHOT)
    assert "{" not in rendered.text
    assert "[[0]]" in rendered.text
    assert rendered.citations == placeholder_keys(draft.text)


def test_a_rendered_fact_is_the_records_own_text() -> None:
    rendered = render("The countertop on {C1.page} {C1.outcome}. {C1.printed}.", SNAPSHOT)
    assert rendered.text == (
        "The countertop on page 4 needs correction [[0]]. "
        'On page 4, the printed overall, 84 1/2" [[0]].'
    )
    assert rendered.citations == ("C1",)


def test_sign_off_is_written_by_code_from_readiness() -> None:
    assert render("{signoff.status}.", SNAPSHOT).text == (
        "Sign-off is blocked: 3 findings still need your decision."
    )
    assert render("{signoff.status}.", READY).text == "You can sign off."


def test_the_prompt_offers_only_placeholders_the_records_fill() -> None:
    fields = record_fields(SNAPSHOT)
    assert "printed" in fields["C1"] and "printed" not in fields["C2"]
    assert "piece.3" in fields["C1"]
    assert fields["P3"] == ["page", "no_countertop"]
    assert fields["P9"] == ["page", "second_row"]
    for key, names in fields.items():
        for name in names:
            if name != "outcome":  # an outcome is checked with its subject, above
                check(Draft(text=f"{{{key}.{name}}}."), SNAPSHOT, by_model=False)


def test_forbidden_words_lists_what_it_found() -> None:
    assert forbidden_words("This passed and is fine") == ("passed", "fine")
    assert forbidden_words("the right-hand wall on the left") == ()


@pytest.mark.parametrize(
    "text",
    [
        "Run A {C3.outcome}.",
        "The countertop run A on {C1.page} {C1.outcome}.",
        "Sample run C {C3.outcome}.",
        "The sample countertop {C3.outcome}.",
    ],
)
def test_a_label_in_the_models_own_words_is_refused(text: str) -> None:
    _refused(Draft(text=text), "label-in-own-words")


@pytest.mark.parametrize(
    "text",
    [
        "A countertop on {C3.page} {C3.outcome}.",
        "The countertop called {C3.label} on {C3.page} {C3.outcome}.",
        "The readers chose different lines on {C2.page}, so nothing was read.",
    ],
)
def test_the_label_placeholder_the_article_a_and_nothing_pass(text: str) -> None:
    check(Draft(text=text), SNAPSHOT)


@pytest.mark.parametrize(
    "text",
    ["Nothing is wrong with {C1.page}.", "Nothing is pending.", "Nothing remains on {C1.page}."],
)
def test_nothing_before_an_outcome_word_is_still_refused(text: str) -> None:
    _refused(Draft(text=text))


@pytest.mark.parametrize(
    ("outcome", "phrase"),
    [
        ("C1", "needs correction"),
        ("C2", "needs your decision"),
        ("C3", "looks right; you confirmed it (carried over from the earlier run)"),
        ("F1", "is waiting on a value"),
    ],
)
def test_the_outcome_placeholder_is_a_verb_phrase(outcome: str, phrase: str) -> None:
    rendered = render(f"The countertop {{{outcome}.outcome}}.", SNAPSHOT)
    assert rendered.text == f"The countertop {phrase} [[0]]."


def test_a_count_word_is_still_refused_even_in_a_plain_explanation() -> None:
    # "two" is a count; counts come only from {count.*} placeholders.
    _refused(Draft(text="The two readers chose different lines on {C2.page}."))
