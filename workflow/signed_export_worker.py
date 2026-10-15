"""Engine registration for approval-bound publication."""

from datetime import timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from hatchet_sdk import Hatchet
    from hatchet_sdk.runnables.workflow import Workflow


#: How long a publication may wait for a worker before the engine cancels it (SDK default: 5 minutes).
#: The worker runs `max_concurrent_page_tasks` tasks at once (2 by default) and a reading holds one for
#: hours, so an approval can wait behind readings; a cancelled one is an approval with no signed files.
SIGNED_EXPORT_WAITS_UP_TO: Final = timedelta(hours=12)

#: How long one publication may take once started (SDK default: 60 seconds). Approval to signed bundle
#: took 2 to 9 seconds on six full runs (9 to 15 Oct); 30 minutes leaves ample room for a slow store.
SIGNED_EXPORT_TAKES_UP_TO: Final = timedelta(minutes=30)


class SignedExportInput(BaseModel):
    package_revision_id: UUID
    approval_id: UUID


def register_signed_exports(
    hatchet: "Hatchet", factory: sessionmaker[Session], stages: object
) -> "Workflow[SignedExportInput]":
    from app.models.review import Approval
    from workflow.signed_outputs import record_publication_failure
    from workflow.stages import DatabaseStages

    workflow = hatchet.workflow(name="generate_signed_exports", input_validator=SignedExportInput)

    @workflow.task(
        name="publish_signed_exports",
        schedule_timeout=SIGNED_EXPORT_WAITS_UP_TO,
        execution_timeout=SIGNED_EXPORT_TAKES_UP_TO,
    )
    def publish(payload: SignedExportInput, ctx: object) -> dict[str, object]:
        del ctx
        if not isinstance(stages, DatabaseStages):
            raise TypeError("signed export stages are not configured")
        try:
            with factory.begin() as db:
                approval = db.get(Approval, payload.approval_id)
                if approval is None or approval.package_revision_id != payload.package_revision_id:
                    raise ValueError("signed export request belongs to a different revision")
                return dict(stages.generate_signed_outputs(db, payload.approval_id))
        except Exception as error:
            record_publication_failure(factory, payload.approval_id, error)
            raise

    return workflow
