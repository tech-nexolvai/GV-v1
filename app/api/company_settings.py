"""GV's company standards: the numbers every project starts from, set once (#812).

Many of the rulebook's parameters are GV's house rules — the usual cabinet depth, a side panel's
thickness, the sink clearance, the filler and cabinet bounds — the same on every job and on no drawing.
Until now nothing could record them company-wide: each project typed them again, or its checks said
NOT_FOUND. Recorded here once, they apply to every project; a project or a run still overrides one,
and the override report (Q10) shows where.

**What is listed: GV's standards, and only those** (#817, the admin's decision of 2026-10-02). A company
standard is a parameter the rulebook gives a default — the back-offset minimum, the sink's front offset
and clearance, the filler bounds, and since #991 the field cut: 1" per wall end, the client lead's "1" is typical,
customizable per project" (Q1), adopted as GV's standard by the admin on 2026-10-07, so an admin can
change it here for every job and a project still overrides it. The rest are per project by the client lead's own
checklist ("Specified · G.C / Client · Project Specific": side panel, overhang, backsplash; the cabinet
depth, carcass plus door; the six cabinet width bounds, mandatory entries per job), and they are, by
design, the ones with no default. A company value for one of those would apply to every job a number GV
does not set.
Run scope is left out too: a sink's cut-sheet size is true for one review.

**And, since #1181, four company defaults for per-project settings** (Anant, 2026-10-11, replacing
the 2026-10-02 line above for these four): the countertop overhang, the backsplash thickness, the
cabinet side panel thickness and the cabinet depth. On every real set the checks reading them waited,
so GV may set the number it usually works to once. **Empty until GV sets them:** the rulebook gives
none, nothing is filled in for GV, and a check with neither a company default nor a project value
still says it is waiting. A value typed for a project wins over the company default, and the changed
values report then lists it against the default it set aside. Listed as `kind: project_default`.

**How a save is stored.** As a new version of the company (GLOBAL) layer that carries forward every
earlier company value it does not set — the rule #799 gave the project layer — so saving the cabinet
depth never removes the side thickness. Provenance `COMPANY_STANDARD`, with the person who saved it.
The rulebook's own defaults stay beneath, and a company value replaces only the default of its own
name (`workflow/stages._layered`).

**Who.** Anyone signed in may read; only an admin may save (`Action.MANAGE_COMPANY_STANDARDS`): a
company standard changes every future verdict on every project. Since #1369 the read says whether the
caller may save (`can_save`), from the same checks the save route makes, so the page shows read-only
values to everyone else. It only hides controls: every save is still checked by the server.

## Projects (#1341)

Anant decided on 2026-10-15 that a project's own values live here too, under **Projects**, so the
review never has to ask for a setting: each project the caller belongs to, every setting the
published checks read for it (its overrides, the cabinet width limits on a cabinet job), the value
in use and where it comes from (the checklist default in the rulebook, GV's standard, or the
project's own value), and the project's sink card (`app/api/project_sink.py`).

**Reviewers may save these** (`Action.ENTER_VALUES`, as on Measurements); GV's standards above stay
admin-only. A save goes into a new version of the same PROJECT layer Measurements writes
(`measurements._store` with carry-forward), with the same source rules (#827), and is audited.
No new table. The sink's two inside sizes are refused here: the sink card saves them with the
sink's name. A value typed for one package on Measurements (the RUN layer) still wins for that
package.

Source: issue #812 (#798 Step 1), #1341. Verification: tests/api/test_company_settings.py,
tests/api/test_project_settings.py.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.project_sink import DEPTH, WIDTH, SinkEntryOut
from app.api.project_sink import _read as _read_sink
from app.api.rules import READERS
from app.api.setting_values import _parse, _published_rules, _source, _store
from app.audit.events import AuditCategory, emit
from app.auth import Action, Principal, require_action, require_project_access, require_role
from app.models import Package, Project
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.models.parameters import declared_defaults, from_rows
from app.review.setting_sizes import refused_size
from app.review.sizes import typed_negative
from app.schemas.review import optional_in_schema
from rules.parameter_sources import SOURCE_GUIDANCE, allowed_sources
from rules.parameters import ParameterLayer, ParameterValue, Provenance, is_rulebook_default
from rules.required_inputs import BLOCKED_PARAMETERS
from rules.schema import ParameterScope, Rule
from units.imperial import format_inches
from units.measurement import Measurement
from vocabulary.setting_names import setting_name

router = APIRouter(tags=["company settings"])

#: The scopes a company standard may be recorded for. RUN is one review's, never the company's.
_COMPANY_SCOPES = (ParameterScope.GLOBAL, ParameterScope.PROJECT)

#: Per-project settings GV may give a company default for (#1181). The rulebook gives them none, so
#: each is empty until an admin saves one here; a project's own value always wins over it.
PROJECT_DEFAULTS: Final = frozenset(
    {"countertop_overhang", "backsplash_thickness", "cabinet_side_thickness", "cabinet_depth"}
)


class CompanySettingOut(BaseModel):
    """One parameter, as an admin needs to see it to decide GV's standard."""

    name: str
    scope: Literal["global", "project"]
    rule_ids: list[str] = Field(description="The published checks that use it.")
    rulebook_default: str | None = Field(
        description="The rule author's default, `2 1/2 in`, or null where the rulebook gives none."
    )
    rulebook_note: str | None = Field(
        description="What the rulebook default rests on and any doubt about it (#674)."
    )
    company_value: str | None = None
    company_set_by: str | None = None
    company_set_at: datetime | None = None
    in_use: str | None = Field(
        default=None,
        description=(
            "What a check starts from on a project that sets nothing: the company value, else the "
            "rulebook default, else nothing — and then the check says NOT_FOUND."
        ),
    )
    in_use_from: Literal["company", "rulebook"] | None = None
    kind: Literal["standard", "project_default"] = Field(
        default="standard",
        json_schema_extra=optional_in_schema,
        description=(
            "`standard`: one of GV's standards. `project_default`: a per-project setting GV may "
            "give a default for (#1181); a project's own value wins over it."
        ),
    )


class CompanySettingsOut(BaseModel):
    settings: list[CompanySettingOut]
    version: int | None = Field(
        default=None, description="The company layer's current version, or null before the first."
    )
    can_save: bool = Field(
        description=(
            "True when the caller may save GV's standards (admins only). Read-only: the save route "
            "still checks it."
        )
    )


class CompanyValueIn(BaseModel):
    name: str
    value: str = Field(description='With its unit — `24"`, `610 mm`. A bare number is refused.')


class CompanySettingsIn(BaseModel):
    values: list[CompanyValueIn] = Field(min_length=1)


#: A bare number, such as `24` or `2 1/2`: typed without its unit (#1394).
_BARE_NUMBER: Final = re.compile(r"^\s*\d+(?:\.\d+)?(?:\s+\d+/\d+)?\s*$|^\s*\d+/\d+\s*$")


def _parse_setting(value: str, *, label: str) -> Measurement:
    """`_parse`, refused in plain words a person can act on (#1394): a negative value "must be more
    than 0", a bare number "Add the unit", and never the parser's own code-level message."""
    if typed_negative(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{label} must be more than 0.",
        )
    if _BARE_NUMBER.match(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'{label}: Add the unit, e.g. 24".',
        )
    try:
        return _parse(value, field=label)
    except HTTPException as refused:
        if "unmarked_unit" in str(refused.detail) or "Unit." in str(refused.detail):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f'{label}: this value could not be read. Type inches, e.g. 24".',
            ) from refused
        raise


def _text(value: ParameterValue) -> str:
    quantity = value.value
    return f"{format_inches(quantity.exact_value)} {quantity.unit}"


def _company_set(session: Session) -> tuple[int | None, dict[str, ParameterValue]]:
    """The company layer's latest version and its values, or `(None, {})` before the first."""
    stored = session.execute(
        select(StoredParameterSet)
        .where(
            StoredParameterSet.layer == ParameterLayer.GLOBAL.value,
            StoredParameterSet.project_id.is_(None),
        )
        .order_by(StoredParameterSet.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if stored is None:
        return None, {}
    rows = list(
        session.execute(
            select(StoredParameterValue).where(StoredParameterValue.parameter_set_id == stored.id)
        ).scalars()
    )
    return stored.version, dict(from_rows(stored, rows).parameters)


def _declared(rules: list[Rule]) -> dict[str, tuple[ParameterScope, list[str]]]:
    """Every company standard the rules declare — a default, at company or project scope — and the
    per-project settings GV may give a default (#1181), with the checks that use each."""
    declared: dict[str, tuple[ParameterScope, list[str]]] = {}
    for rule in rules:
        for name, parameter in rule.parameters.items():
            if parameter.scope not in _COMPANY_SCOPES:
                continue
            if parameter.default is None and name not in PROJECT_DEFAULTS:
                continue
            _scope, users = declared.setdefault(name, (parameter.scope, []))
            users.append(rule.id)
    return declared


def may_save_company(principal: Principal) -> bool:
    """The check `save_company_settings` makes (`require_action`), asked without refusing (#1369)."""
    return principal.may(Action.MANAGE_COMPANY_STANDARDS)


def may_save_project(principal: Principal, project_id: UUID) -> bool:
    """The two checks `save_project_settings` makes — project access and `Action.ENTER_VALUES` —
    asked without refusing (#1369)."""
    return principal.belongs_to(project_id) and principal.may(Action.ENTER_VALUES)


def _settings(session: Session, principal: Principal) -> CompanySettingsOut:
    rules = _published_rules(session)
    version, company = _company_set(session)
    defaults = declared_defaults(rules, when=datetime.now(UTC)).parameters
    notes = {
        name: parameter.note
        for rule in rules
        for name, parameter in rule.parameters.items()
        if parameter.note is not None
    }
    settings = []
    for name, (scope, rule_ids) in sorted(_declared(rules).items()):
        default = defaults.get(name)
        recorded = company.get(name)
        in_use = recorded if recorded is not None else default
        settings.append(
            CompanySettingOut(
                name=name,
                scope="global" if scope is ParameterScope.GLOBAL else "project",
                rule_ids=sorted(rule_ids),
                rulebook_default=None if default is None else _text(default),
                rulebook_note=notes.get(name),
                company_value=None if recorded is None else _text(recorded),
                company_set_by=None if recorded is None else recorded.set_by,
                company_set_at=None if recorded is None else recorded.set_at,
                in_use=None if in_use is None else _text(in_use),
                in_use_from=(
                    None
                    if in_use is None
                    else "rulebook" if is_rulebook_default(in_use) else "company"
                ),
                kind="project_default" if name in PROJECT_DEFAULTS else "standard",
            )
        )
    return CompanySettingsOut(
        settings=settings, version=version, can_save=may_save_company(principal)
    )


@router.get(
    "/company-settings",
    response_model=CompanySettingsOut,
    summary="GV's standard numbers, which every project starts from",
)
def read_company_settings(
    principal: Annotated[Principal, Depends(require_role(*READERS))],
    session: Annotated[Session, Depends(get_session)],
) -> CompanySettingsOut:
    return _settings(session, principal)


@router.post(
    "/company-settings",
    response_model=CompanySettingsOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record GV's standard numbers; every value not sent stays as it was",
)
def save_company_settings(
    principal: Annotated[Principal, Depends(require_action(Action.MANAGE_COMPANY_STANDARDS))],
    session: Annotated[Session, Depends(get_session)],
    body: CompanySettingsIn,
) -> CompanySettingsOut:
    """A new version of the company layer: these values, and every earlier one not sent.

    A name the published rules do not declare at global or project scope is refused rather than
    stored: a misspelt standard would be a number no check ever reads, saved as though it mattered.
    """
    declared = _declared(_published_rules(session))
    values = {}
    typed = {}
    for entry in body.values:
        if entry.name not in declared:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{entry.name!r} is not a company standard: it is a per-project setting, "
                    "entered for each job, or not a setting any published check uses."
                ),
            )
        values[entry.name] = _parse_setting(entry.value, label=setting_name(entry.name))
        # A default cabinet depth or side panel of zero is a mistake, never a value (#1181).
        too_small = refused_size(entry.name, values[entry.name], label=setting_name(entry.name))
        if too_small is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=too_small)
        typed[entry.name] = entry.value

    version, _stored = _store(
        session,
        project_id=None,
        layer=ParameterLayer.GLOBAL,
        values=values,
        typed=typed,
        actor=principal.id,
        carry_forward=True,
        provenance=Provenance.COMPANY_STANDARD,
    )
    saved = session.execute(
        select(StoredParameterSet).where(
            StoredParameterSet.layer == ParameterLayer.GLOBAL.value,
            StoredParameterSet.project_id.is_(None),
            StoredParameterSet.version == version,
        )
    ).scalar_one()
    # Audited in the same transaction, like every action that changes what a check decides.
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=principal.id,
        target_id=saved.id,
        target_type="company_standards",
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return _settings(session, principal)


# ---------------------------------------------------------------------------------------------------
# Projects (#1341): each project's own values, editable by its reviewers
# ---------------------------------------------------------------------------------------------------

#: The sink's inside sizes: saved by the project's sink card with the sink's name, never here.
_SINK_SIZES: Final = frozenset({WIDTH, DEPTH})

#: What each setting is for, in a sentence a new reviewer can act on. A setting missing here shows
#: no sentence rather than a guessed one; `test_every_value_the_review_settings_step_asks_for_is_listed`
#: holds the table complete over the published rules.
PURPOSES: Final[dict[str, str]] = {
    "back_offset_minimum": "The least stone allowed behind the sink cut-out, to the back edge.",
    "front_offset_required": "How far the sink cut-out sits from the countertop's front edge.",
    "sink_cutout_clearance": (
        "Taken off each side of the sink's inside size to get the cut-out; set by the fabricator."
    ),
    "filler_min": "The narrowest filler strip allowed between cabinets or at a wall.",
    "filler_max": "The widest filler strip allowed before a cabinet must change size instead.",
    "field_cut": "Extra stone left at each wall end, trimmed on site to fit the wall.",
    "countertop_overhang": "How far the countertop sticks out past the cabinet front.",
    "backsplash_thickness": "The backsplash's thickness, which takes up countertop depth at the wall.",
    "cabinet_side_thickness": "The thickness of a cabinet's side panel, for the sink cabinet's inside.",
    "cabinet_depth": "Cabinet box plus door, front to back; the countertop depth starts from it.",
    "single_door_cab_width_min": "The narrowest single-door cabinet this job allows.",
    "single_door_cab_width_max": "The widest single-door cabinet this job allows.",
    "double_door_cab_width_min": "The narrowest double-door cabinet this job allows.",
    "double_door_cab_width_max": "The widest double-door cabinet this job allows.",
    "drawer_cab_width_min": "The narrowest drawer cabinet this job allows.",
    "drawer_cab_width_max": "The widest drawer cabinet this job allows.",
}


class CheckOut(BaseModel):
    id: str
    name: str = Field(description="The check's plain name, as the rulebook gives it.")


class SourceChoiceOut(BaseModel):
    value: str
    guidance: str


class ProjectValueOut(BaseModel):
    """The project's own value for one setting, and who set it."""

    text: str
    set_by: str
    set_at: datetime
    source: str = Field(description="Where the person said it came from (#827).")
    reference: str | None


class ProjectSettingOut(BaseModel):
    """One setting as a project uses it: what it is for, which checks read it, and the value in use."""

    name: str
    label: str = Field(description="The setting's plain name.")
    purpose: str | None = Field(description="What the setting is for, in one sentence.")
    checks: list[CheckOut] = Field(description="The published checks that read it.")
    applies: bool = Field(
        description=(
            "True when a check for one of this project's products reads it (or the project has no "
            "drawing set with a stated product yet). False: still settable, but no drawing set of "
            "this project is checked with it — the cabinet width limits on a countertop-only job."
        )
    )
    cabinet_width_limit: bool = Field(description="One of the six cabinet width limits.")
    sources: list[SourceChoiceOut] = Field(
        description="Where a value may honestly come from; the person picks when there are several."
    )
    rulebook_default: str | None = Field(
        description="The default the rulebook carries from GV's checklist, or null."
    )
    company_value: str | None = Field(description="GV's standard, or null where GV set none.")
    project_value: ProjectValueOut | None
    in_use: str | None = Field(
        description="What this project's checks use now, or null: those checks then wait."
    )
    in_use_from: Literal["rulebook", "company", "project"] | None


class ProjectSettingsOut(BaseModel):
    id: UUID
    name: str
    product_types: list[str] = Field(
        description="The products this project's drawing sets are for, as stated at upload."
    )
    version: int | None = Field(description="The project layer's current version, or null.")
    settings: list[ProjectSettingOut]
    sink: SinkEntryOut | None = Field(description="The project's sink card, or null until entered.")
    can_save: bool = Field(
        description=(
            "True when the caller may save this project's values (its reviewers and admins). "
            "Read-only: the save route still checks it."
        )
    )


class ProjectsSettingsOut(BaseModel):
    projects: list[ProjectSettingsOut]


class ProjectValueIn(BaseModel):
    name: str
    value: str = Field(description='With its unit — `1"`, `25 mm`. A bare number is refused.')
    source: Provenance | None = Field(
        default=None, description="Needed only where the setting allows more than one source."
    )
    reference: str | None = Field(default=None, max_length=200)


class ProjectValuesIn(BaseModel):
    values: list[ProjectValueIn] = Field(min_length=1)


def _project_declared(rules: list[Rule]) -> dict[str, tuple[list[Rule], ParameterScope]]:
    """Every setting a project may hold: the rules' parameters, less run-only ones and blocked ones."""
    declared: dict[str, tuple[list[Rule], ParameterScope]] = {}
    for rule in rules:
        for name, parameter in rule.parameters.items():
            if name in BLOCKED_PARAMETERS or name in _SINK_SIZES:
                continue
            if parameter.scope not in _COMPANY_SCOPES:
                continue
            users, _scope = declared.setdefault(name, ([], parameter.scope))
            users.append(rule)
    return declared


def _project_set(
    session: Session, project_id: UUID
) -> tuple[int | None, dict[str, ParameterValue], dict[str, StoredParameterValue]]:
    """The project layer's newest version, its values, and their stored rows (for who and source)."""
    stored = session.execute(
        select(StoredParameterSet)
        .where(
            StoredParameterSet.layer == ParameterLayer.PROJECT.value,
            StoredParameterSet.project_id == project_id,
        )
        .order_by(StoredParameterSet.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if stored is None:
        return None, {}, {}
    rows = list(
        session.scalars(
            select(StoredParameterValue).where(StoredParameterValue.parameter_set_id == stored.id)
        )
    )
    return stored.version, dict(from_rows(stored, rows).parameters), {row.name: row for row in rows}


def _project_settings(
    session: Session, project: Project, rules: list[Rule], principal: Principal
) -> ProjectSettingsOut:
    _version, company = _company_set(session)
    defaults = declared_defaults(rules, when=datetime.now(UTC)).parameters
    version, values, rows = _project_set(session, project.id)
    stated = session.scalars(
        select(Package.product_type).where(Package.project_id == project.id).distinct()
    ).all()
    products = sorted({product for product in stated if product is not None})
    # No stated product (no drawing set yet, or one from before #994) runs every product's rules.
    every_product = not products or None in stated
    settings = []
    for name, (users, _scope) in sorted(_project_declared(rules).items()):
        default, standard, own = defaults.get(name), company.get(name), values.get(name)
        in_use = own or standard or default
        row = rows.get(name)
        settings.append(
            ProjectSettingOut(
                name=name,
                label=setting_name(name),
                purpose=PURPOSES.get(name),
                checks=[CheckOut(id=rule.id, name=rule.name) for rule in users],
                applies=every_product or any(rule.product_type.value in products for rule in users),
                cabinet_width_limit="_cab_width_" in name,
                sources=[
                    SourceChoiceOut(value=source.value, guidance=SOURCE_GUIDANCE[source])
                    for source in allowed_sources(name)
                ],
                rulebook_default=None if default is None else _text(default),
                company_value=None if standard is None else _text(standard),
                project_value=(
                    None
                    if own is None or row is None
                    else ProjectValueOut(
                        text=_text(own),
                        set_by=row.set_by,
                        set_at=row.set_at,
                        source=row.provenance,
                        reference=row.source_reference,
                    )
                ),
                in_use=None if in_use is None else _text(in_use),
                in_use_from=(
                    "project"
                    if own is not None
                    else (
                        "company"
                        if standard is not None
                        else "rulebook" if default is not None else None
                    )
                ),
            )
        )
    return ProjectSettingsOut(
        id=project.id,
        name=project.name,
        product_types=products,
        version=version,
        settings=settings,
        sink=_read_sink(session, project.id).sink,
        can_save=may_save_project(principal, project.id),
    )


def store_project_values(
    session: Session,
    principal: Principal,
    project_id: UUID,
    values_in: list[ProjectValueIn],
    *,
    rules: list[Rule] | None = None,
) -> None:
    """Check and store a project's values as a new version of its layer, audited; never commits.

    The one path a project's own values are saved by: this page's save, and the simple review's
    "Enter this job's values" (#1403), which commits it with its re-check request. Everything is
    checked before anything is stored, as `save_project_settings` says.
    """
    if not values_in:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Type at least one value."
        )
    if rules is None:
        rules = _published_rules(session)
    declared = _project_declared(rules)
    values = {}
    typed = {}
    sources: dict[str, tuple[Provenance, str | None]] = {}
    for entry in values_in:
        if entry.name in _SINK_SIZES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{setting_name(entry.name)} is saved on the project's sink card, with the "
                    "sink's name, not here."
                ),
            )
        if entry.name not in declared:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{entry.name!r} is not a setting any published check reads for a project.",
            )
        label = setting_name(entry.name)
        values[entry.name] = _parse_setting(entry.value, label=label)
        too_small = refused_size(entry.name, values[entry.name], label=label)
        if too_small is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=too_small)
        reference = (entry.reference or "").strip() or None
        sources[entry.name] = (_source(entry.name, entry.source), reference)
        typed[entry.name] = entry.value

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
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=principal.id,
        target_id=saved,
        target_type="project_settings",
    )


@router.get(
    "/company-settings/projects",
    response_model=ProjectsSettingsOut,
    summary="Each of your projects' own settings, the value in use and where it comes from",
)
def read_project_settings(
    principal: Annotated[Principal, Depends(require_role(*READERS))],
    session: Annotated[Session, Depends(get_session)],
) -> ProjectsSettingsOut:
    """Only the projects the caller belongs to: every other project is invisible (ADR-0006)."""
    if not principal.projects:
        return ProjectsSettingsOut(projects=[])
    rules = _published_rules(session)
    projects = session.scalars(
        select(Project).where(Project.id.in_(principal.projects)).order_by(Project.name, Project.id)
    ).all()
    return ProjectsSettingsOut(
        projects=[_project_settings(session, project, rules, principal) for project in projects]
    )


@router.post(
    "/company-settings/projects/{project_id}",
    response_model=ProjectSettingsOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record a project's own values; every value not sent stays as it was",
)
def save_project_settings(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.ENTER_VALUES))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    body: ProjectValuesIn,
) -> ProjectSettingsOut:
    """A new version of the project's layer — the one Measurements writes — carrying the rest forward.

    Everything is checked before anything is stored: a name no published check reads, the sink's
    sizes (the sink card's), a bare number, a zero size or a missing source refuses the whole save.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    rules = _published_rules(session)
    store_project_values(session, principal, project_id, body.values, rules=rules)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return _project_settings(session, project, rules, principal)
