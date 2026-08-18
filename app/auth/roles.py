"""Who may do what, as data rather than scattered `if` statements.

A permission written as a condition inside an endpoint is a permission nobody can audit: answering
"who can publish a rule?" means reading every route. Written as a table it can be printed, reviewed,
and tested exhaustively — and a new action that nobody assigned is a `KeyError` at import rather than
an endpoint quietly open to everyone.

Source: backend proposal §11 · Design: `docs/DESIGN_PLATFORM.md` §4.3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    """The three roles. Deliberately few — a role per endpoint is a permission system nobody reads."""

    REVIEWER = "reviewer"
    """Confirms evidence and approves packages. May not change what the rules say."""

    RULE_ADMIN = "rule_admin"
    """Publishes rule snapshots. May not approve the packages those rules judge.

    Kept separate from `reviewer` on purpose: the person who decides what "correct" means and the
    person who certifies that a drawing meets it should be able to be different people, and a single
    combined role would make that impossible to express.
    """

    ADMIN = "admin"


class Action(StrEnum):
    """Every guarded action. One member per thing a role may be allowed to do."""

    READ_PACKAGE = "read_package"
    CONFIRM_EVIDENCE = "confirm_evidence"
    APPROVE_PACKAGE = "approve_package"
    PUBLISH_RULE = "publish_rule"
    MANAGE_PROJECT = "manage_project"


#: Which roles may take which action. Exhaustive over `Action` — see `_every_action_is_assigned`.
#:
#: `admin` is listed explicitly everywhere rather than short-circuited in code. A wildcard would mean
#: the table no longer answers "who may publish a rule?" on its own, which is the entire point of
#: having one.
PERMISSIONS: dict[Action, frozenset[Role]] = {
    Action.READ_PACKAGE: frozenset({Role.REVIEWER, Role.RULE_ADMIN, Role.ADMIN}),
    Action.CONFIRM_EVIDENCE: frozenset({Role.REVIEWER, Role.ADMIN}),
    Action.APPROVE_PACKAGE: frozenset({Role.REVIEWER, Role.ADMIN}),
    Action.PUBLISH_RULE: frozenset({Role.RULE_ADMIN, Role.ADMIN}),
    Action.MANAGE_PROJECT: frozenset({Role.ADMIN}),
}

_unassigned = set(Action) - set(PERMISSIONS)
_empty = {action for action, roles in PERMISSIONS.items() if not roles}
if _unassigned or _empty:  # pragma: no cover - a wiring error, caught at import
    raise RuntimeError(
        f"actions with no roles assigned: {sorted(a.value for a in _unassigned | _empty)}. "
        "An unassigned action is one no check can evaluate, and the safe reading of that is not "
        "obvious — so it fails here rather than at the first request.\n\n"
        "An action mapped to an *empty* set counts too, and it is the easier one to write by "
        "accident: it passes a presence check, reads as deliberate, and means nobody may ever take "
        "the action. A permission nobody holds and nobody intended is indistinguishable from a "
        "broken endpoint."
    )


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is asking, and what they may reach.

    `projects` is the isolation boundary (ADR-0006), not a convenience filter. A principal holds the
    projects they belong to, and everything else is invisible — not forbidden, *invisible*, which is
    a different and stronger claim.
    """

    id: str
    roles: frozenset[Role]
    projects: frozenset[UUID] = field(default_factory=frozenset)

    def may(self, action: Action) -> bool:
        return bool(self.roles & PERMISSIONS[action])

    def belongs_to(self, project_id: UUID) -> bool:
        return project_id in self.projects
