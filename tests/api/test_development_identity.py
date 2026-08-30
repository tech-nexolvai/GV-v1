"""The development identity, and the two locks that keep it on a laptop.

`authenticate` refuses by default, which is what stops a missing identity provider becoming an
anonymous principal. The cost is that nothing can call the API locally, so there is a development
override — and the whole value of it is that it cannot be reached anywhere else. These assert that,
in both directions: it works when asked for in development, and it refuses everywhere else.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.auth import AuthenticationNotConfigured, Role, authenticate
from app.auth.development import (
    PRINCIPAL_VARIABLE,
    PROJECTS_VARIABLE,
    DevelopmentIdentityRefused,
    principal_from_environment,
)
from app.config import Settings
from app.main import create_app

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"


def _settings(environment: str = "development") -> Settings:
    return Settings(database_url=DATABASE_URL, environment=environment)  # type: ignore[call-arg]


def test_nothing_is_installed_unless_it_is_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """**Being in development is not enough.** Somebody has to set the variable, so a developer who
    has never heard of this gets the same refusal as production."""
    monkeypatch.delenv(PRINCIPAL_VARIABLE, raising=False)

    assert principal_from_environment("development") is None
    assert create_app(_settings()).dependency_overrides == {}


def test_it_is_refused_outside_development(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The lock that matters.** A development identity checks no credential — it believes whatever
    the environment says — so anywhere real, it is a way in with no lock on it.

    It refuses loudly rather than being ignored: a deployment that set this believing it did
    something should find out at startup, not by discovering later that every request was
    unauthenticated.
    """
    monkeypatch.setenv(PRINCIPAL_VARIABLE, "anant")

    for environment in ("production", "staging", "Development", "dev", ""):
        with pytest.raises(DevelopmentIdentityRefused, match="environment"):
            principal_from_environment(environment)


def test_the_app_refuses_to_build_with_it_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal has to reach the factory, not just the helper. A production app that started
    happily and ignored the variable would be the quiet version of the same mistake."""
    monkeypatch.setenv(PRINCIPAL_VARIABLE, "anant")

    with pytest.raises(DevelopmentIdentityRefused):
        create_app(_settings(environment="production"))


def test_in_development_it_signs_the_named_person_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """And it has to actually work, or the override is theatre and nobody can run the app."""
    project = uuid4()
    monkeypatch.setenv(PRINCIPAL_VARIABLE, "anant")
    monkeypatch.setenv(PROJECTS_VARIABLE, str(project))

    principal = principal_from_environment("development")

    assert principal is not None
    assert principal.id == "anant"
    assert principal.belongs_to(project)
    assert principal.roles == frozenset(Role)


def test_an_unnamed_principal_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An approval names a person. "Approved by the system" answers a different question from
    "approved by whom", and only the second is defensible later."""
    monkeypatch.setenv(PRINCIPAL_VARIABLE, "   ")

    with pytest.raises(DevelopmentIdentityRefused, match="empty"):
        principal_from_environment("development")


def test_a_malformed_project_is_refused_rather_than_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping would silently narrow what the developer can see, and they would spend the afternoon
    wondering why a package 404s — the boundary is deliberately indistinguishable from absence."""
    monkeypatch.setenv(PRINCIPAL_VARIABLE, "anant")
    monkeypatch.setenv(PROJECTS_VARIABLE, f"{uuid4()},not-a-uuid")

    with pytest.raises(DevelopmentIdentityRefused, match="not a UUID"):
        principal_from_environment("development")


def test_without_the_override_the_api_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default the whole design rests on, asserted here so this file cannot be read as having
    softened it."""
    monkeypatch.delenv(PRINCIPAL_VARIABLE, raising=False)
    app = create_app(_settings())

    with pytest.raises(AuthenticationNotConfigured):
        app.dependency_overrides.get(authenticate, authenticate)(None)  # type: ignore[arg-type]
