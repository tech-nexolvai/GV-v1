"""The project's sink: entered once, read by every sink check (#1181).

Three checks need the sink's own inside size, which is on its spec sheet and on no shop drawing
(`CLIENT_FACTS` Q7). The reviewer enters the sink once for the project: its name, inside width and
inside depth (Anant, 2026-10-12: only those; the front offset stays GV's standard, or a project
setting on Measurements). Every sink check of every package in the job then reads it.

**Saved through the project-values path, not beside it.** The numbers go into a new version of the
project's settings (`app/api/measurements.py:_store`), as `sink_interior_width` and
`sink_interior_depth`, each with who, when, its source and a reference naming the sink — the same
record, and the same layer the checks read, as any setting typed on Measurements. Every other project
value is carried into the new version unchanged. The sink's name and the note go in
`project_sinks`, beside the version they were saved as.

**Never a guess.** Width and depth are required, must carry a unit and must be more than zero. A
size typed for one package on Measurements is that package's and still wins there, so
`GET .../packages/{id}/sink-in-use` says which size a package's checks use, and where it is from.

**Who.** Anyone in the project may read it; reviewers and admins may save (`Action.ENTER_VALUES`),
as for every project value.

Source: issue #1181. Verification: `tests/api/test_waiting_values.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.setting_values import _parse, _published_rules, _revision, _source, _store
from app.auth import Action, Principal, require_action, require_project_access
from app.models import Project
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.models.project_sinks import (
    MODEL_NAME_MAX_LENGTH,
    SOURCE_NOTE_MAX_LENGTH,
    ProjectSink,
)
from app.review.depth_readings import vendor_sink_notes
from app.review.setting_sizes import refused_size
from app.review.settings_in_force import settings_in_force
from app.schemas.review import optional_in_schema
from rules.parameters import ParameterLayer, Provenance
from units.imperial import format_inches
from units.measurement import Measurement, Unit
from vocabulary.setting_names import setting_name

router = APIRouter(tags=["project sink"])

NOT_FOUND_DETAIL = "Not found"

#: The settings a sink entry writes, by the entry's own field.
WIDTH = "sink_interior_width"
DEPTH = "sink_interior_depth"


class SinkValueOut(BaseModel):
    """One of the sink's numbers as the project holds it now, and who set it."""

    text: str = Field(description='Exact, as a drawing writes it: `16 1/2"`.')
    set_by: str
    set_at: datetime


class SinkEntryOut(BaseModel):
    model_name: str
    source_note: str | None
    set_by: str
    set_at: datetime
    inside_width: SinkValueOut | None = Field(
        description="The project's inside width now; null only if it was never saved."
    )
    inside_depth: SinkValueOut | None


class ProjectSinkOut(BaseModel):
    sink: SinkEntryOut | None = Field(description="Null until a sink is entered for the project.")


class SinkEntryIn(BaseModel):
    model_name: str = Field(min_length=1, max_length=MODEL_NAME_MAX_LENGTH)
    inside_width: str = Field(description='With its unit: `16 1/2"`, `419 mm`.')
    inside_depth: str
    source_note: str | None = Field(default=None, max_length=SOURCE_NOTE_MAX_LENGTH)


def _value(row: StoredParameterValue | None) -> SinkValueOut | None:
    if row is None:
        return None
    exact = row.exact_value
    text = f'{format_inches(exact)}"' if row.unit == Unit.INCH.value else f"{exact} {row.unit}"
    return SinkValueOut(text=text, set_by=row.set_by, set_at=row.set_at)


def _read(session: Session, project_id: UUID) -> ProjectSinkOut:
    entry = session.execute(
        select(ProjectSink)
        .where(ProjectSink.project_id == project_id)
        .order_by(ProjectSink.created_at.desc(), ProjectSink.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if entry is None:
        return ProjectSinkOut(sink=None)
    # The numbers as the project holds them now, which is what the checks read: a later project save
    # carries them forward, so the newest version has them unless somebody changed one since.
    latest = session.execute(
        select(StoredParameterSet.id)
        .where(
            StoredParameterSet.layer == ParameterLayer.PROJECT.value,
            StoredParameterSet.project_id == project_id,
        )
        .order_by(StoredParameterSet.version.desc())
        .limit(1)
    ).scalar_one()
    rows = {
        row.name: row
        for row in session.scalars(
            select(StoredParameterValue).where(
                StoredParameterValue.parameter_set_id == latest,
                StoredParameterValue.name.in_((WIDTH, DEPTH)),
            )
        )
    }
    return ProjectSinkOut(
        sink=SinkEntryOut(
            model_name=entry.model_name,
            source_note=entry.source_note,
            set_by=entry.set_by,
            set_at=entry.created_at,
            inside_width=_value(rows.get(WIDTH)),
            inside_depth=_value(rows.get(DEPTH)),
        )
    )


def _project(session: Session, project_id: UUID) -> None:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)


def _size(name: str, typed: str) -> Measurement:
    """A sink size, with its unit and more than zero; never stored as zero (#1181)."""
    label = setting_name(name)
    value = _parse(typed, field=label)
    refused = refused_size(name, value, label=label)
    if refused is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=refused)
    return value


class SinkSizeInUseOut(BaseModel):
    """One sink size a package's checks use now, and where it comes from."""

    text: str
    origin: Literal["sink_card", "package"] = Field(
        description="`sink_card`: the project's sink. `package`: typed for this package only."
    )
    set_by: str | None
    set_at: datetime | None


class VendorSinkNoteOut(BaseModel):
    """The vendor's printed words naming a sink (#1295): a hint, never filled into a setting."""

    text: str = Field(description="As the vendor's drawing prints it.")
    page_number: int


class SinkInUseOut(BaseModel):
    """The sink a package's checks use now (#1181)."""

    model_name: str | None = Field(
        description="The sink card's name, when at least one size comes from it."
    )
    inside_width: SinkSizeInUseOut | None
    inside_depth: SinkSizeInUseOut | None
    vendor_notes: list[VendorSinkNoteOut] = Field(
        default_factory=list,
        json_schema_extra=optional_in_schema,
        description=(
            "What the vendor's drawing prints about the sink, on the pages the depth and sink reader "
            "read (#1295). **A hint only**: it is never filled into a setting, and the sink's inside "
            "size is still typed from its spec sheet. Empty when the reader never read the set."
        ),
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/sink-in-use",
    response_model=SinkInUseOut,
    summary="The sink size this package's checks use now, and where each number comes from",
)
def read_sink_in_use(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> SinkInUseOut:
    revision = _revision(session, project_id, package_id)
    in_force = settings_in_force(session, project_id, revision.id, _published_rules(session))

    def size(name: str) -> SinkSizeInUseOut | None:
        value = in_force.get(name)
        if value is None or value.origin not in ("sink_card", "package"):
            return None
        return SinkSizeInUseOut(
            text=value.text,
            origin="sink_card" if value.origin == "sink_card" else "package",
            set_by=value.set_by,
            set_at=value.set_at,
        )

    width, depth = size(WIDTH), size(DEPTH)
    from_card = any(item is not None and item.origin == "sink_card" for item in (width, depth))
    card = _read(session, project_id).sink if from_card else None
    return SinkInUseOut(
        model_name=None if card is None else card.model_name,
        inside_width=width,
        inside_depth=depth,
        vendor_notes=[
            VendorSinkNoteOut(text=note.text, page_number=note.page_number)
            for note in vendor_sink_notes(session, revision.id)
        ],
    )


@router.get(
    "/projects/{project_id}/sink",
    response_model=ProjectSinkOut,
    summary="The sink this project's sink checks use",
)
def read_project_sink(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
) -> ProjectSinkOut:
    _project(session, project_id)
    return _read(session, project_id)


@router.post(
    "/projects/{project_id}/sink",
    response_model=ProjectSinkOut,
    status_code=status.HTTP_201_CREATED,
    summary="Enter the project's sink; every sink check reads it once the checks run again",
)
def save_project_sink(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.ENTER_VALUES))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    body: SinkEntryIn,
) -> ProjectSinkOut:
    _project(session, project_id)
    model_name = body.model_name.strip()
    if not model_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Name the sink, as its spec sheet does.",
        )
    note = (body.source_note or "").strip() or None
    reference = f"Sink {model_name}" + (f": {note}" if note else "")

    values = {WIDTH: _size(WIDTH, body.inside_width), DEPTH: _size(DEPTH, body.inside_depth)}
    typed = {WIDTH: body.inside_width, DEPTH: body.inside_depth}
    # The sink's size is specified by the client through its spec sheet (`G.C / Client`, #827).
    sources: dict[str, tuple[Provenance, str | None]] = {
        WIDTH: (_source(WIDTH, Provenance.GC_CLIENT), reference),
        DEPTH: (_source(DEPTH, Provenance.GC_CLIENT), reference),
    }
    version, _stored = _store(
        session,
        project_id=project_id,
        layer=ParameterLayer.PROJECT,
        values=values,
        typed=typed,
        actor=principal.id,
        carry_forward=True,
        sources=sources,
    )
    session.flush()
    saved = session.execute(
        select(StoredParameterSet.id).where(
            StoredParameterSet.layer == ParameterLayer.PROJECT.value,
            StoredParameterSet.project_id == project_id,
            StoredParameterSet.version == version,
        )
    ).scalar_one()
    session.add(
        ProjectSink(
            project_id=project_id,
            model_name=model_name,
            source_note=note,
            parameter_set_id=saved,
            set_by=principal.id,
        )
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return _read(session, project_id)
