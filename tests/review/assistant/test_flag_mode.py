"""Flag mode (Anant, DECIDED 2026-11-04): every problem found is flagged, code does the maths.

`GV_ASSISTANT_FLAG_MODE=flag` (`Settings.assistant_flag_mode`). The same group names, so the screen,
the files and the CSV keep working; only who goes where changes (`proof._flag_group`):

- **clouded** (red): a difference code's maths checked, with no blind second look and no track
  record needed; a blind look that disagrees, or a best-guess page, keeps it a please-check;
- **likely_problem**: every other item that says something differs, whatever code could not
  verify;
- **not_compared**: only what had nothing to compare;
- **right**: kept in the data, never counted or listed.

The default (`proof`) keeps every proof rule as it was. Made-up values only.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.review.assistant import (
    card_fold,
    check_record,
    classification,
    dim_ends,
    drafting_notes,
    proof,
    review_action,
    stuck_report,
)
from app.review.assistant.chat_trust import judged
from app.review.assistant.full_check_advocate import NOT_VERIFIED
from app.review.assistant.review_action import CheckState
from app.review.assistant.send_package import PackageContext, PackageIdentity, register_rows

_IDENTITY = PackageIdentity(
    project="Test project",
    vendor="Test vendor",
    submittal=7,
    received="2026-11-01",
    products=("cabinet",),
)


@pytest.fixture
def flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(classification.FLAG_MODE_ENV, "flag")


def _box(page: int, x: float) -> dict[str, Any]:
    return {"page": page, "x0": x, "y0": 100, "x1": x + 20, "y1": 110}


def _wrong(**more: Any) -> dict[str, Any]:
    """A Wrong a helper paired: the vendor's 23" against the architect's 24", code reading both
    at their spots (made up)."""
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 3,
        "vs_page": 9,
        "item": "Base run: depth",
        "category": "cabinet",
        "status": "different",
        "vendor": '23"',
        "architect": '24"',
        "source": "architect",
        "created_by": "helper:4",
        "vendor_box": _box(3, 100),
        "arch_box": _box(9, 300),
        "checks": {"printed_architect": "match", "printed_vendor": "match"},
        "inches": {"architect": "24", "vendor": "23", "difference": "-1"},
        **more,
    }
    return judged(finding)


def _advocate(note: str, **answers: str) -> dict[str, Any]:
    return {"model": "devil's advocate", "result": "objects", "note": note, "answers": answers}


def _ai_only(**more: Any) -> dict[str, Any]:
    """A difference only an AI read (no code witness)."""
    return judged(_wrong(checks={}, **more))


def _right(**more: Any) -> dict[str, Any]:
    return judged(
        {
            "id": "R1",
            "page": 3,
            "item": "Base run: height",
            "status": "same",
            "vendor": '34 1/2"',
            "architect": '34 1/2"',
            "source": "architect",
            "created_by": "helper:4",
            "vendor_box": _box(3, 140),
            "arch_box": _box(9, 340),
            "checks": {"printed_architect": "match", "printed_vendor": "match"},
            "inches": {"architect": "34.5", "vendor": "34.5", "difference": "0"},
            **more,
        }
    )


def _look(**more: Any) -> dict[str, Any]:
    """The page reviewer's please-check (an AI looking at the sheet)."""
    return {
        "id": "F9",
        "page": 4,
        "item": "Sink: who supplies it",
        "status": "check",
        "vendor": "see the drawing",
        "architect": "n/a",
        "created_by": "page_reviewer",
        "check_reason": "pairing_not_certain",
        "whole_look": {
            "kind": "scope",
            "words": "Please check and confirm: who supplies it.",
            "proof": "likely_only",
            "by": "page_reviewer",
        },
        **more,
    }


# ---- the switch ------------------------------------------------------------------------------------


def test_the_setting_defaults_to_the_proof_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.model_fields["assistant_flag_mode"].default == "proof"
    monkeypatch.delenv(classification.FLAG_MODE_ENV, raising=False)
    assert not proof.flag_mode()
    monkeypatch.setenv(classification.FLAG_MODE_ENV, "proof")
    assert not proof.flag_mode()
    monkeypatch.setenv(classification.FLAG_MODE_ENV, " FLAG ")
    assert proof.flag_mode()


# ---- clouded: a difference code's maths checked ----------------------------------------------------


@pytest.mark.usefixtures("read_twice")
def test_no_blind_pick_needed_for_a_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """Code read both numbers; the blind pick was never made. Proof rules: not proved. Flag mode:
    red, code-checked."""
    finding = judged(
        {
            **_wrong(),
            "checks": {**_wrong()["checks"], "critic": "objects"},
            "critic": _advocate(f"{NOT_VERIFIED}.", ink="pass", round="pass"),
        }
    )
    assert proof.proof_group(finding) != "clouded"
    monkeypatch.setenv(classification.FLAG_MODE_ENV, "flag")
    assert proof.flag_checked(finding)
    assert proof.proof_group(finding) == "clouded"
    assert proof.checked_by(finding) == "code"
    shown = proof.proof_of(finding)
    assert shown["checked_by"] == "code" and shown["should_be"].startswith('24"')
    assert proof.group_words(finding, "clouded") == "Problem, code-checked (clouded)"


@pytest.mark.usefixtures("read_twice")
def test_no_track_record_hold_in_flag_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    finding = _wrong(paired_by="code")
    monkeypatch.setattr(check_record, "cleared", lambda kind: False)
    monkeypatch.setattr(check_record, "held_reason", lambda finding: "held (made up)")
    assert proof.proof_group(finding) == "likely_problem"
    assert proof.proof_of(finding)["held"] is True
    monkeypatch.setenv(classification.FLAG_MODE_ENV, "flag")
    assert proof.proof_group(finding) == "clouded"
    assert proof.held_back(finding) is None and proof.proof_of(finding)["held"] is False


@pytest.mark.usefixtures("read_twice", "flag")
def test_a_disagreeing_blind_look_keeps_it_a_please_check() -> None:
    finding = _wrong(paired_by="code", checks={**_wrong()["checks"], "second_read": "disagrees"})
    finding = judged(finding)
    assert proof.flag_checked(finding)
    assert proof.proof_group(finding) == "likely_problem"
    assert proof.flag_notes(finding) == [proof.READINGS_DIFFER]
    shown = proof.proof_of(finding)
    assert shown["reason"].startswith("readings differ; code-checked: ")
    assert shown["checked_by"] == "code"
    assert proof.likely_words(finding).endswith("(readings differ).")
    assert card_fold.question_key(finding)[0] == "flag:readings_differ"


@pytest.mark.usefixtures("read_twice", "flag")
def test_a_blind_pick_naming_another_dimension_is_a_disagreement() -> None:
    critic = _advocate(
        "Not proved: a blind second pick chose p9.D4, not p9.D2.",
        ink="pass", round="pass", advocate_pick="p9.D4", helper_pick="p9.D2",
    )  # fmt: skip
    finding = judged({**_wrong(), "critic": critic,
                      "checks": {**_wrong()["checks"], "critic": "objects"}})  # fmt: skip
    assert proof.proof_group(finding) == "likely_problem"
    assert proof.READINGS_DIFFER in proof.flag_notes(finding)


@pytest.mark.usefixtures("read_twice", "flag")
def test_a_failed_code_check_is_never_red_but_still_a_problem() -> None:
    """The advocate's own code check (an older round) stopped it: code's fact, never set aside."""
    critic = _advocate("Not proved: the vendor value is from an older round.", round="fail")
    finding = judged({**_wrong(), "critic": critic,
                      "checks": {**_wrong()["checks"], "critic": "objects"}})  # fmt: skip
    assert not proof.flag_checked(finding)
    assert proof.proof_group(finding) == "likely_problem"


@pytest.mark.usefixtures("flag")
def test_the_maths_step_makes_a_red_on_the_ais_own_numbers() -> None:
    finding = _ai_only(maths={"result": "differs", "words": '23" against 24": 1" short'})
    assert proof.proof_group(finding) == "clouded"
    assert proof.proof_line(finding).startswith('Code\'s maths: 23" against 24": 1" short')
    assert proof.maths_result(finding) == "differs"
    # Without the maths step's record the same AI reading is a please-check, found by AI.
    plain = _ai_only()
    assert proof.proof_group(plain) == "likely_problem"
    assert proof.checked_by(plain) == "ai"


@pytest.mark.usefixtures("flag")
def test_a_best_guess_page_keeps_every_flag_a_please_check_and_says_so() -> None:
    guess = {"page": 12, "words": "best guess: architect page 12"}
    finding = _ai_only(maths={"result": "differs", "words": "1 inch"}, pairing_guess=guess)
    assert proof.flag_checked(finding)
    assert proof.proof_group(finding) == "likely_problem"
    assert proof.proof_of(finding)["reason"].startswith("best guess: architect page 12; ")
    assert proof.likely_words(finding).endswith("(best guess: architect page 12).")
    assert card_fold.question_key(finding)[0] == "flag:best_guess"
    # A flag that only an AI found says the guess too.
    found = _ai_only(pairing_guess={"page": 12})
    assert proof.flag_notes(found) == ["best guess: architect page 12"]
    # GV's own rule is not compared with the architect: no guess to name.
    assert proof.flag_notes({**found, "source": "rules"}) == []


@pytest.mark.usefixtures("read_twice", "flag")
def test_a_best_guess_page_is_never_red_even_when_code_proved_it() -> None:
    """The pairing step's record as it writes it (a wrong guess is expected on about 1 page in 6):
    a comparison with the architect on such a page is a please-check, its words kept whole."""
    words = "best guess: architect page 17 (not confirmed; other candidate architect page 9)"
    finding = _wrong(paired_by="code", pairing_guess={"page": 17, "file": "a.pdf", "words": words})
    assert proof.code_proved(finding) and proof.flag_checked(finding)
    assert proof.proof_group(finding) == "likely_problem"
    assert proof.proof_of(finding)["reason"].startswith(f"{words}; code-checked: ")
    # The vendor's own string on that page does not rest on the architect's page: it may be red.
    own = judged(
        {
            **finding,
            "source": "vendor_arithmetic",
            "created_by": "consistency",
            "parts": ['11"', '12"'],
            "printed_total": '24"',
            "parts_sum": "23",
            "checks": {"arithmetic": "does_not_add_up"},
            "inches": {"architect": "23", "vendor": "24", "difference": "1"},
        }
    )
    assert proof.flag_notes(own) == []
    assert proof.proof_group(own) == "clouded"


def _maths(result: str, kind: str, words: str = "made up") -> dict[str, Any]:
    return {"result": result, "words": words, "kind": kind, "read_from": "fields"}


@pytest.mark.usefixtures("flag")
def test_the_maths_step_on_each_kind() -> None:
    """The maths step's record (`ai_maths`): the vendor's own string that does not add up is red;
    two sizes are red only on a confirmed page and never on a scaled value; GV's rule worked out
    on the AI's numbers stays a please-check, code-checked; "same" is no problem."""
    words = '2" + 47 1/8" + 3" add up to 52 1/8", but the overall is 53"'
    string = _look(maths=_maths("differs", "string", words), pairing_guess={"page": 4})
    assert proof.proof_group(string) == "clouded"  # a best guess never touches its own sum
    assert proof.should_be(string) == f"{words} (sum of the vendor's own parts, as read)"
    assert proof.checked_by(string) == "code"

    sizes = _ai_only(maths=_maths("differs", "sizes"))
    assert proof.proof_group(sizes) == "clouded"
    guessed = {**sizes, "pairing_guess": {"page": 4, "words": "best guess: architect page 4"}}
    assert proof.proof_group(guessed) == "likely_problem"
    scaled = _ai_only(maths=_maths("differs", "sizes"), check_reason="scaled")
    assert not proof.flag_checked(scaled)
    assert proof.proof_group(scaled) != "clouded"

    rule = _look(maths=_maths("differs", "rule"))
    assert proof.proof_group(rule) == "likely_problem"
    assert proof.flag_notes(rule) == [proof.GV_RULE_ON_AI]
    assert proof.checked_by(rule) == "code"

    text = _ai_only(maths=_maths("differs", "identifier"))
    assert not proof.flag_checked(text)

    same = _look(maths=_maths("same", "string"))
    assert proof.proof_group(same) == "right" and not proof.listed(same)
    assert proof.proof_group(_ai_only(maths=_maths("same", "sizes"))) == "right"


@pytest.mark.usefixtures("flag")
def test_the_maths_is_never_red_against_codes_own_facts() -> None:
    """Two sizes the AI itself said measure different things; a string code's own labels show is
    not the AI's (`string_reads` gap); a string a blind second reading adds up: still problems."""
    unsure = _ai_only(
        status="check", check_reason="different_extent", maths=_maths("differs", "sizes")
    )
    assert not proof.flag_checked(unsure)
    assert proof.proof_group(unsure) == "likely_problem"
    string = _look(status="different", maths=_maths("differs", "string"))
    gap = {**string, "string_reads": {"outcome": "gap", "why": "made up"}}
    assert not proof.flag_checked(gap)
    assert proof.proof_group(gap) == "likely_problem"
    adds = {**string, "string_reads": {"outcome": "adds_up", "why": "made up"}}
    assert proof.flag_checked(adds) and proof.flag_notes(adds) == [proof.READINGS_DIFFER]
    assert proof.proof_group(adds) == "likely_problem"


@pytest.mark.usefixtures("read_twice", "flag")
def test_codes_own_reading_wins_over_the_ais_same() -> None:
    finding = _wrong(paired_by="code", maths=_maths("same", "sizes"))
    assert proof.proof_group(finding) == "clouded"


@pytest.mark.usefixtures("flag")
def test_an_identifier_or_gvs_sticker_is_never_red() -> None:
    sticker = _ai_only(maths={"result": "differs"}, gv_sticker={"page": 3, "text": '24"'})
    assert not proof.flag_checked(sticker)
    assert proof.proof_group(sticker) == "likely_problem"


# ---- likely_problem: everything else that says something differs -----------------------------------


@pytest.mark.usefixtures("flag")
def test_what_code_could_not_verify_is_a_problem_not_not_compared() -> None:
    string = judged(
        {
            "id": "F3",
            "page": 5,
            "item": "Dimension string (across)",
            "status": "different",
            "vendor": '60"',
            "architect": '60 1/4"',
            "source": "vendor_arithmetic",
            "created_by": "helper:2",
            "parts": ['20"', '20 1/4"', '20"'],
            "printed_total": '60"',
            "checks": {"arithmetic": "does_not_add_up"},
        }
    )
    tags = _ai_only(
        vendor="AB-1", architect="AB-2", item="Top tag",
        tag_read={"outcome": "not_compared", "reason": "tags_differ", "why": "tags differ"},
    )  # fmt: skip
    words = judged(
        _wrong(vendor="by others", architect="by vendor", item="Faucet supplier",
               checks={"printed_vendor": "not_a_number"})
    )  # fmt: skip
    for finding, reason in ((string, "string_not_verified"), (tags, "tags_differ"),
                            (words, "words")):  # fmt: skip
        assert classification.classify(finding, proved=False).reason == reason
        assert proof.proof_group(finding) == "likely_problem", reason
        assert proof.checked_by(finding) == "ai"
        key, said = card_fold.question_key(finding)
        assert key == f"flag:{reason}" and said.startswith("Please check: ")
        assert stuck_report.group_of({**finding, "card": {"question": {"key": key}}}) == (
            "likely_problem"
        )
    # The why never opens with the Not-compared list's lead.
    string["string_reads"] = {"outcome": "gap", "why": 'Not compared: a gap of 3 3/4" (made up)'}
    assert proof.proof_of(string)["reason"].startswith("a gap of")


@pytest.mark.usefixtures("flag")
def test_an_ai_please_check_stays_a_problem() -> None:
    finding = _look()
    assert proof.proof_group(finding) == "likely_problem"
    assert proof.checked_by(finding) == "ai"
    assert proof.group_words(finding, "likely_problem") == "Problem: please check (found by AI)"


@pytest.mark.usefixtures("flag")
def test_a_code_checks_please_check_says_so() -> None:
    finding = _look(created_by="room_matrix")
    assert proof.checked_by(finding) == "code_check"


def test_proof_mode_problems_are_never_hidden_by_flag_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A held reading with a job value missing: a please-check under the proof rules, never moved
    to Not compared by flag mode."""
    finding = _wrong(paired_by="code", job_value_rule={"missing": ["sink inside depth"]})
    monkeypatch.setattr(
        "app.review.assistant.reading_routes.held", lambda finding: "a second reading was unsure"
    )
    assert proof.proof_group(finding) == "likely_problem"
    monkeypatch.setenv(classification.FLAG_MODE_ENV, "flag")
    assert proof.proof_group(finding) == "likely_problem"


# ---- not_compared: only when nothing was compared --------------------------------------------------


@pytest.mark.usefixtures("flag")
def test_nothing_to_compare_stays_not_compared() -> None:
    silent = _ai_only(status="check", architect="not shown", keep_reason="architect_prints_nothing")
    label = {"id": "D2", "page": 9}
    vif = _ai_only(
        architect='24" V.I.F.',
        dims={"architect": label},
        drafting_note={"label": "|9|D2", "kinds": ["field_verify"], "why": "field verify"},
    )
    internal = _ai_only(salvaged=True)
    copies = _ai_only(same_item={"copies": ["F1", "F2"], "conflict": ['24"', '25"']})
    revision = _ai_only(item="Title block: revision number", vendor="3", architect="4",
                        inches={})  # fmt: skip
    assert drafting_notes.softened(vif) == "field verify"
    for finding in (silent, vif, internal, copies, revision):
        assert proof.proof_group(finding) == "not_compared", finding
        assert proof.checked_by(finding) is None


@pytest.mark.usefixtures("flag")
def test_two_dimensions_code_proved_unlike_stay_not_compared() -> None:
    finding = _ai_only(
        maths={"result": "differs"},
        dims={"vendor": {"id": "D1", "page": 3}, "architect": {"id": "D2", "page": 9}},
    )
    finding[dim_ends.FIELD] = {
        "outcome": "unlike",
        "why": "the architect's runs to a centre line (made up)",
        "vendor": dim_ends._label_key(finding["dims"]["vendor"]),
        "architect": dim_ends._label_key(finding["dims"]["architect"]),
    }
    assert dim_ends.unlike_reason(finding) is not None
    assert proof.proof_group(finding) == "not_compared"


@pytest.mark.usefixtures("flag")
def test_a_missing_job_value_stays_not_compared() -> None:
    finding = _wrong(status="check", keep_reason="needs_job_value", why="front overhang")
    assert proof.classified(finding).reason == "missing_setting"
    assert proof.proof_group(finding) == "not_compared"


# ---- right: kept, never counted or listed -------------------------------------------------------------


@pytest.mark.usefixtures("flag")
def test_a_right_is_kept_but_never_listed() -> None:
    finding = _right()
    assert proof.proof_group(finding) == "right"
    assert not proof.listed(finding)
    assert proof.listed(_look())
    context = PackageContext(
        identity=_IDENTITY, date="2026-11-04", round_no=1, rounds=None, state=CheckState(),
        labels={},
    )  # fmt: skip
    rows = register_rows([finding, _look()], context)
    assert [row[5] for row in rows] == ["Please check and confirm"]
    result = review_action.review_action([finding, _look()])
    assert result.items == 1


def test_a_short_value_code_did_not_read_is_never_green(monkeypatch: pytest.MonkeyPatch) -> None:
    finding = _right(vendor="6", architect="6", item="Shelf count", checks={}, inches={})
    finding = judged(finding)
    assert proof.proof_group(finding) == "right"  # the proof rules, unchanged
    monkeypatch.setenv(classification.FLAG_MODE_ENV, "flag")
    assert proof.proof_group(finding) == "not_compared"
    assert "short value" in str(proof.classified(finding).why)
    read = judged({**finding, "checks": {"printed_architect": "match", "printed_vendor": "match"}})
    assert proof.proof_group(read) == "right"


@pytest.mark.usefixtures("flag")
def test_the_same_number_on_an_unsure_pairing_is_no_problem() -> None:
    finding = _ai_only(status="check", check_reason="pairing_not_certain", vendor='36"',
                       architect='36"')  # fmt: skip
    assert classification.same_unsure(finding)
    assert proof.proof_group(finding) == "right"


# ---- unchanged ----------------------------------------------------------------------------------------


@pytest.mark.usefixtures("flag")
def test_the_reviewers_decisions_still_override() -> None:
    agreed = _ai_only(decision={"decision": "agree"})
    assert proof.proof_group(agreed) == "clouded"
    assert proof.checked_by(agreed) == "reviewer"
    dismissed = _ai_only(decision={"decision": "not_a_problem"})
    assert proof.proof_group(dismissed) == "dismissed"


@pytest.mark.usefixtures("flag")
def test_please_checks_never_make_the_check_incomplete() -> None:
    result = review_action.review_action([_look(), _ai_only()], CheckState())
    assert result.action != "not_for_sending"
    assert result.rule != "S0"


@pytest.mark.usefixtures("flag")
def test_the_rule_line_says_flag_mode() -> None:
    assert proof.rule_line() == proof.FLAG_RULE_LINE
    assert "false alarm" in proof.FLAG_RULE_LINE
