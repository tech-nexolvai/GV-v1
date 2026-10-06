"""Engine registration for approval-bound publication."""

from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from hatchet_sdk import Hatchet
    from hatchet_sdk.runnables.workflow import Workflow


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

    @workflow.task(name="publish_signed_exports")
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
