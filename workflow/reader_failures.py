"""Reader failures the workflow names itself, because `app/lifecycle` may not import `extraction/`.

**Out of credit (#1206, #1200).** When the AI provider refuses the account for payment (HTTP 402),
the reader raises `ProviderAccountRefused`. Unclassified, it was a permanent failure: the package
went to `FAILED_PERMANENT` and the screen said "Processing failed … will not retry", with nothing
telling anyone to top the account up. It is not permanent: once the account is topped up, the same
reading succeeds, and the answers already paid for are reused (#1112). So the package goes to
`FAILED_RETRYABLE` with a reason a person can act on (`OUT_OF_CREDIT`, or `CREDIT_TOO_LOW` when
the in-flight budget still refused after its bounded retries), which the package and Usage screens
show word for word, and a person runs the reading again from the package screen.

**Not retried by the engine.** Retrying on a timer cannot top the account up; each retry would read
every page again for nothing. A person retries once the account is topped up.

`app/lifecycle/side_states.py` decides by exception type and must not import `extraction/` (#208's
guard), so the class and the reason are passed to it from here (`enter_failure(failure_class=...)`).

Verification: `tests/workflow/test_reader_failures.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.lifecycle.side_states import CREDIT_TOO_LOW, OUT_OF_CREDIT, FailureClass

__all__ = ["KnownFailure", "known_failure"]

#: How far down an exception's causes to look: a reader error wrapped by a caller is still itself.
_CAUSE_DEPTH = 8


@dataclass(frozen=True, slots=True)
class KnownFailure:
    """A failure the workflow classifies itself: retry or not, and the reason in plain words."""

    failure_class: FailureClass
    reason: str
    engine_retries: bool
    """Whether the engine should try again on its own; `False` when only a person can fix it."""


def known_failure(error: BaseException) -> KnownFailure | None:
    """The failure's own class and reason, or `None` for the classification table to decide."""
    # Imported here, not at module scope: `workflow/review.py` is imported by tests that never read
    # a drawing, and the reader's module is only needed once a reading has failed.
    from extraction.slot_reader.anthropic import ProviderAccountRefused

    seen: BaseException | None = error
    for _ in range(_CAUSE_DEPTH):
        if seen is None:
            break
        if isinstance(seen, ProviderAccountRefused):
            return KnownFailure(
                failure_class=FailureClass.RETRYABLE,
                reason=CREDIT_TOO_LOW if seen.credit_too_low else OUT_OF_CREDIT,
                engine_retries=False,
            )
        seen = seen.__cause__ or seen.__context__
    return None
