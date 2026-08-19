"""Task identity, and the database constraint that makes a duplicate delivery harmless.

A LangGraph interrupt restarts the node it interrupted, and Hatchet delivers at least once. Both
mean the same task can arrive twice. `AGENTS.md` §6 and `docs/DESIGN_PLATFORM.md` §6.2 fix the
identity of a task so the second arrival can be recognised as the same work:

    document_version_id + page/region + task_type + extractor_version + config_hash

`idempotency_key()` computes that identity. `claim()` writes it to `task_runs.idempotency_key` and
turns the second arrival into a no-op that returns the first arrival's row.

**Which layer guarantees what.** This is the part that is easy to over-claim, so it is stated
before anything else.

* The **database** guarantees that at most one `task_runs` row exists per key. The unique constraint
  shipped with the table in `alembic/versions/0005_run_records.py`. It holds against every writer —
  this module, another service, a hand-written `INSERT` at a psql prompt.
* This **module** guarantees nothing about uniqueness and deliberately does not try to. There is no
  `SELECT` before the insert, because a pre-check cannot prevent anything: two workers that both
  read "no row yet" would both proceed, and the constraint is what separates them anyway. Removing
  the pre-check removes the temptation to believe it did the work.
* What `claim()` adds is that the loser of that race gets a **usable answer instead of an
  exception**. It catches the unique violation, reads the winner's row, and returns it. That is a
  convenience over the constraint, not a second line of defence.
* **Not guaranteed: that the prior task finished, or succeeded.** A claim that was not created by
  this caller means somebody else owns the key — possibly still running it, possibly having failed.
  The caller must read `Claim.task_run.outcome` and decide. `claim()` cannot tell you whether work
  it did not do went well.
* **Not guaranteed: that a paid model call is exactly-once.** A model call is not part of any
  database transaction, so no key can make it atomic with a row. What the key does deliver is that
  only one caller is ever told to make the call for a given task identity, and that a retry of a
  *delivery* does not produce a second claim. Which side of a crash the cost lands on is a
  caller-side ordering choice: claim-then-call can lose work if the process dies in between,
  call-then-claim can pay twice. `model_invocations` is append-only precisely so either outcome is
  visible afterwards rather than inferred.
* **`claim()` can block.** If another transaction has inserted the same key and has not yet
  committed, PostgreSQL makes the second `INSERT` wait for it — that is how the constraint decides
  the race. The wait ends when the first transaction commits (violation, so the prior row is
  returned) or rolls back (no violation, so this caller wins).

**Why the key is a hash of a structured document rather than joined-up text.** Concatenating the
five components with a separator lets one component impersonate another: a region literally spelled
`whole|extract` would collide with a different task whose region is `whole` and whose type is
`extract`. Serialising a mapping and hashing that leaves no separator for a value to contain.

**Stability across processes and restarts** is the property the whole mechanism rests on, and it is
easy to lose by accident. Nothing here reads the clock, generates randomness, or calls the built-in
`hash()` — `hash()` of a string is salted per process, so a key built from it would change at every
restart and every stored key would stop matching, silently, with the failure showing up as work
being done twice rather than as an error. Ordering comes from `sort_keys`, never from `dict`
iteration order. `tests/workflow/test_idempotency.py` runs the key in fresh interpreters under
different `PYTHONHASHSEED` values and compares against a written-down digest.

**Floats are refused** in the config, in line with `AGENTS.md` §6. `Decimal` and `Fraction` are
accepted and are the intended way to express an exact configured number.

Source: `AGENTS.md` §6; backend proposal §9.2, §9.3, §9.4 · Design: `docs/DESIGN_PLATFORM.md` §6.2 ·
Verification: `tests/workflow/test_idempotency.py`
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.runs import TaskRun

__all__ = [
    "CLAIMED",
    "Claim",
    "claim",
    "idempotency_key",
]

#: Prefix naming the digest algorithm, matching how rule snapshots are identified in
#: `app/models/rules.py`. It is inside the hashed identity's *label*, not its input: if this project
#: ever moves off SHA-256, the keys visibly change algorithm instead of two different digests being
#: indistinguishable strings of hex.
KEY_PREFIX: Final = "sha256:"

#: The outcome written when a task is claimed and has not yet reported anything. `task_runs.outcome`
#: is `NOT NULL` with a non-empty check, so a claim has to say something; this says "owned, no
#: result yet" rather than borrowing a word that means the work finished.
CLAIMED: Final = "claimed"

#: PostgreSQL's SQLSTATE for a unique violation.
_UNIQUE_VIOLATION: Final = "23505"

#: Substring identifying the constraint this module is allowed to treat as a duplicate delivery.
#: The installed name is `uq_task_runs_idempotency_key` — the project's naming convention, applied
#: on both build routes because `alembic/env.py` passes `Base.metadata` as `target_metadata`.
#: Matching on the column name rather than the whole string keeps this working if the table is ever
#: rebuilt under PostgreSQL's own default name, while still being specific enough that a future
#: unique constraint on some other `task_runs` column could not be mistaken for this one.
_IDEMPOTENCY_CONSTRAINT: Final = "idempotency_key"


@dataclass(frozen=True, slots=True)
class Claim:
    """Who owns a task identity, and whether this caller is the one that took it.

    `created` is the only thing that should gate a side effect. `True` means this caller inserted
    the row and is expected to do the work. `False` means the row was already there: do nothing, and
    read `task_run.outcome` to find out what happened to it.

    The row is returned in both cases on purpose. The issue's sketched interface returned either a
    `TaskRun` or a separate prior-result type, which forces an `isinstance` check at every call site
    and gives the losing caller no handle on the run that beat it — the run whose outcome it needs.
    """

    task_run: TaskRun
    created: bool


def idempotency_key(
    *,
    document_version_id: UUID,
    region: str | None,
    task_type: str,
    extractor_version: str,
    config: Mapping[str, object],
) -> str:
    """Compute the stable identity of one task as `sha256:<64 hex characters>`.

    The same five components always produce the same string — in this process, in another process,
    after a restart, on another machine. That is what lets a retry recognise itself.

    The **extractor version and the config are inside the key** because a changed reader is a
    different task, not a cache hit (`AGENTS.md` §2.7). Upgrading pdfplumber, or changing a
    threshold, produces a different key, so the page is read again rather than an old answer being
    reused under a new reader's name.

    Two spellings of the same thing are two keys, and the error always falls that way: a key that
    changes when it did not need to costs one rerun, while a key that fails to change reuses a
    result computed under different conditions. So `Decimal("1.0")` and `Decimal("1.00")` give
    different keys, and so do `region="page:7"` and `region="page:07"`. Callers should spell a
    region one way; nothing here can tell that two spellings meant the same page.

    Types are never flattened into text, so `{"pages": 1}` and `{"pages": "1"}` are different
    configs with different keys rather than a collision.

    Args:
        document_version_id: the immutable document version being read. A new upload is a new
            version and therefore a new key, which is what makes re-uploads safe.
        region: the page or region, or `None` for a task covering the whole document version. An
            empty string is refused — `None` is the one spelling of "no region", and allowing both
            would mean the same task could hold two identities.
        task_type: what is being done, e.g. `extract_page`.
        extractor_version: the version of the reader that will do it.
        config: the reader's configuration. Nested mappings and sequences are fine. Keys must be
            strings. Values may be `str`, `int`, `bool`, `None`, `Decimal`, `Fraction`, `UUID`, or
            mappings and sequences of those. Anything else raises `TypeError` rather than being
            coerced, because a coercion nobody chose is how two different configs end up sharing a
            key.

    Returns:
        The key, 71 characters, safe for the `String(500)` column.

    Raises:
        TypeError: a component or config value has a type this cannot serialise unambiguously.
        ValueError: a required component is empty, or a config key is unusable.
    """
    if not isinstance(document_version_id, UUID):
        raise TypeError("document_version_id must be a UUID")
    if region is not None:
        if not isinstance(region, str):
            raise TypeError("region must be a string or None")
        if not region.strip():
            raise ValueError("region must be a non-empty string, or None for a whole-document task")
    _require_text(task_type, "task_type")
    _require_text(extractor_version, "extractor_version")
    if not isinstance(config, Mapping):
        raise TypeError("config must be a mapping")

    document = {
        "config": _canonical(config),
        "document_version_id": str(document_version_id),
        "extractor_version": extractor_version,
        "region": region,
        "task_type": task_type,
    }
    # `sort_keys` rather than the order written above, so a later edit to this literal cannot change
    # any key. `separators` pins the spacing, `ensure_ascii` pins the escaping, and `allow_nan=False`
    # refuses the non-standard `NaN`/`Infinity` tokens outright. The encoding is stated, not
    # defaulted: `str.encode()` is UTF-8 today, and a key is not something to leave to a default.
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return KEY_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim(
    session: Session,
    key: str,
    *,
    workflow_run_id: UUID,
    task_type: str,
    attempt: int = 1,
    outcome: str = CLAIMED,
) -> Claim:
    """Take ownership of a task identity, or return the run that already owns it.

    Never raises because a task was delivered twice. It does raise for anything else, including an
    integrity error the driver cannot attribute to the idempotency key — swallowing one of those is
    how a mistyped `workflow_run_id` would turn into a silent no-op instead of the foreign-key
    error it is.

    **This function does not commit.** The caller's transaction decides whether the claim stands
    together with everything else it wrote. Until that commit, the claim is only visible to this
    transaction, and a rollback releases it — correctly, because a claim for work that was never
    recorded would block the retry that should redo it.

    Two implementation details that are not obvious and do matter:

    * The insert runs inside a **savepoint**. A failed `INSERT` aborts a PostgreSQL transaction, and
      without the savepoint the caller's whole unit of work would be unusable after a perfectly
      ordinary duplicate delivery.
    * **Work the caller had pending is not caught in that rollback.** Opening a nested transaction
      flushes the session, and SQLAlchemy emits that flush *before* the `SAVEPOINT` — verified in
      the statement log, not assumed — so rows the caller added earlier are already outside the
      savepoint's reach. That ordering belongs to SQLAlchemy rather than to this module, so
      `test_a_losing_claim_does_not_discard_the_callers_other_pending_work` pins it: if a future
      version flushed after the savepoint instead, a losing claim would quietly undo the caller's
      rows, and that test is what would say so.

    Reading the winner's row back relies on `READ COMMITTED`, PostgreSQL's default and this
    project's: the `SELECT` after the violation is a new statement and sees the row the winner
    committed. Under `REPEATABLE READ` that row would be invisible, and this re-raises the integrity
    error rather than reporting no prior run — a loud failure the caller can retry, never a wrong
    answer.

    Args:
        session: the caller's session. Not committed here.
        key: the value from `idempotency_key()`. Any non-empty string is accepted; the column stores
            what it is given.
        workflow_run_id: the workflow run this task belongs to.
        task_type: recorded on the row. Only meaningful when the claim is created — a losing caller
            gets the winner's row, with the winner's `task_type`, whatever was passed here.
        attempt: the delivery attempt that took the claim. Defaults to 1. The row is written once,
            so this records which attempt won, not how many there have been.
        outcome: the starting outcome. Defaults to `CLAIMED`.

    Returns:
        A `Claim`. `created` is `True` for the caller that inserted the row, `False` for one handed
        a prior run.

    Raises:
        TypeError: the key, task type, outcome, workflow run id or attempt is the wrong type.
        ValueError: the key, task type or outcome is blank, or `attempt` is below 1.
        IntegrityError: any integrity failure other than a duplicate idempotency key.
    """
    _require_text(key, "key")
    _require_text(task_type, "task_type")
    _require_text(outcome, "outcome")
    if not isinstance(workflow_run_id, UUID):
        raise TypeError("workflow_run_id must be a UUID")
    if not isinstance(attempt, int) or isinstance(attempt, bool):
        raise TypeError("attempt must be an integer")
    if attempt < 1:
        raise ValueError("attempt must be 1 or greater")

    task_run = TaskRun(
        workflow_run_id=workflow_run_id,
        idempotency_key=key,
        task_type=task_type,
        attempt=attempt,
        outcome=outcome,
    )
    try:
        with session.begin_nested():
            session.add(task_run)
            session.flush()
    except IntegrityError as error:
        if not _is_duplicate_idempotency_key(error):
            raise
        prior = session.scalar(select(TaskRun).where(TaskRun.idempotency_key == key))
        if prior is None:
            raise
        return Claim(task_run=prior, created=False)
    return Claim(task_run=task_run, created=True)


def _require_text(value: object, name: str) -> str:
    """Reject an absent or blank component, which would otherwise be a silently valid identity."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _is_duplicate_idempotency_key(error: IntegrityError) -> bool:
    """Decide whether this integrity error is the duplicate delivery we are allowed to absorb.

    Two things have to hold: the SQLSTATE says unique violation, and the constraint the database
    names is the one on the idempotency key. If the driver cannot name the constraint, the answer is
    no. Guessing would let some future unique constraint on `task_runs` be reported as "already
    done", which is the failure that costs a task rather than an error message.
    """

    original = getattr(error, "orig", None)
    diagnostic = getattr(original, "diag", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(diagnostic, "sqlstate", None)
    if sqlstate != _UNIQUE_VIOLATION:
        return False
    constraint = getattr(diagnostic, "constraint_name", None)
    return isinstance(constraint, str) and _IDEMPOTENCY_CONSTRAINT in constraint


def _canonical(value: object) -> Any:
    """Convert a config value into a JSON structure whose text is fixed by its type and content.

    Exact numbers keep their type: a `Decimal` becomes `{"$decimal": "..."}` rather than a bare
    string, so a configured `Decimal("1.5")` and the string `"1.5"` remain different configs. Config
    keys may not start with `$`, which is what keeps a caller's own dictionary from imitating one of
    these tags.
    """

    if value is None or isinstance(value, bool | str):
        # `bool` before `int`, because `bool` is a subclass of it and `True` must not read as 1.
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError(
            "config values must not be floats — use Decimal or Fraction for an exact number "
            "(AGENTS.md section 6)"
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("config Decimal values must be finite")
        return {"$decimal": str(value)}
    if isinstance(value, Fraction):
        return {"$fraction": f"{value.numerator}/{value.denominator}"}
    if isinstance(value, UUID):
        return {"$uuid": str(value)}
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise TypeError("config keys must be strings")
            if not raw_key:
                raise ValueError("config keys must not be empty")
            if raw_key.startswith("$"):
                raise ValueError("config keys must not start with '$', which is reserved here")
            canonical[raw_key] = _canonical(item)
        return canonical
    if isinstance(value, AbstractSet):
        raise TypeError(
            "config values must not be sets — a set has no order, so use a list in the order you "
            "meant and let that be part of the identity"
        )
    if isinstance(value, bytes | bytearray):
        raise TypeError("config values must not be bytes — pass text, or a hex or base64 string")
    if isinstance(value, Sequence):
        return [_canonical(item) for item in value]
    raise TypeError(f"config values of type {type(value).__name__} cannot be hashed unambiguously")
