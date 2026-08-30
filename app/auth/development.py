"""A signed-in identity for local development, and the guard that keeps it out of everywhere else.

`authenticate` refuses by default and that is deliberate: an anonymous default would make every
authorisation check pass, so the failure would surface as a data leak rather than as a missing
setting. The consequence is that nothing can call this API until a deployment wires an identity
provider — which is correct in production and makes the app unusable on a laptop.

This is the laptop answer, and it is written to be impossible to reach by accident:

* it is installed **only** when `Settings.environment == "development"`, checked at wiring time;
* it is installed **only** when `GV_DEV_PRINCIPAL` is set, so running in development is not enough —
  somebody has to ask for it;
* it logs a warning naming the principal and the projects, every time an app is built with it.

Two locks rather than one, because either alone is the kind of thing that gets flipped in a hurry. A
deployment that forgets to set `environment` still has to have exported the variable, and a
deployment that exports the variable still has to be calling itself development.

**This is not an identity provider and must never become one.** It has no credential to check — it
believes whatever the environment says. Real authentication replaces `authenticate` in the
application factory; see `app/auth/dependencies.py`.

Source: `docs/DESIGN_PLATFORM.md` §4.3 · Verification: `tests/api/test_development_identity.py`
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from app.auth.roles import Principal, Role

LOGGER = logging.getLogger(__name__)

#: The variable that asks for a development identity. Its value is the principal's name.
PRINCIPAL_VARIABLE = "GV_DEV_PRINCIPAL"
#: Comma-separated project UUIDs the development principal belongs to.
PROJECTS_VARIABLE = "GV_DEV_PROJECTS"
#: The only environment in which any of this is permitted.
DEVELOPMENT = "development"


class DevelopmentIdentityRefused(RuntimeError):
    """Raised when a development identity was asked for somewhere it may not exist."""


def _projects(raw: str | None) -> frozenset[UUID]:
    """Parse the project list, refusing anything that is not a UUID.

    A malformed entry is refused rather than skipped. Skipping would silently narrow what the
    developer can see, and they would spend the afternoon wondering why a package 404s.
    """
    if not raw or not raw.strip():
        return frozenset()
    projects = set()
    for entry in raw.split(","):
        text = entry.strip()
        if not text:
            continue
        try:
            projects.add(UUID(text))
        except ValueError as error:
            raise DevelopmentIdentityRefused(
                f"{PROJECTS_VARIABLE} contains {text!r}, which is not a UUID. Every project is "
                "identified by one, and guessing at what was meant would hand you access to "
                "something you did not name."
            ) from error
    return frozenset(projects)


def principal_from_environment(environment: str) -> Principal | None:
    """The development principal, or `None` if one was not asked for.

    Raises `DevelopmentIdentityRefused` if one was asked for outside development. Refusing loudly
    beats ignoring it: a deployment that set the variable believing it did something should find out
    at startup, not by discovering later that every request was unauthenticated.
    """
    requested = os.environ.get(PRINCIPAL_VARIABLE)
    if requested is None:
        return None

    if environment != DEVELOPMENT:
        raise DevelopmentIdentityRefused(
            f"{PRINCIPAL_VARIABLE} is set but the environment is {environment!r}. A development "
            "identity checks no credential — it believes whatever the environment says — so it "
            "exists only where nothing real is reachable. Wire a real identity provider instead."
        )

    name = requested.strip()
    if not name:
        raise DevelopmentIdentityRefused(
            f"{PRINCIPAL_VARIABLE} is empty. An approval names a person, and 'approved by the "
            "system' answers a different question from 'approved by whom'."
        )

    projects = _projects(os.environ.get(PROJECTS_VARIABLE))
    LOGGER.warning(
        "Using a DEVELOPMENT identity: principal=%s projects=%s. No credential is checked. "
        "This must never be reachable outside a laptop.",
        name,
        sorted(str(project) for project in projects) or "none",
    )
    return Principal(id=name, roles=frozenset(Role), projects=projects)
