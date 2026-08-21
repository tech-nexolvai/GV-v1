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

from dataclasses import dataclass
from uuid import UUID

__all__ = ["InvocationRecord"]


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
        extraction_run_id: the version-pinned extractor run this call belongs to. It is what makes a
            cost total attributable to a package at all.
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
        cost_micros: the cost of this call in millionths of a currency unit.
        latency_ms: wall-clock duration in whole milliseconds.
        outcome: how the call ended. The closed set is enforced by the database; see the module
            docstring for why it is not re-stated here.

    Raises:
        TypeError: if any count, cost or duration is not a plain `int`.
    """

    extraction_run_id: UUID
    model_id: str
    prompt_id: str
    template_id: str
    crop_artifact_id: UUID | None
    input_tokens: int
    output_tokens: int
    cost_micros: int
    latency_ms: int
    outcome: str
    node_invocation_key: str | None = None
    candidate_id: UUID | None = None

    def __post_init__(self) -> None:
        """Refuse a count or a cost that is not exactly an integer, before it can be stored."""

        _exact_count("input_tokens", self.input_tokens)
        _exact_count("output_tokens", self.output_tokens)
        _exact_count("cost_micros", self.cost_micros)
        _exact_count("latency_ms", self.latency_ms)
        if self.node_invocation_key is not None and (
            not self.node_invocation_key.startswith("sha256:")
            or len(self.node_invocation_key) != 71
        ):
            raise ValueError("node_invocation_key must be a sha256-prefixed digest or None")
