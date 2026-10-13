"""The cause a failed or unsent model call keeps, and what it must never keep (#1215).

Verification for `extraction.models.invocations.failure_reason` and `failure_kind_reason`. The
cause is stored in `model_invocations.rejection_reason`, which diagnostics print and group by, so it
is one short line in the code's own words: never a key, a token, a URL, quoted text (a prompt, a
model's answer or a drawing's value) or a second line of a long error.
"""

from __future__ import annotations

import unicodedata

import pytest

from extraction.models.invocations import (
    FAILURE_REASON_LIMIT,
    UNNAMED_FAILURE,
    failure_kind_reason,
    failure_reason,
    printable,
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


# --- review of #1218: characters the database or a terminal cannot take ------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "the stream broke\x00 here",
        "the stream broke \x1b[31mhere\x1b[0m",
        "the stream broke \u202ehere",
        "the stream broke\x07 here",
    ],
)
def test_control_and_format_characters_never_reach_the_column(message: str) -> None:
    """A NUL byte made the insert itself fail, losing the row; ESC and U+202E rewrite a terminal."""
    reason = failure_reason(RuntimeError(message))

    assert all(not unicodedata.category(c).startswith("C") for c in reason), repr(reason)
    assert reason.startswith("RuntimeError: the stream broke")


def test_the_cleaner_replaces_every_control_or_format_character_with_a_space() -> None:
    assert printable("a\x00b\x1bc\u202ed\te") == "a b c d e"


# --- review of #1218: secrets the first sanitiser let through ----------------------------------------

LETTERS_ONLY_KEY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ"


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        (f"key {LETTERS_ONLY_KEY} was refused", LETTERS_ONLY_KEY),
        ("key sk-madeupfakekey was refused", "sk-madeupfakekey"),
        ("key ghp_madeupfakekey was refused", "ghp_madeupfakekey"),
        ("key gho_madeupfakekey was refused", "gho_madeupfakekey"),
        ("key xoxb-madeup-fake was refused", "xoxb-madeup-fake"),
        ("login failed for pass hunter22", "hunter22"),
        ("login failed for pwd hunter22", "hunter22"),
        ("login failed for password hunter22", "hunter22"),
        ("login failed, password: hunter22", "hunter22"),
        ("could not reach madeupuser:hunter22@db.example.invalid", "hunter22"),
        ("could not parse the answer 'Made-up cabinet is 36 wide", "Made-up cabinet"),
        ('could not parse the answer "Made-up cabinet is 36 wide', "Made-up cabinet"),
    ],
)
def test_more_secret_shapes_are_not_kept(message: str, secret: str) -> None:
    reason = failure_reason(RuntimeError(message))

    assert secret not in reason, reason
    assert reason.startswith("RuntimeError: ")


def test_a_plain_long_word_without_a_secret_shape_is_kept() -> None:
    """Over-redaction has a cost too: an ordinary sentence survives."""
    reason = failure_reason(RuntimeError("the request passed validation but the model was busy"))

    assert reason == "RuntimeError: the request passed validation but the model was busy"


class ClientError(Exception):
    """Shaped like botocore's: the code is in `response['Error']['Code']`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(
            f"An error occurred ({code}) when calling the Converse operation: {message}"
        )
        self.response = {"Error": {"Code": code, "Message": message}}


def test_a_provider_error_keeps_only_its_class_and_code_never_its_message() -> None:
    error = ClientError("ValidationException", "The input 'Made-up cabinet 36 wide' is too long")

    assert failure_reason(error) == "ClientError: ValidationException"


def test_a_provider_error_with_an_odd_code_keeps_only_its_class() -> None:
    error = ClientError("not a code\x00", "anything")

    assert failure_reason(error) == "ClientError"
