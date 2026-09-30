"""Distribution API for Q8/Q9/Q21: the reviewer classifies, the route computes.

**This file used to pin the opposite behaviour.** Until #678 the endpoint asked the reviewer to
pick one adjustable cabinet and gave it the whole remainder — `test_larger_difference_distributes_
to_reviewer_chosen_cabinet` asserted `['30"', '32"']` — which was the correct reading of Q9 until
slide 11 of the 2026-09-21 deck. It is not a rounding difference: on the client's own worked
example it returned `18" / 36" / 24"` and called it a PASS, where Raj's answer is `21" / 36" / 21"`.

So the first two tests below are the two worked examples, end to end through the HTTP route.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.main import API_PREFIX, create_app

PROJECT = uuid4()
PATH = f"{API_PREFIX}/projects/{PROJECT}/filler-distribution"

#: Wide enough that no test below is decided by a cabinet bound unless it sets its own. These are
#: not defaults — CLIENT_FACTS Q21 says the real values are unsettled, and the route has none.
WIDE_BOUNDS: dict[str, str] = {
    "single_door_cab_width_min": '9"',
    "single_door_cab_width_max": '48"',
    "double_door_cab_width_min": '9"',
    "double_door_cab_width_max": '48"',
    "drawer_cab_width_min": '9"',
    "drawer_cab_width_max": '48"',
}


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        environment="test",
    )


def _client() -> TestClient:
    app = create_app(_settings())
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({PROJECT}),
    )
    return TestClient(app, raise_server_exceptions=False)


def _payload(**updates: Any) -> dict[str, Any]:
    """Raj's layout: filler | CAB_REGULAR | CAB_EQUIP | CAB_REGULAR | filler."""
    payload: dict[str, Any] = {
        "assembly": {
            "cabinets": [
                {"id": "CAB-1", "width": '24"', "type": "double_door"},
                {"id": "CAB-EQUIP", "width": '36"', "type": "equipment"},
                {"id": "CAB-2", "width": '24"', "type": "double_door"},
            ],
            "fillers": [
                {"id": "F-L", "width": '3"'},
                {"id": "F-R", "width": '3"'},
            ],
        },
        "field_width": '82"',
        "filler_min": '2"',
        "filler_max": '3"',
        **WIDE_BOUNDS,
    }
    payload.update(updates)
    return payload


def _widths(body: dict[str, Any], part: str) -> list[str]:
    return [item["proposed"]["display"] for item in body[part]]


def test_the_first_worked_example_reaches_the_reviewer_intact() -> None:
    """Slide 4: 90" to 82". Fillers give 2", the two regulars take 3" each, equipment holds."""
    response = _client().post(PATH, json=_payload())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "PASS"
    assert body["condition"] == "cabinets_absorb_remainder"
    assert _widths(body, "fillers") == ['2"', '2"']
    assert _widths(body, "cabinets") == ['21"', '36"', '21"']
    assert body["cabinets_retained"] is False


def test_the_second_worked_example_grows_the_run_the_same_way() -> None:
    """Slide 8: 88" to 96". The mirror, and the equipment cabinet holds in this direction too."""
    body = (
        _client()
        .post(
            PATH,
            json=_payload(
                field_width='96"',
                assembly={
                    "cabinets": [
                        {"id": "CAB-1", "width": '24"', "type": "double_door"},
                        {"id": "CAB-EQUIP", "width": '36"', "type": "equipment"},
                        {"id": "CAB-2", "width": '24"', "type": "double_door"},
                    ],
                    "fillers": [
                        {"id": "F-L", "width": '2"'},
                        {"id": "F-R", "width": '2"'},
                    ],
                },
            ),
        )
        .json()
    )

    assert body["outcome"] == "PASS"
    assert _widths(body, "fillers") == ['3"', '3"']
    assert _widths(body, "cabinets") == ['27"', '36"', '27"']


def test_no_reviewer_input_can_put_the_whole_remainder_on_one_cabinet() -> None:
    """The defect #678 fixes, stated as a property rather than a single case.

    `adjustable_cabinet_id` is gone and the request forbids unknown fields, so the old call is
    refused outright rather than quietly ignored — a silently dropped field would leave a caller
    believing it had chosen a cabinet.
    """
    response = _client().post(PATH, json=_payload(adjustable_cabinet_id="CAB-1"))

    assert response.status_code == 422, response.text


def test_every_regular_cabinet_moves_and_the_equipment_cabinet_never_does() -> None:
    """`adjustable` no longer means "the one the reviewer picked" — it means "may change"."""
    body = _client().post(PATH, json=_payload()).json()

    assert [cabinet["adjustable"] for cabinet in body["cabinets"]] == [True, False, True]
    assert [cabinet["type"] for cabinet in body["cabinets"]] == [
        "double_door",
        "equipment",
        "double_door",
    ]
    assert body["cabinets"][1]["proposed"]["display"] == '36"'
    assert body["cabinets"][1]["original"]["display"] == '36"'


def test_fillers_absorbing_it_all_leaves_the_cabinets_green() -> None:
    """Slide 12, outcome 3: "mark the cabinets with green checks and change only the fillers"."""
    body = _client().post(PATH, json=_payload(field_width='88"')).json()

    assert body["outcome"] == "PASS"
    assert body["condition"] == "fillers_absorb"
    assert _widths(body, "fillers") == ['2"', '2"']
    assert _widths(body, "cabinets") == ['24"', '36"', '24"']
    assert body["cabinets_retained"] is True


def test_a_site_that_matches_the_drawing_needs_no_change() -> None:
    """Slide 12, outcome 5."""
    body = _client().post(PATH, json=_payload(field_width='90"')).json()

    assert body["outcome"] == "PASS"
    assert body["condition"] == "no_change_required"
    assert _widths(body, "cabinets") == ['24"', '36"', '24"']


def test_a_bound_a_cabinet_cannot_meet_asks_for_an_rfi() -> None:
    """Slide 12, outcome 4: "should not force a fix… 'cannot be resolved, RFI to architect'"."""
    body = _client().post(PATH, json=_payload(double_door_cab_width_min='23"')).json()

    assert body["outcome"] == "REVIEW_REQUIRED"
    assert body["condition"] == "cannot_be_resolved"
    assert "RFI" in body["reviewer_action"]
    # The proposal is the untouched run: an abstention proposes nothing.
    assert _widths(body, "cabinets") == ['24"', '36"', '24"']
    assert body["cabinets_retained"] is False


def test_a_split_that_lands_on_an_undrawable_width_is_not_a_pass() -> None:
    """4" over three cabinets is 22 2/3", which is not a width anyone draws or cuts."""
    body = (
        _client()
        .post(
            PATH,
            json=_payload(
                field_width='108"',
                assembly={
                    "cabinets": [
                        {"id": "CAB-1", "width": '24"', "type": "drawer"},
                        {"id": "CAB-2", "width": '24"', "type": "drawer"},
                        {"id": "CAB-3", "width": '24"', "type": "drawer"},
                        {"id": "CAB-EQUIP", "width": '36"', "type": "equipment"},
                    ],
                    "fillers": [
                        {"id": "F-L", "width": '3"'},
                        {"id": "F-R", "width": '3"'},
                    ],
                },
            ),
        )
        .json()
    )

    assert body["outcome"] == "REVIEW_REQUIRED"
    assert body["condition"] == "share_does_not_divide"
    assert "apportioned" in body["reviewer_action"]


def test_a_run_that_is_not_two_fillers_is_accepted() -> None:
    """Slide 12 names a wall on only one side, so an arity of two would refuse a real layout."""
    body = (
        _client()
        .post(
            PATH,
            json=_payload(
                field_width='64"',
                filler_min='1"',
                filler_max='4"',
                assembly={
                    "cabinets": [
                        {"id": "CAB-1", "width": '24"', "type": "single_door"},
                        {"id": "CAB-EQUIP", "width": '36"', "type": "equipment"},
                    ],
                    "fillers": [{"id": "F-L", "width": '2"'}],
                },
            ),
        )
        .json()
    )

    # 2 + 24 + 36 = 62 on the drawing; the site is 2" wider and the single filler takes all of it.
    assert body["outcome"] == "PASS"
    assert body["condition"] == "fillers_absorb"
    assert _widths(body, "fillers") == ['4"']
    assert len(body["fillers"]) == 1
    assert _widths(body, "cabinets") == ['24"', '36"']


def test_a_missing_type_bound_is_refused_rather_than_defaulted() -> None:
    """CLIENT_FACTS Q21: no bound in this route has a default, so an absent one cannot be filled.

    A 422 here is the API shape of "NOT FOUND, and not zero": the request is refused rather than a
    proposal being computed against a number nobody approved. The envelope names the shape and not
    the field, because `test_a_validation_failure_does_not_echo_the_submitted_value` requires that
    validation failures do not echo what was sent — so this asserts the refusal, not its wording.
    """
    for bound in (
        "drawer_cab_width_min",
        "single_door_cab_width_max",
        "double_door_cab_width_min",
        "filler_min",
    ):
        payload = _payload()
        del payload[bound]

        response = _client().post(PATH, json=payload)

        assert response.status_code == 422, f"{bound} was accepted: {response.text}"


def test_a_cabinet_without_a_classification_is_refused() -> None:
    """Slide 11 puts the classification with the reviewer; nothing here infers one."""
    payload = _payload()
    del payload["assembly"]["cabinets"][0]["type"]

    assert _client().post(PATH, json=payload).status_code == 422


def test_a_category_the_deck_does_not_name_is_refused() -> None:
    """Closed set. An unrecognised word must never fall through to "regular" and be resized."""
    payload = _payload()
    payload["assembly"]["cabinets"][1]["type"] = "appliance"

    assert _client().post(PATH, json=payload).status_code == 422


def test_contradictory_bounds_are_the_callers_mistake_not_a_server_fault() -> None:
    """The operation raises `RuleAuthoringError`; the route must not let that become a 500."""
    response = _client().post(
        PATH, json=_payload(double_door_cab_width_min='40"', double_door_cab_width_max='20"')
    )

    assert response.status_code == 422, response.text
    assert "double_door_cab_width_min" in response.text


def test_missing_field_dimension_returns_not_found_with_user_input_source() -> None:
    response = _client().post(PATH, json=_payload(field_width=None))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "NOT_FOUND"
    assert body["condition"] == "field_width_missing"
    assert body["cabinets_retained"] is False
    assert body["field_dimension"] == {
        "name": "field_width",
        "source": "USER_INPUT",
        "status": "HUMAN_CONFIRMED",
        "value": None,
    }


def test_duplicate_cabinet_ids_are_refused() -> None:
    payload = _payload()
    payload["assembly"]["cabinets"][2]["id"] = "CAB-1"

    assert _client().post(PATH, json=payload).status_code == 422


def test_every_condition_the_operation_can_return_has_a_reviewer_message() -> None:
    """The route raises rather than showing a reviewer a bare condition string.

    A condition added to the operation without a message here would otherwise reach the screen as
    machine vocabulary, so this walks the enum instead of waiting for one to slip through.
    """
    from app.api.distribution import _MESSAGES
    from verdict.operations.distribution import DistributionCondition

    for condition in DistributionCondition:
        if condition is DistributionCondition.CABINET_SELECTION_REQUIRED:
            # Belongs to `filler_distribution`, which this route no longer calls.
            continue
        assert condition.value in _MESSAGES, condition.value


def test_a_run_the_operation_cannot_compare_is_a_review_not_a_server_error() -> None:
    """#673's abstention carries no site difference, and the route must not read one anyway.

    The request schema makes this hard to reach — cabinet types are a closed `Literal`, and the
    route passes one cabinet run as both the design and the proposal, so the lengths always agree.
    It is pinned because "hard to reach" is not "unreachable", and the failure mode would be a 500
    on an operation that abstained politely.
    """
    from app.api.distribution import _MESSAGES
    from verdict.operations.distribution import (
        DistributionCondition,
        UnsupportedRunShape,
        _unsupported_shape,
    )

    refused = _unsupported_shape(
        UnsupportedRunShape("design_fillers is empty", "this check needs at least one value")
    )
    facts = dict(refused.intermediates)

    assert facts["condition"] == DistributionCondition.RUN_SHAPE_UNSUPPORTED.value
    assert "site_difference" not in facts
    assert facts["condition"] in _MESSAGES


def test_the_reviewer_is_told_how_the_drawing_is_being_corrected() -> None:
    """Slides 5 and 9 ask for this by name, and say why.

    *"It would be better if the program identifies the variables first and provides the logic as
    below. This explanation will help the shop drawing reviewer to understand how the program is
    correcting the drawing."* Before #682 the response carried a fixed sentence per condition and
    the operation's own trace — true, but not an explanation.
    """
    body = _client().post(PATH, json=_payload()).json()

    said = body["message"]
    for figure in ('90"', '82"', '8"', '3"', '2"', '6"', '36"', '24"', '21"'):
        assert figure in said, f"{figure} is missing from the explanation: {said}"
    # The short label survives alongside it, for a list of findings.
    assert body["summary"] == (
        "The fillers reached their limit, so the rest is divided equally between the regular "
        "cabinets. The equipment cabinets keep their width."
    )


def test_the_explanation_never_shows_a_decimal() -> None:
    """A drawing writes `1 1/2"`. A reviewer comparing the two by eye must read the same thing."""
    import re

    body = (
        _client()
        .post(
            PATH,
            json=_payload(
                field_width='89"',
                filler_min='1"',
                filler_max='2"',
                assembly={
                    "cabinets": [
                        {"id": "CAB-1", "width": '24"', "type": "double_door"},
                        {"id": "CAB-EQUIP", "width": '36"', "type": "equipment"},
                        {"id": "CAB-2", "width": '24"', "type": "double_door"},
                    ],
                    "fillers": [
                        {"id": "F-L", "width": '2"'},
                        {"id": "F-R", "width": '2"'},
                    ],
                },
            ),
        )
        .json()
    )

    # The fillers are already at their 2" maximum, so the 1" falls to the two regular cabinets:
    # half an inch each, and 24 1/2" is how a drawing writes it.
    assert '24 1/2"' in body["message"]
    assert not re.search(r"\d\.\d", body["message"]), body["message"]
