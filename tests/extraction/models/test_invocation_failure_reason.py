"""The cause a failed or unsent model call keeps, and what it must never keep (#1215).

Verification for `extraction.models.invocations.failure_reason` and `failure_kind_reason`. The
cause is stored in `model_invocations.rejection_reason`, which diagnostics print and group by, so it
is one short line in the code's own words: never a key, a token, a URL, quoted text (a prompt, a
model's answer or a drawing's value) or a second line of a long error.
"""

from __future__ import annotations

import pytest

from extraction.models.invocations import (
    FAILURE_REASON_LIMIT,
    UNNAMED_FAILURE,
    failure_kind_reason,
    failure_reason,
)

#: Made-up credentials in the shapes a provider error could echo. None is real.
FAKE_ACCESS_KEY = "AKIAEXAMPLEFAKE12345"
FAKE_SECRET = "wJalrXUtnFEMI0K7MDENG0bPxRfiCYEXAMPLEKEY"


class ReadTimeoutError(Exception):
    """Named like botocore's, so the class name is what a real one would store."""


def test_the_class_and_a_plain_message_are_kept() -> None:
    error = ReadTimeoutError("Read timeout on endpoint after 30 seconds")

    assert failure_reason(error) == "ReadTimeoutError: Read timeout on endpoint after 30 seconds"


def test_a_message_less_error_keeps_its_class() -> None:
    assert failure_reason(TimeoutError()) == "TimeoutError"


@pytest.mark.parametrize(
    "message",
    [
        f"credentials {FAKE_ACCESS_KEY} were refused",
        f"signature mismatch: aws_secret_access_key={FAKE_SECRET}",
        f"Authorization: Bearer {FAKE_SECRET}",
        f"request failed at https://user:{FAKE_SECRET}@example.invalid/model/converse",
        f"x-amz-security-token={FAKE_SECRET} expired",
        f"token {FAKE_SECRET} is not valid",
    ],
)
def test_no_secret_looking_value_is_kept(message: str) -> None:
    reason = failure_reason(RuntimeError(message))

    assert reason.startswith("RuntimeError: ")
    assert FAKE_ACCESS_KEY not in reason
    assert FAKE_SECRET not in reason
    assert "example.invalid" not in reason
    assert "user:" not in reason


def test_quoted_text_is_not_kept() -> None:
    """A prompt, a model's answer or a drawing value arrives quoted in an error; it is dropped."""
    error = ValueError("could not parse the answer 'Made-up cabinet B-17 is 36 wide' at char 4")

    reason = failure_reason(error)

    assert "Made-up cabinet" not in reason
    assert reason.startswith("ValueError: could not parse the answer")


def test_only_the_first_line_is_kept_and_it_is_bounded() -> None:
    error = RuntimeError("first line\nsecond line holding the prompt text\n" + "x" * 1000)
    long_error = RuntimeError("word " * 500)

    assert failure_reason(error) == "RuntimeError: first line"
    bounded = failure_reason(long_error)
    assert len(bounded) <= FAILURE_REASON_LIMIT
    assert "\n" not in bounded


def test_a_known_failure_kind_is_kept_as_it_is() -> None:
    assert failure_kind_reason("PictureWouldBeResized") == "PictureWouldBeResized"
    assert failure_kind_reason("SpendCapExceeded") == "SpendCapExceeded"


@pytest.mark.parametrize("kind", ["", "two words", "x" * 101, f"Bearer {FAKE_SECRET}\n"])
def test_a_failure_kind_that_is_not_a_class_name_is_not_stored_as_written(kind: str) -> None:
    assert failure_kind_reason(kind) == UNNAMED_FAILURE
