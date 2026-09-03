"""Reading the published rulebook back out of the database.

The counterpart to `app/models/parameters.py:from_rows`, and here rather than in `app/models/rules.py`
for a reason a guard found: `app/review/ledger.py` imports that module for its ORM classes, and
`tests/review/test_proposal_gate.py` refuses to let the correction ledger reach anything that can
publish. Putting a `rules.snapshot` import in the ORM module handed it exactly that reach — a
correction becoming a rule change is the thing the guard exists to prevent, and it would have arrived
sideways, through a loader nobody thought of as publishing machinery.

So the tables stay in `app/models/rules.py` and the translation lives here, beside the other code that
turns rows into the objects the engine consumes.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.rules import RuleSnapshot
from rules.schema import Rule
from rules.snapshot import RuleSnapshot as InMemorySnapshot
from rules.snapshot import SnapshotStore

__all__ = ["UnreadableSnapshot", "from_row", "snapshot_store"]


class UnreadableSnapshot(ValueError):
    """Raised when a stored snapshot cannot be read back as the rule it claims to be.

    Loud, and never skipped. A rulebook that loaded seven of eight rules would run seven checks and
    report nothing about the eighth, and an unrun check is indistinguishable from a passing one.
    """


def from_row(row: RuleSnapshot) -> InMemorySnapshot:
    """Rebuild the published snapshot from its row.

    The counterpart to `app/models/parameters.py:from_rows`, and for the same reason its docstring
    gives: the row is not the value object. This one has a surrogate key, a creation time and foreign
    keys, and lives in a package `rules/` may not import.

    **Rebuilt through `Rule.model_validate`, not reconstructed field by field.** The canonical JSON is
    the bytes that were hashed, so validating them is the only reading that can be the published rule
    rather than a rule that resembles it. A row whose JSON no longer satisfies the schema fails here,
    where it is a loud refusal, instead of becoming a `Rule` the engine trusts.

    **`verify()` is called, and that is the point of storing `canonical_json` at all.** The identifier
    *is* the content hash, so re-hashing the stored bytes and comparing is what makes "this is the
    snapshot that judged the drawing" checkable rather than asserted. A row edited in the database —
    which the append-only trigger should prevent, but a superuser can — is caught here.
    """
    try:
        rule = Rule.model_validate(json.loads(row.canonical_json))
    except (ValueError, TypeError) as error:
        # Named, because the caller is loading the whole rulebook and "a rule failed to validate" is
        # not something anybody can act on. Raised rather than skipped: a snapshot that will not
        # validate is an integrity failure, and quietly loading the other seven would mean a check
        # silently stopped running — which is the omission the whole system is built to prevent.
        raise UnreadableSnapshot(
            f"rule snapshot {row.snapshot_id!r} does not validate as a rule: {error}"
        ) from error

    snapshot = InMemorySnapshot(
        snapshot_id=row.snapshot_id,
        rule=rule,
        canonical_json=row.canonical_json,
    )
    snapshot.verify()
    return snapshot


def snapshot_store(session: Session) -> SnapshotStore:
    """Every published rule, as the store the applicability resolver and the engine expect.

    Loaded in full rather than queried per rule: the resolver's first act is to ask for every
    candidate id (`rules/applicability.py`), so a lazy store would issue a query per rule to answer a
    question it asks once. The rulebook is small by design — eight rules today, and a rulebook that
    grew past a page would be a different problem than this function.

    `SnapshotStore.add` verifies on the way in and de-duplicates by identifier, so two rows carrying
    the same published bytes collapse to one entry rather than racing.

    **The store is built empty when nothing is published, and that is a real answer.** It is what
    `scripts/dev_server.py` has been passing all along; the difference is that this one is empty
    because the database says so.
    """
    store = SnapshotStore()
    for row in session.execute(select(RuleSnapshot)).scalars():
        store.add(from_row(row))
    return store
