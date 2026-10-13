"""What must be recorded about every model call, and the one rule the database cannot enforce.

Two questions have to stay answerable long after a package has shipped: *what did this cost, and
why*, and *what did the model actually see when it produced this reading*. This module owns the shape
of the answer — the complete field set for one call, validated at construction — and nothing else.
Turning it into a row lives in `app/runs/invocations.py`.

**Failures are the point of the record rather than a detail of it.** A call that timed out, was
refused, or produced output the validator rejected still consumed tokens, still took wall-clock time,
and still explains a gap in the extracted evidence. Recording only the successes would turn the table
into a summary of the work that went well, which is the one shape a cost record must never have: F5's
per-package ceiling would then be computed from an under-count, and it would fail to trigger
precisely on the packages that burned the most money going nowhere. `InvocationRecord` therefore
requires the same complete field set whatever the outcome — a refusal states its zero output tokens
rather than omitting them.

**Cost is an integer number of micros, and this is the only layer that can say so.** `cost_micros` is
a PostgreSQL `integer`; handing the driver `137.6` stores `138`, and handing it `Decimal("137.4")`
stores `137`. No error, no warning, and the number a ceiling later reads is not the number anybody
computed. The database cannot catch this — by the time the value reaches it, the rounding has already
happened and looks like an honest integer. So the type is checked here, at construction, where the
caller can still be told which field was wrong.
`tests/extraction/models/test_invocations.py::test_the_column_alone_would_round_a_float_cost`
demonstrates the rounding against a real database rather than taking it on trust.

**Why there is no database in this file.** `docs/DESIGN_AI.md` §2 gives `extraction/models/` an
explicit import table: it may import `evidence/` and `storage/`, and must never import `rules/` or
`verdict/` — and the standard it sets is *reachability*, not direct imports ("the prohibited
capabilities are absent from the agent's reachable surface, not refused at call time"). An earlier
version of this module imported `app.models` for the ORM class, which made `rules/` reachable in two
hops through `app.models.evidence -> rules.semantic_types`. Passing the ORM class in as a parameter
would have removed the import edge while leaving this file as the persistence layer with its types
erased, which satisfies a guard without satisfying the intent. Splitting it along the real seam does
both: what must be recorded and what makes the record trustworthy is extraction's own knowledge about
its own paid calls, and it needs no database to be correct. This module imports nothing outside the
standard library, so the question cannot come back.

**What this file deliberately does not check.** Blank identifiers, negative counts and unknown
outcomes are all refused by the database, and re-checking them here would create two copies of one
rule — with the copy that drifted being the one nobody was watching. The integer-type check is here
because it is the one thing the database genuinely cannot see.

**`outcome` is a plain `str`, and that is a compromise worth naming.** The closed set of outcomes
lives in `ModelInvocationOutcome` in `app/models/runs.py`, which generates the `CHECK` constraint that
enforces it. This module cannot import it without reaching `app/` again, and defining a second enum
here would be exactly the drift the paragraph above refuses. So the set stays in one place and this
field is typed loosely; a caller inside `app/` should still pass the enum member. The real fix is to
move that enum into `vocabulary/`, which every package may name — that is a change to a shipped C1.4
module and outside this story, and it is noted on issue #251 rather than done here.

Source: backend proposal §6.3, §10.1 `model_invocations`, and issue #251 ·
Design: `docs/DESIGN_AI.md` §4.5 · Verification: `tests/extraction/models/test_invocations.py`
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final
from uuid import UUID

from extraction.models.context import AssembledContext

__all__ = [
    "FAILURE_REASON_LIMIT",
    "UNNAMED_FAILURE",
    "InvocationRecord",
    "failure_kind_reason",
    "failure_reason",
    "printable",
]

#: The longest cause stored for a call that failed outright (#1215): one short line, never a dump.
FAILURE_REASON_LIMIT: Final = 200

#: The cause stored when the code named a failure in a way that is not a class name. Never the text.
UNNAMED_FAILURE: Final = "unnamed_failure"

_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
#: `user:password@host` written without a scheme.
_USERINFO = re.compile(r"[^\s:@/]+:[^\s@/]+@\S+")
#: An apostrophe between two letters (`didn't`, `model's`) is not a quote: made U+2019 first, so it
#: cannot pair with a real opening quote and leave the quoted text between them in place.
_WORD_APOSTROPHE = re.compile(r"(?<=[A-Za-z])'(?=[A-Za-z])")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
#: A quote left open after the closed ones are gone: everything from it to the end of the line.
_UNCLOSED_QUOTE = re.compile(r"['\"`].*$")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+\S+")
_SECRET_PAIR = re.compile(
    r"(?i)\b([\w-]*(?:secret|token|password|passwd|signature|credential|api[_-]?key|access[_-]?key"
    r"|authorization)[\w-]*)\s*[=:]\s*\S+"
)
#: A compound secret name (`session-token`, `client_secret`) followed by a space and its value.
_COMPOUND_SECRET_WORD = re.compile(
    r"(?i)\b((?=[\w-]*[-_])[\w-]*(?:secret|token|password|passwd|credential|api[_-]?key"
    r"|access[_-]?key)[\w-]*)\s+\S+"
)
#: The bare names a password is written after, with a space allowed in place of `=` or `:`.
_PASSWORD_WORD = re.compile(r"(?i)\b(pass|pwd|password|passwd)(?:\s*[=:]\s*|\s+)\S+")
#: Well-known key prefixes: OpenAI-style `sk-`, GitHub `ghp_`/`gho_`, Slack `xox`.
_KEY_PREFIX = re.compile(r"\b(?:sk-|ghp_|gho_|xox)[A-Za-z0-9_-]*")
_ACCESS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|AIDA|AROA)[A-Z0-9]{12,}\b")
#: A long run of key-like characters that contains a digit: a key, a token, a hash, a request id.
_TOKEN_LIKE = re.compile(r"(?=[A-Za-z0-9+/_=-]*[0-9])[A-Za-z0-9+/_=-]{20,}")
#: A very long key-like run, digits or not: no ordinary word is this long.
_LONG_RUN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_SPACES = re.compile(r"\s+")


def printable(text: str) -> str:
    """`text` with every control or format character (Unicode category `C*`) made a space.

    PostgreSQL refuses a NUL in a text column, so one NUL in an error message failed the insert
    and lost the row it belonged to; ESC sequences and U+202E rewrite what a terminal shows.
    """
    return "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)


def failure_kind_reason(kind: str | None) -> str:
    """The stored cause of a reader attempt that failed or was never sent (#1215).

    The readers name a failure by its exception's class name (`PictureWouldBeResized`,
    `SpendCapExceeded`, `ThrottlingException`), which is code-authored and safe to keep as it is.
    Anything else is not stored as written: `unnamed_failure` says a cause existed without copying
    text nobody vetted.
    """
    if kind is not None and _CLASS_NAME.fullmatch(kind):
        return kind
    return UNNAMED_FAILURE


def _provider_code(error: BaseException) -> str | None:
    """The error code of a botocore-shaped error (`response['Error']['Code']`), when it has one."""
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    details = response.get("Error")
    if not isinstance(details, Mapping):
        return None
    code = details.get("Code")
    return code if isinstance(code, str) else ""


def failure_reason(error: BaseException) -> str:
    """The stored cause of a call that failed outright: its class and a safe, short message (#1215).

    A provider error (botocore `ClientError` shape) keeps only its class and error code, never its
    message, which can repeat the request. For any other error the message is cut to its first
    line, control and format characters become spaces, and anything that could carry a secret or a
    client value is replaced: URLs and `user:password@host`, quoted text (a prompt, a model's answer
    or a drawing value is quoted when an error repeats it; an apostrophe inside a word is not a
    quote; an unclosed quote takes the rest of the line), bearer and `name=value` credentials, a password after `pass`/`pwd`/`password`, known key
    prefixes, access key ids and long key-like runs. The whole is one line of at most
    `FAILURE_REASON_LIMIT` characters.
    """
    name = type(error).__name__
    code = _provider_code(error)
    if code is not None:
        return f"{name}: {code}" if _CLASS_NAME.fullmatch(code) else name
    try:
        message = str(error)
    except Exception:  # noqa: BLE001 - a broken __str__ must not lose the class name too
        message = ""
    first = printable(next((line for line in message.splitlines() if line.strip()), ""))
    for pattern, replacement in (
        (_URL, "<url>"),
        (_USERINFO, "<redacted>"),
        (_WORD_APOSTROPHE, "\u2019"),
        (_QUOTED, "<quoted>"),
        (_UNCLOSED_QUOTE, "<quoted>"),
        (_BEARER, r"\1 <redacted>"),
        (_SECRET_PAIR, r"\1=<redacted>"),
        (_COMPOUND_SECRET_WORD, r"\1=<redacted>"),
        (_PASSWORD_WORD, r"\1=<redacted>"),
        (_KEY_PREFIX, "<redacted>"),
        (_ACCESS_KEY_ID, "<redacted>"),
        (_TOKEN_LIKE, "<redacted>"),
        (_LONG_RUN, "<redacted>"),
    ):
        first = pattern.sub(replacement, first)
    first = _SPACES.sub(" ", first).strip()
    reason = f"{name}: {first}" if first else name
    if len(reason) > FAILURE_REASON_LIMIT:
        reason = reason[: FAILURE_REASON_LIMIT - 3].rstrip() + "..."
    return reason


def _exact_count(name: str, value: int) -> None:
    """Raise unless `value` is a plain integer, saying which field was wrong and why.

    `bool` is excluded deliberately even though it is a subclass of `int`: `True` reaching a token
    count means a caller passed a flag where a number belongs, and storing `1` would hide it.

    `float` and `Decimal` are excluded because the destination column is a PostgreSQL `integer` and
    the driver rounds silently on the way in. A caller holding a fractional cost has done
    floating-point arithmetic on money somewhere upstream, and the place to find that out is here
    rather than in a ceiling that fails to fire six weeks later.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{name} must be a plain int, not {type(value).__name__}. "
            "This is stored in a PostgreSQL integer column: a float or Decimal is rounded on insert "
            "with no error, so a cost that was never exact would be recorded as though it were."
        )


@dataclass(frozen=True, slots=True)
class InvocationRecord:
    """One model call, complete and validated, whether or not it produced anything.

    Frozen, because this is a statement about something that already happened. Build one on every
    path out of the model client — including the ones that raise — and hand it to
    `app.runs.invocations.record`. There is no way to persist a call without building one of these
    first, so the integer check below is not skippable.

    Attributes:
        extraction_run_id: the version-pinned extractor run this call belongs to, or `None` for a
            call that is not an extraction. Exactly one of this and `package_revision_id` is set.
        package_revision_id: the package revision a review-time call belongs to — reviewer chat and
            findings narration, which have no extraction run. ADR-0019. Before it existed, those
            calls were anchored to the package's *latest* extraction run, which attributed a
            narration to work that did not make it and raised outright on a package that had never
            been extracted (#694).
        model_id: the exact model identifier, version included. "Which model said this" is not
            answerable from a family name once the family has moved on.
        prompt_id: the identifier of the prompt this call used.
        template_id: the identifier of the template the prompt was rendered from.
        crop_artifact_id: the `evidence_artifacts` row for the image the model was given, or `None`
            when there was no crop to reference. There is no default: "there was no crop" is
            something a caller states rather than something it forgets. See
            `app/runs/invocations.py` for what the `None` case costs you.
        input_tokens: tokens sent, as counted by the provider.
        output_tokens: tokens returned, as counted by the provider. Zero is the normal value for a
            refusal or a timeout, and is stated as zero rather than left out.
        cost_micros: the cost of this call in millionths of a US dollar, or `None` when no price
            was stated for the model (#700). Never a stand-in zero: `0` means no tokens were used.
        latency_ms: wall-clock duration in whole milliseconds.
        outcome: how the call ended. The closed set is enforced by the database; see the module
            docstring for why it is not re-stated here.
        rejection_reason: for a rejected response, the code-authored reason it was rejected. Locally
            known validation failures use their bounded reason code; callers that only know an
            exception may pass a bounded diagnostic string. Since #1215 a call that failed, timed
            out, was refused or was never sent may name its cause here too (`failure_kind_reason`,
            `failure_reason`); only a successful call may not.

    Raises:
        TypeError: if any count, cost or duration is not a plain `int`.
    """

    extraction_run_id: UUID | None
    model_id: str
    prompt_id: str
    template_id: str
    crop_artifact_id: UUID | None
    input_tokens: int
    output_tokens: int
    cost_micros: int | None
    latency_ms: int
    outcome: str
    rejection_reason: str | None = None
    package_revision_id: UUID | None = None
    node_invocation_key: str | None = None
    candidate_id: UUID | None = None
    assembled_context: AssembledContext | None = None
    bound_pt: Decimal | None = None
    private_raw_response: str | None = field(default=None, repr=False)
    reader_page_index: int | None = None
    reader_attempt_number: int | None = None
    reader_question_packet: Mapping[str, object] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Refuse a count or a cost that is not exactly an integer, before it can be stored."""

        # **Exactly one origin.** The database enforces it too, and it is enforced here as well so
        # a call with no origin cannot be *built* — the same discipline as the integer checks below.
        # Neither means the row is attributable to nothing; both means two different things claim to
        # have made one call.
        if (self.extraction_run_id is None) == (self.package_revision_id is None):
            raise ValueError(
                "exactly one of extraction_run_id and package_revision_id must be set: an "
                "extraction call belongs to a run, a review-time call to a package revision, and a "
                "call belonging to neither cannot be attributed to anything (ADR-0019)"
            )
        _exact_count("input_tokens", self.input_tokens)
        _exact_count("output_tokens", self.output_tokens)
        if self.cost_micros is not None:
            _exact_count("cost_micros", self.cost_micros)
        _exact_count("latency_ms", self.latency_ms)
        if self.outcome == "rejected" and self.rejection_reason is None:
            raise ValueError("a rejected invocation must record why the local code rejected it")
        if self.rejection_reason is not None:
            if not isinstance(self.rejection_reason, str) or not self.rejection_reason.strip():
                raise ValueError("rejection_reason must be a non-empty string or None")
            if len(self.rejection_reason) > 500:
                raise ValueError("rejection_reason must be 500 characters or fewer")
            if self.outcome == "ok":
                raise ValueError(
                    "rejection_reason is not valid for a successful invocation: it names why a "
                    "call was rejected, failed or was never sent (#1215)"
                )
        if self.node_invocation_key is not None and (
            not self.node_invocation_key.startswith("sha256:")
            or len(self.node_invocation_key) != 71
        ):
            raise ValueError("node_invocation_key must be a sha256-prefixed digest or None")
        if (self.assembled_context is None) != (self.bound_pt is None):
            raise ValueError("assembled_context and bound_pt must be recorded together")
        if self.bound_pt is not None and (
            not isinstance(self.bound_pt, Decimal)
            or not self.bound_pt.is_finite()
            or self.bound_pt < 0
        ):
            raise ValueError("bound_pt must be a finite, non-negative Decimal")
        if self.private_raw_response is not None and not isinstance(self.private_raw_response, str):
            raise TypeError("private_raw_response must be a string or None")
        if self.reader_question_packet is not None and not isinstance(
            self.reader_question_packet, Mapping
        ):
            raise TypeError("reader_question_packet must be a JSON-safe mapping or None")
        if (self.reader_page_index is None) != (self.reader_attempt_number is None):
            raise ValueError(
                "reader_page_index and reader_attempt_number must be recorded together"
            )
        if self.reader_page_index is not None and (
            isinstance(self.reader_page_index, bool)
            or not isinstance(self.reader_page_index, int)
            or self.reader_page_index < 0
        ):
            raise ValueError("reader_page_index must be a non-negative integer or None")
        if self.reader_attempt_number is not None and (
            isinstance(self.reader_attempt_number, bool)
            or not isinstance(self.reader_attempt_number, int)
            or self.reader_attempt_number < 1
        ):
            raise ValueError("reader_attempt_number must be a positive integer or None")
