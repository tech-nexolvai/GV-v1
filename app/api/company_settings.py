"""GV's company standards: the numbers every project starts from, set once (#812).

Many of the rulebook's parameters are GV's house rules — the usual cabinet depth, a side panel's
thickness, the sink clearance, the filler and cabinet bounds — the same on every job and on no drawing.
Until now nothing could record them company-wide: each project typed them again, or its checks said
NOT_FOUND. Recorded here once, they apply to every project; a project or a run still overrides one,
and the override report (Q10) shows where.

**What is listed: GV's standards, and only those** (#817, the admin's decision of 2026-10-02). A company
standard is a parameter the rulebook gives a default — the back-offset minimum, the sink's front offset
and clearance, the filler bounds. The rest are per project by Raj's own checklist ("Specified · G.C /
Client · Project Specific": side panel, overhang, backsplash; the cabinet depth, carcass plus door; the
field cut; the six cabinet width bounds, mandatory entries per job), and they are, by design, the ones
with no default. A company value for one of those would apply to every job a number GV does not set.
Run scope is left out too: a sink's cut-sheet size is true for one review.

**How a save is stored.** As a new version of the company (GLOBAL) layer that carries forward every
earlier company value it does not set — the rule #799 gave the project layer — so saving the cabinet
depth never removes the side thickness. Provenance `COMPANY_STANDARD`, with the person who saved it.
The rulebook's own defaults stay beneath, and a company value replaces only the default of its own
name (`workflow/stages._layered`).

**Who.** Anyone signed in may read; only an admin may save (`Action.MANAGE_COMPANY_STANDARDS`): a
company standard changes every future verdict on every project.

Source: issue #812 (#798 Step 1). Verification: tests/api/test_company_settings.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.measurements import _parse, _published_rules, _store
from app.api.rules import READERS
from app.audit.events import AuditCategory, emit
from app.auth import Action, Principal, require_action, require_role
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.models.parameters import declared_defaults, from_rows
from rules.parameters import ParameterLayer, ParameterValue, Provenance, is_rulebook_default
from rules.schema import ParameterScope, Rule
from units.imperial import format_inches

router = APIRouter(tags=["company settings"])

#: The scopes a company standard may be recorded for. RUN is one review's, never the company's.
_COMPANY_SCOPES = (ParameterScope.GLOBAL, ParameterScope.PROJECT)


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


class CompanySettingsOut(BaseModel):
    settings: list[CompanySettingOut]
    version: int | None = Field(
        default=None, description="The company layer's current version, or null before the first."
    )


class CompanyValueIn(BaseModel):
    name: str
    value: str = Field(description='With its unit — `24"`, `610 mm`. A bare number is refused.')


class CompanySettingsIn(BaseModel):
    values: list[CompanyValueIn] = Field(min_length=1)


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
    """Every company standard the rules declare — a default, at company or project scope — with the
    checks that use it."""
    declared: dict[str, tuple[ParameterScope, list[str]]] = {}
    for rule in rules:
        for name, parameter in rule.parameters.items():
            if parameter.scope not in _COMPANY_SCOPES or parameter.default is None:
                continue
            _scope, users = declared.setdefault(name, (parameter.scope, []))
            users.append(rule.id)
    return declared


def _settings(session: Session) -> CompanySettingsOut:
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
            )
        )
    return CompanySettingsOut(settings=settings, version=version)


@router.get(
    "/company-settings",
    response_model=CompanySettingsOut,
    summary="GV's standard numbers, which every project starts from",
)
def read_company_settings(
    _principal: Annotated[Principal, Depends(require_role(*READERS))],
    session: Annotated[Session, Depends(get_session)],
) -> CompanySettingsOut:
    return _settings(session)


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
        values[entry.name] = _parse(entry.value, field=entry.name)
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
    return _settings(session)
