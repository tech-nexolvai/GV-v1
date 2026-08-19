"""Dependencies more than one route group needs: the request session, and the artifact store.

**Why these moved here.** `get_session` was written for the findings router (#222) and is now needed by
the packages and documents routers too (#205). Three copies of an engine-caching dependency is three
places for the caching to be got wrong, and the one that matters — building an engine per request,
which means a connection pool per request — is invisible in review because each copy looks fine on its
own. `app.api.findings.get_session` still resolves to the function below, so tests that override it by
name keep working: `dependency_overrides` is keyed by the function object, not by where it is written.

**The artifact store is a seam that fails closed**, in the same way `app/auth/dependencies.py`
`authenticate` does. Cloud provisioning is deferred (`docs/DESIGN_PLATFORM.md` §7) and the upload
ticket needs a signing secret that must not have a default, so there is nothing honest to construct
here. A default local store would silently give every deployment a working-looking upload path
writing to a developer's home directory, and the failure would arrive as missing drawings rather than
as missing configuration.

Source: `docs/DESIGN_PLATFORM.md` §4.1, §7 · Verification: `tests/api/test_packages.py`
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from app.db.session import engine_from_settings, session_factory
from storage.store import ArtifactStore

__all__ = [
    "ARTIFACT_STORE_STATE",
    "ArtifactStoreNotConfigured",
    "get_artifact_store",
    "get_session",
]

_engine_lock = threading.Lock()
_ENGINE_STATE = "_gv_read_engine"

#: Where a deployment puts the store: `app.state.artifact_store = LocalStore(...)`. Read off the app
#: rather than the environment for the same reason settings are — `app/config.py` is explicit that a
#: value is validated once, at startup, and an `os.environ.get` here would be a second, unvalidated
#: source of the same configuration.
ARTIFACT_STORE_STATE = "artifact_store"


class ArtifactStoreNotConfigured(RuntimeError):
    """No artifact store is wired, so no upload can be arranged.

    Raised rather than defaulting to a local store under the developer's home directory. A default
    would make the upload endpoints appear to work in every environment, and the failure would surface
    as drawings nobody can find rather than as a setting nobody set.
    """


def get_session(request: Request) -> Iterator[Session]:
    """A session for the life of one request, built from the settings the factory validated.

    The engine is cached on the application rather than rebuilt per request: an engine owns a
    connection pool, and making a new one per request means a new pool per request, which exhausts
    PostgreSQL's connection limit under any real load.

    **This does not commit or roll back.** A read endpoint needs neither, and a write endpoint has to
    own its own transaction boundary — the point of the outbox (`workflow/outbox.py`) is that the
    business change and the outbox row commit *together*, and a dependency that committed on the way
    out would decide that boundary on the endpoint's behalf. The session is closed either way, which
    discards an uncommitted transaction.

    A deployment or a test replaces this with `dependency_overrides`.
    """
    engine = getattr(request.app.state, _ENGINE_STATE, None)
    if engine is None:
        # Locked because sync dependencies run in a threadpool: two simultaneous first requests
        # would otherwise each build a pool, and one of them would be leaked with nothing closing it.
        with _engine_lock:
            engine = getattr(request.app.state, _ENGINE_STATE, None)
            if engine is None:
                engine = engine_from_settings(request.app.state.settings)
                setattr(request.app.state, _ENGINE_STATE, engine)
    session = session_factory(engine)()
    try:
        yield session
    finally:
        session.close()


def get_artifact_store(request: Request) -> ArtifactStore:
    """The configured artifact store, or a refusal.

    Returns whatever the deployment put on `app.state.artifact_store`. It is not type-checked at
    runtime beyond being present: `ArtifactStore` is a `Protocol`, and an `isinstance` check against a
    runtime-checkable protocol only confirms the method *names* exist, which would read as a
    verification while proving almost nothing.
    """
    store: ArtifactStore | None = getattr(request.app.state, ARTIFACT_STORE_STATE, None)
    if store is None:
        raise ArtifactStoreNotConfigured(
            "no artifact store is configured. Set `app.state.artifact_store` in the application "
            "factory or override `get_artifact_store`; this refuses rather than defaulting to a "
            "local store, because a default makes uploads look configured everywhere."
        )
    return store
