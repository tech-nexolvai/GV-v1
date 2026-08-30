"""Run the API on a laptop, with the things a deployment would otherwise provide.

`create_app` deliberately builds an application that cannot do very much. The artifact store and the
rulebook are read off `app.state` and **refuse when absent** rather than defaulting, because a default
store would make uploads look configured everywhere and a default rulebook would answer "which rules
exist?" with a wrong fact instead of a missing setting. `authenticate` refuses for the same reason.

Those refusals are right in production and leave nothing runnable locally. This wires the missing
pieces and nothing else.

**It refuses outside development.** Same two locks as `app/auth/development.py`: the environment must
say `development`, and it will not start otherwise. A local store signing its own upload tickets and
an empty rulebook are development fixtures, not a deployment.

Usage:

    createdb gv                       # once
    alembic upgrade head              # once
    python scripts/dev_server.py

Environment:

    GV_DATABASE_URL     required — where the schema lives. Note the `GV_` prefix: `Settings` uses
                        `env_prefix="GV_"`, while the *test* fixture reads a bare `DATABASE_URL`.
                        Two names for two things, and mixing them up looks like a missing setting.
    GV_DEV_PRINCIPAL    required — who you are; see app/auth/development.py
    GV_DEV_PROJECTS     the project UUIDs you belong to, comma-separated
    GV_DEV_STORAGE      where uploaded drawings go (default ./.dev-storage)
    GV_DEV_PORT         default 8000

Verification: `tests/test_dev_server.py`
"""

from __future__ import annotations

import io
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEVELOPMENT = "development"
DEFAULT_PORT = 8000


class NotADevelopmentEnvironment(RuntimeError):
    """Raised when this is asked to run somewhere it must not."""


def build_app():
    """The application, plus the local fixtures a deployment would supply."""
    from app.api.dependencies import ARTIFACT_STORE_STATE
    from app.api.rules import RULEBOOK_STATE, Rulebook
    from app.config import Settings
    from app.main import create_app
    from rules.governance.publish import PublicationLog
    from rules.snapshot import SnapshotStore
    from storage.local import LocalStore

    settings = Settings()  # type: ignore[call-arg]
    if settings.environment != DEVELOPMENT:
        raise NotADevelopmentEnvironment(
            f"environment is {settings.environment!r}. This wires a local filesystem store that "
            "signs its own upload tickets and an empty in-memory rulebook. They are development "
            "fixtures; a deployment supplies the real ones."
        )

    app = create_app(settings)

    root = Path(os.environ.get("GV_DEV_STORAGE", ".dev-storage")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Generated per run, and that is the honest behaviour: a ticket is short-lived, and one that
    # survived a restart would be a standing capability nobody minted deliberately. Tickets issued
    # before a restart stop verifying, which is what should happen.
    secret = os.environ.get("GV_DEV_TICKET_SECRET", secrets.token_hex(32)).encode()
    store = LocalStore(root=root, ticket_secret=secret)
    setattr(app.state, ARTIFACT_STORE_STATE, store)

    # Empty on purpose. The real rulebook is authored and published through D6; an empty one answers
    # "which rules exist?" with "none", which is true here, rather than with a fixture that would be
    # mistaken for the client's.
    _mount_upload_shim(app, store)

    setattr(
        app.state,
        RULEBOOK_STATE,
        Rulebook(store=SnapshotStore(), log=PublicationLog(), proposals={}, regression=None),
    )
    return app


#: Imported at module level, not inside `_mount_upload_shim`, and that is load-bearing.
#:
#: Under PEP 649 — the default from Python 3.14 — a function's annotations are evaluated lazily
#: against its **module globals**, not the closure it was defined in. With `Request` imported inside
#: the mounting function, FastAPI could not resolve the annotation, decided `request` was an ordinary
#: value, and demoted it to a required query parameter: every upload came back 422 asking for a query
#: field named `request`. The route was mounted and matched; only the signature was misread.
from fastapi import HTTPException, Request, status

from storage.local import TICKET_PARAMETER


def _mount_upload_shim(app, store) -> None:
    """Accept the browser's upload, because a browser cannot PUT to a `file://` URL.

    `LocalStore.upload_ticket` returns a `file:` URI with a signed token in the query string, and
    says of itself: *"A local filesystem has no gatekeeper to present it to… what it gives you is
    `verify_upload_ticket`, so whatever does accept a write — a development upload shim, a test
    harness — can check correctly."* This is that shim.

    **It is mounted here and never in `app/`.** `create_app` has an enumerating test that fails if any
    of its routes accepts a file body, and that guard exists because the control plane does short work
    only — on one 8 GB VM a request carrying a drawing competes with PostgreSQL and OCR for memory.
    Nothing about that changes; this route is added after the factory returns, by a script that
    refuses to run outside development, and it does not ship.

    **It verifies the ticket rather than accepting any write.** An open write endpoint under the
    storage root would be a worse thing to have on a laptop than the inconvenience it removes, and
    `verify_upload_ticket` is exactly the check S3 would perform on its own signature. On the S3
    backend (#221) the ticket is a real presigned URL, the browser writes straight to the bucket, and
    this shim has nothing left to do.
    """

    @app.put("/_dev/upload/{key:path}", include_in_schema=False)
    async def receive(key: str, request: Request) -> dict[str, str]:
        # `TICKET_PARAMETER`, not a repeated "ticket" literal: the store composes the URL and this
        # takes it apart, so the two must agree, and importing the constant is what makes them.
        token = request.query_params.get(TICKET_PARAMETER, "")
        try:
            store.verify_upload_ticket(token, key=key)
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This ticket does not permit writing that object: {error}",
            ) from error

        body = await request.body()
        content_type = request.headers.get("content-type", "application/octet-stream")
        store.put(key, io.BytesIO(body), content_type=content_type)
        return {"key": key, "bytes": str(len(body))}


def main() -> int:
    import uvicorn

    if not os.environ.get("GV_DATABASE_URL"):
        print(
            "GV_DATABASE_URL is not set. The API needs a schema to talk to. Note the GV_ prefix — "
            "a bare DATABASE_URL is what the test fixture reads, not the application.",
            file=sys.stderr,
        )
        return 1
    if not os.environ.get("GV_DEV_PRINCIPAL"):
        print(
            "GV_DEV_PRINCIPAL is not set, so every request would be refused. Set it to your name "
            "— see app/auth/development.py for why it is not defaulted.",
            file=sys.stderr,
        )
        return 1

    port = int(os.environ.get("GV_DEV_PORT", DEFAULT_PORT))
    print(f"API on http://localhost:{port} — development identity, local storage, empty rulebook.")
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
