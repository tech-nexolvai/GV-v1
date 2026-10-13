"""Reader failures the workflow names itself: out of credit is retryable, by a person (#1206).

Verification for `workflow/reader_failures.py`. No network; the errors are made here.
"""

from __future__ import annotations

from app.lifecycle.side_states import OUT_OF_CREDIT, FailureClass, classify
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
