"""Four database roles, and what each is allowed to touch.

The import guard in `tests/test_verdict_isolation.py` proves the verdict service cannot *import*
retrieval or model code. This is the other half: proving it cannot *read* those tables even if
somebody hands it a session. A static guard stops the obvious mistake; a grant stops the one where
a session is passed in from outside and the import graph is untouched.

**An allowlist, not a denylist.** `gv_verdict` is granted the handful of tables a verdict needs and
nothing else. The issue asked for "cannot read retrieval or model tables", and a denylist would
satisfy that sentence while failing the intent: the next retrieval table somebody adds would be
readable by default, and nobody would notice until it was already an operand. With an allowlist, a
new table is denied until someone writes it down.

**Group roles with `NOLOGIN`, and no passwords anywhere.** These are permission sets, not accounts.
Deployment creates login users and grants them membership; nothing here needs a credential, so
nothing here can leak one. It also makes the tests honest: a test can `SET ROLE gv_verdict` and get
that role's actual privileges without a connection string existing for it.

**The trap this avoids.** Migration `0013` proposed revoking `UPDATE` from `gv_app` and `gv_worker`
and then explained why it did not: neither role existed, and CI connects as the database owner —
`REVOKE` does not restrict an owner or a superuser, so the revoke would have run, the test would
have attempted an update, and it would have **succeeded**. The guard would have been decoration.
`SET ROLE` is what makes these grants testable: after it, permission checks use the assumed role and
ownership no longer applies.

**What this does not yet do.** Nothing connects as any of these roles. The application still runs as
the owner, so these grants restrict nothing in production today — they are the declaration that a
deployment binds to, and the live tests prove the declaration is real rather than aspirational. The
day a service connects as `gv_app`, the grant half of C1.12 that `0013` deferred becomes true: an
`UPDATE` against an immutable table is refused by privilege as well as by trigger.

**Why `app.models` is imported here.** Every grant below is derived from `Base.metadata`, and a
SQLAlchemy model only registers itself when its module is imported. Without that import this module
computed its grants against an empty schema: three of the four roles got no grants at all, and
`forbidden_for_verdict()` returned nothing — so the test enumerating tables the verdict must not read
collected zero cases and passed. That is the failure `app/models/__init__.py` was written to prevent,
in its words: *"the check is green and it is checking nothing."* It was caught here before this
shipped, and `test_the_declaration_is_not_empty` now fails if it ever comes back.

Source: backend proposal §11; `AGENTS.md` §2.1, §2.9 · Verification: ``tests/db/test_roles.py``
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

import app.models  # noqa: F401  (side-effect import, must come after Base)
from app.db.base import Base, immutable_table_names

__all__ = [
    "APPEND_ONLY_PRIVILEGES",
    "MUTABLE_PRIVILEGES",
    "ROLE_GRANTS",
    "VERDICT_READS",
    "VERDICT_WRITES",
    "Role",
    "RoleGrants",
    "all_table_names",
    "forbidden_for_verdict",
]


class Role(StrEnum):
    """The four roles backend §11 names.

    Named with a `gv_` prefix because roles are cluster-wide: a database shared with anything else
    would otherwise have this project claiming names like `app` and `report`.
    """

    APP = "gv_app"
    """The control-plane API. Reads and writes operational state; appends to immutable tables."""

    WORKER = "gv_worker"
    """Durable workflow execution. The same as `APP` plus the queue and run-tracking tables."""

    REPORT = "gv_report"
    """Read-only reporting. Read-only *at the database*, so no application bug can widen it."""

    VERDICT = "gv_verdict"
    """The deterministic core. An allowlist — no model tables, no retrieval tables, nothing that
    would let it reach past the evidence gate."""


#: What a role may do to a table it can change.
#:
#: `DELETE` is absent deliberately, for every role. Nothing in this system deletes a row as part of
#: normal operation: a package is superseded, a finding is re-run, a correction is a new row. A role
#: holding `DELETE` because it might one day need it is how the audit trail acquires a gap.
MUTABLE_PRIVILEGES: Final = ("SELECT", "INSERT", "UPDATE")

#: What any role may do to an immutable table. This is the grant half of C1.12.
#:
#: `0013` enforces it with a trigger, which an owner can disable. A role that was never granted
#: `UPDATE` cannot issue one at all, and the two together are what makes "append-only" a property
#: rather than a convention.
APPEND_ONLY_PRIVILEGES: Final = ("SELECT", "INSERT")

#: Exactly what a verdict reads.
#:
#: `verdict_inputs` and not `canonical_observations`: the sealed handoff is the boundary, and reading
#: observations directly is how an unqualified reading becomes an operand. `AGENTS.md` §2.1 —
#: retrieval output must never be a verdict operand — is enforced here by there being no privilege
#: to read it.
VERDICT_READS: Final = (
    "check_runs",
    "finding_evidence",
    "findings",
    "rule_applicability_scopes",
    "rule_definitions",
    "rule_snapshots",
    "verdict_inputs",
)

#: What a verdict writes. All three are immutable, so `INSERT` and `SELECT` only.
VERDICT_WRITES: Final = ("check_runs", "finding_evidence", "findings")

#: Tables the worker needs beyond what the API does — the queue and the run records.
WORKER_ONLY: Final = (
    "agent_node_invocation_claims",
    "extraction_runs",
    "outbox_entries",
    "task_runs",
    "workflow_runs",
)


@dataclass(frozen=True, slots=True)
class RoleGrants:
    """One role's privileges, as a table name to privilege tuple mapping.

    A mapping rather than a list of statements, so a test can compare the declaration against
    `information_schema` without parsing SQL — and so the declaration is the same object the
    migration applies. Two representations of a grant is how a grant comes to differ from what
    somebody read in the docstring.
    """

    role: Role
    privileges: MappingProxyType[str, tuple[str, ...]]

    def tables(self) -> tuple[str, ...]:
        return tuple(sorted(self.privileges))

    def may(self, table: str, privilege: str) -> bool:
        return privilege in self.privileges.get(table, ())


def all_table_names() -> tuple[str, ...]:
    """Every mapped table, derived rather than listed.

    Derived so a new table is covered by the read-only and no-privilege rules the moment it exists.
    A hand-kept list would leave the newest table — the one nobody has thought about yet — outside
    every rule here.
    """
    return tuple(sorted(Base.metadata.tables))


def _operational_grants() -> dict[str, tuple[str, ...]]:
    """Read-write where a table is mutable, append-only where it is not."""
    immutable = set(immutable_table_names())
    return {
        table: APPEND_ONLY_PRIVILEGES if table in immutable else MUTABLE_PRIVILEGES
        for table in all_table_names()
    }


def _app_grants() -> dict[str, tuple[str, ...]]:
    """Everything except the worker's own queue and run tables.

    The API does not run workflows. Granting it the outbox would mean an HTTP handler could enqueue
    or, worse, mark work done — and the durability argument for an outbox is that exactly one thing
    writes it.
    """
    grants = _operational_grants()
    for table in WORKER_ONLY:
        grants.pop(table, None)
    return grants


def _worker_grants() -> dict[str, tuple[str, ...]]:
    return _operational_grants()


def _report_grants() -> dict[str, tuple[str, ...]]:
    """`SELECT` on everything, and nothing else.

    Read-only at the database rather than in application code. A reporting query that accidentally
    opened a write transaction, or a report endpoint that grew a "fix this row" button, is refused
    by the connection rather than by whoever reviews the pull request.
    """
    return {table: ("SELECT",) for table in all_table_names()}


def _verdict_grants() -> dict[str, tuple[str, ...]]:
    """The allowlist. Everything not named here is denied by omission."""
    writes = set(VERDICT_WRITES)
    return {
        table: APPEND_ONLY_PRIVILEGES if table in writes else ("SELECT",) for table in VERDICT_READS
    }


ROLE_GRANTS: Final[MappingProxyType[Role, RoleGrants]] = MappingProxyType(
    {
        role: RoleGrants(role=role, privileges=MappingProxyType(dict(sorted(grants.items()))))
        for role, grants in (
            (Role.APP, _app_grants()),
            (Role.WORKER, _worker_grants()),
            (Role.REPORT, _report_grants()),
            (Role.VERDICT, _verdict_grants()),
        )
    }
)
"""Every role's grants, keyed by role.

Immutable at every level — the outer mapping and each `privileges` mapping are both read-only. A
caller that could add a table to a role's grants at runtime would make the declaration and the
database disagree, and the declaration is what the tests check.
"""


def forbidden_for_verdict() -> tuple[str, ...]:
    """Tables the verdict role must not be able to read, for the test to assert against.

    Derived by subtraction, which is the point: a retrieval or model table added tomorrow appears
    here automatically and the test starts asserting it. Naming them by hand would mean the guard
    covered the tables somebody remembered.
    """
    allowed = set(ROLE_GRANTS[Role.VERDICT].privileges)
    return tuple(table for table in all_table_names() if table not in allowed)
