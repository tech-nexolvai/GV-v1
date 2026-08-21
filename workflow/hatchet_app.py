"""The Hatchet client and worker: built from settings, never at import (#215, C4.3).

`Hatchet()` refuses to construct without a token — verified, not assumed:

    ValidationError: 1 validation error for ClientConfig
    Value error, Token must be set

So this module exposes factories rather than module-level objects. A client built at import would make
this file unimportable wherever the token is absent: every test, every static check, every tool that
merely wants to read the stage list. `app/main.py` takes the same shape with `create_app`, and for the
reason it states — a missing value should fail at startup where somebody sees it, not at import where
the failure is a stack trace in something unrelated.

**The worker holds no business truth.** It runs steps; `workflow/review.py` writes to PostgreSQL through
`app/lifecycle/`. That is backend §2's requirement that the engine stay replaceable, and it is why there
is so little in this file: everything worth testing is in `review.py`, which needs no engine at all.

**Concurrency is configuration, and it starts low on purpose.** One 8 GB VM shares memory between
rendering, OCR and PostgreSQL, so the default is one package at a time — see `app/config.py`. Raising it
is a measurement, not a guess.

Source: backend proposal §9.1–§9.4 · Design: `docs/DESIGN_PLATFORM.md` §6 ·
Verification: `tests/workflow/test_review_workflow.py`
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy.orm import Session, sessionmaker

from workflow.review import WORKFLOW_NAME, Stages, register

if TYPE_CHECKING:  # pragma: no cover - import-time only for the annotation
    # Annotations only, so the gRPC stack still stays out of the runtime import. Both names come from the
    # package root — `hatchet_sdk.Worker` is exported, and `Hatchet.worker` is annotated as returning it,
    # so this is the SDK's own name for the thing rather than a private path that could move.
    from hatchet_sdk import Hatchet, Worker

    from app.config import Settings

__all__ = ["WORKER_NAME", "build_worker", "hatchet_client", "workflow_name"]

#: The worker's name as it registers with the engine. One name, so two workers on one box are visibly
#: two workers rather than one that mysteriously has twice the capacity.
WORKER_NAME: Final = "gv-package-review"


def hatchet_client(settings: Settings) -> Hatchet:
    """A Hatchet client built from validated settings.

    Imported inside the function rather than at module scope. `hatchet_sdk` pulls in a gRPC stack, and
    this module is imported by `workflow/review.py`'s tests and by anything reading `WORKER_NAME`;
    dragging gRPC into all of that for a client most callers never build is cost with no return.

    Raises whatever the SDK raises when the token is absent, which is the intended behaviour: a worker
    that cannot reach the engine should fail on the way up, loudly.
    """
    from hatchet_sdk import ClientConfig, Hatchet

    return Hatchet(
        config=ClientConfig(
            token=settings.hatchet_token,
            namespace=settings.hatchet_namespace,
        )
    )


def build_worker(
    settings: Settings,
    *,
    factory: sessionmaker[Session],
    stages: Stages | None = None,
) -> Worker:
    """A worker with the package review workflow registered on it.

    Returns the worker without starting it, so a caller decides when to block — and so a test can build
    one and inspect it. Starting is `worker.start()`, deliberately not wrapped: a function called
    `run_forever` that swallowed a startup failure would hide exactly the failure worth seeing.

    Both concurrency caps come from settings. `max_concurrent_packages` shapes the workflow's own
    concurrency; `max_concurrent_page_tasks` becomes the worker's slot count.

    Being exact about that second one, because the setting's name is ahead of the code: the only tasks
    registered today are the six stages, and they run in a line, so this currently caps concurrent stage
    tasks rather than page tasks — and a single package can never occupy more than one slot. It becomes
    literally what it says when B6.4 (#163) adds the per-page fan-out. See `app/config.py`.
    """
    hatchet = hatchet_client(settings)
    workflow = register(
        hatchet,
        factory=factory,
        stages=stages,
        max_concurrent_packages=settings.max_concurrent_packages,
    )
    return hatchet.worker(
        WORKER_NAME,
        slots=settings.max_concurrent_page_tasks,
        workflows=[workflow],
    )


def workflow_name() -> str:
    """The name the outbox enqueues and the engine registers.

    A function rather than a re-exported constant so there is one definition — `workflow/review.py`
    owns it, and `app/lifecycle/supersede.py` enqueues the same string. A mismatch between those two is
    a package nothing ever picks up, which looks like a slow queue rather than a bug.
    """
    return WORKFLOW_NAME
