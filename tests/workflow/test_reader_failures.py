"""Reader failures the workflow names itself: out of credit is retryable, by a person (#1206).

Verification for `workflow/reader_failures.py`. No network; the errors are made here.
"""

from __future__ import annotations

from app.lifecycle.side_states import (
    CREDIT_TOO_LOW,
    OUT_OF_CREDIT,
    FailureClass,
    classify,
    plain_stop_reason,
)
from extraction.slot_reader.anthropic import AnthropicRequestError, ProviderAccountRefused
from workflow.reader_failures import known_failure


def test_out_of_credit_is_retryable_with_the_plain_message_and_no_engine_retry() -> None:
    known = known_failure(ProviderAccountRefused(api="OpenRouter Messages API"))

    assert known is not None
    assert known.failure_class is FailureClass.RETRYABLE
    assert known.reason == OUT_OF_CREDIT
    assert known.engine_retries is False
    # Left to the table, it would have been permanent: unclassified errors are (#212).
    assert classify(ProviderAccountRefused()) is FailureClass.PERMANENT


def test_it_is_found_under_an_error_that_wraps_it() -> None:
    try:
        try:
            raise ProviderAccountRefused()
        except ProviderAccountRefused as refused:
            raise RuntimeError("the reading stopped") from refused
    except RuntimeError as wrapped:
        known = known_failure(wrapped)

    assert known is not None and known.reason == OUT_OF_CREDIT


def test_every_other_failure_is_left_to_the_classification_table() -> None:
    assert known_failure(AnthropicRequestError(429, "TooManyRequestsException")) is None
    assert known_failure(AnthropicRequestError(400, "ProviderRequestRejected")) is None
    assert known_failure(ValueError("the rulebook is missing")) is None


def test_in_flight_refusals_that_kept_coming_back_say_the_credit_is_too_low() -> None:
    known = known_failure(ProviderAccountRefused(credit_too_low=True))

    assert known is not None and known.reason == CREDIT_TOO_LOW
    assert known.reason == (
        "The AI provider account's credit is too low for this reading. Top it up, then run the "
        "reading again."
    )


def test_the_outbox_can_tell_a_failure_only_a_person_can_fix_without_the_reader() -> None:
    """`app.lifecycle` may not import the reader (#208): it asks the error itself."""
    assert plain_stop_reason(ProviderAccountRefused()) == OUT_OF_CREDIT
    assert plain_stop_reason(ProviderAccountRefused(credit_too_low=True)) == CREDIT_TOO_LOW
    assert plain_stop_reason(TimeoutError("synthetic")) is None
