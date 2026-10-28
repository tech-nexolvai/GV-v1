"""Typed setting values: parse them, check their source, and store them as a parameter layer.

Moved here unchanged on 2026-10-28 from `app/api/measurements.py` (the old Measure screen's routes),
because the live Settings screens use them: company settings (`app/api/company_settings.py`) and a
project's sink (`app/api/project_sink.py`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Package, PackageRevision
from app.models.parameter_value_citations import ParameterValueCitation, ParameterValueCitationRun
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from app.models.parameters import from_rows, to_rows
from app.schemas.measurements import (
    StoredValue,
)
from app.verdicts.rulebook import snapshot_store
from rules.parameter_sources import SOURCE_GUIDANCE, allowed_sources
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity, Rule
from units.measurement import Measurement
from units.normalise import UnitNormalisationError, normalise_to_inches, typed_fraction_refusal
from workflow.measurements import LIST_MARKER

#: What every refusal says, matching `app/api/packages.py`: nothing about the project or the package.
NOT_FOUND_DETAIL: Final = "Not found"


def _parse(value: str, *, field: str) -> Measurement:
    """One typed token as an exact measurement in inches, or a 422 saying what was wrong.

    `normalise_to_inches` refuses a token with no unit, and that refusal is inherited on purpose: a
    bare `984` was once stored as 984 inches because tokenisation had removed its `mm` (#483). A
    reviewer who omits the mark is told, rather than having one chosen for them.

    A fraction whose top is not smaller than its bottom is refused too (#1199): `163/8"` was stored
    as 20 3/8. The message suggests the split (`did you mean 16 3/8"?`); the person types it again.
    """
    odd = typed_fraction_refusal(value)
    if odd is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{field}: {odd}"
        )
    try:
        return normalise_to_inches(value)
    except UnitNormalisationError as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f'{field}: {refused}. Give the value with its unit — 25 1/2", 610 mm — because a '
                "number with no unit cannot be converted and must not be guessed at."
            ),
        ) from refused


def _revision(session: Session, project_id: UUID, package_id: UUID) -> PackageRevision:
    """This package's current revision, or 404 in the same words for absent and forbidden."""
    revision = session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return revision


def _source(name: str, given: Provenance | None) -> Provenance:
    """The source a setting is recorded with, or a 422 saying what the setting takes (#827).

    **Refused rather than defaulted.** A setting the table does not know has no honest source, so
    recording it as `Measured` — what every setting got before #827 — would be a guess dressed as a
    fact. A setting that allows one source needs no answer; one that allows several needs the
    reviewer's, because which of them is true is a fact about this job the system cannot know.
    """
    allowed = allowed_sources(name)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{name!r} is not a setting any published check uses, so it has no source to "
                "record. Check the name against the form."
            ),
        )
    choices = " or ".join(f"{source.value!r}" for source in allowed)
    if given is None:
        if len(allowed) == 1:
            return allowed[0]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Say where {name!r} came from: {choices}.",
        )
    if given not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{name!r} cannot come from {given.value!r}; it takes {choices}. "
                f"{' '.join(SOURCE_GUIDANCE[source] for source in allowed)}"
            ),
        )
    return given


@dataclass(frozen=True, slots=True)
class _CitedPassage:
    """The passage a stored value was typed from and matched, as `parameter_value_citations`
    records it (#866)."""

    proposal_id: UUID
    document_version_id: UUID
    document_sha256: str
    page_id: UUID
    candidate_ids: tuple[UUID, ...]


def _record_citations(
    session: Session,
    rows: list[StoredParameterValue],
    *,
    fresh: Mapping[str, _CitedPassage],
    carried_from: Mapping[str, UUID],
) -> None:
    """Store each new value's passage beside it, and copy an earlier value's with it when carried.

    `carried_from` names the earlier row each carried value was copied from (#799). Its citation is
    copied too, so the version the checks read still says where the number came from.
    """
    earlier: dict[UUID, _CitedPassage] = {}
    if carried_from:
        for citation in session.scalars(
            select(ParameterValueCitation).where(
                ParameterValueCitation.parameter_value_id.in_(list(carried_from.values()))
            )
        ):
            earlier[citation.parameter_value_id] = _CitedPassage(
                proposal_id=citation.parameter_proposal_id,
                document_version_id=citation.document_version_id,
                document_sha256=citation.document_sha256,
                page_id=citation.page_id,
                candidate_ids=tuple(
                    session.scalars(
                        select(ParameterValueCitationRun.candidate_id)
                        .where(ParameterValueCitationRun.citation_id == citation.id)
                        .order_by(ParameterValueCitationRun.position)
                    )
                ),
            )
    pending: list[tuple[ParameterValueCitation, tuple[UUID, ...]]] = []
    for row in rows:
        if row.name in fresh:
            passage = fresh[row.name]
        elif row.name in carried_from and carried_from[row.name] in earlier:
            passage = earlier[carried_from[row.name]]
        else:
            continue
        citation = ParameterValueCitation(
            parameter_value_id=row.id,
            parameter_proposal_id=passage.proposal_id,
            document_version_id=passage.document_version_id,
            document_sha256=passage.document_sha256,
            page_id=passage.page_id,
        )
        pending.append((citation, passage.candidate_ids))
    if not pending:
        return
    # No relationship ties these tables to `parameter_values`, so the unit of work does not order
    # their inserts by foreign key: each row is flushed before the rows that point at it.
    session.flush()
    session.add_all(citation for citation, _ in pending)
    session.flush()
    session.add_all(
        ParameterValueCitationRun(citation_id=citation.id, candidate_id=candidate_id, position=at)
        for citation, candidate_ids in pending
        for at, candidate_id in enumerate(candidate_ids)
    )


def _store(
    session: Session,
    *,
    project_id: UUID | None,
    layer: ParameterLayer,
    values: dict[str, Measurement],
    typed: dict[str, str],
    actor: str,
    carry_forward: bool,
    package_revision_id: UUID | None = None,
    provenance: Provenance = Provenance.MEASURED,
    sources: Mapping[str, tuple[Provenance, str | None]] | None = None,
    citations: Mapping[str, _CitedPassage] | None = None,
) -> tuple[int | None, tuple[StoredValue, ...]]:
    """Persist one layer's values, reusing an identical set rather than minting a second.

    **With `carry_forward`, the new version keeps every earlier value this request does not set**
    (#799). The checks read only the latest version of a layer, and the form sends only the fields
    it has filled, so a version holding only what was sent erased every other setting: a reviewer
    who corrected the side thickness on Tuesday lost Monday's depth and overhang. A carried value is
    the earlier `ParameterValue` itself — its who, when and provenance unchanged — so the record
    still says who set each number. A RUN set belongs to one package revision (#801), so carrying it
    forward stays within that package: `package_revision_id` names it, and is required for RUN.
    The one exception is a many-valued input sent again: its new list replaces the old one whole
    (#1081), so a shorter run leaves no stale entries behind.

    **A re-submission mints a new version, and that is by design rather than a shortcoming.**
    `ParameterSet.set_id` puts `set_at` *inside* the content hash deliberately —
    `rules/parameters.py` explains why: "two sets recording the same number measured on different
    days are genuinely different records, and collapsing them would lose the distinction a reviewer
    needs." So the same numbers typed twice are two records, not one, and a finding cites the version
    that judged it (ADR-0016). Rewriting a set in place would change what an already-recorded finding
    claims to have used.

    The `set_id` lookup below is therefore a narrow guard, not the normal path: it catches a repeat
    that lands within the same recorded instant, which a fast client retry can produce. Saying so
    plainly because a comment claiming it deduplicates ordinary re-submissions would be false — they
    are supposed to become new versions.

    **`citations` are the passages values were typed from and matched (#866)**, stored beside them.
    A carried value takes its earlier citation with it.
    """
    if not values:
        return None, ()

    now = datetime.now(UTC)
    next_version = (
        session.execute(
            select(func.coalesce(func.max(StoredParameterSet.version), 0)).where(
                StoredParameterSet.project_id == project_id,
                StoredParameterSet.layer == layer.value,
            )
        ).scalar_one()
        + 1
    )
    carried: dict[str, ParameterValue] = {}
    carried_from: dict[str, UUID] = {}
    if carry_forward:
        previous = session.execute(
            select(StoredParameterSet)
            .where(
                StoredParameterSet.project_id == project_id,
                StoredParameterSet.layer == layer.value,
                # Within one review for RUN (#801); `IS NULL` for the other layers, which name none.
                (
                    StoredParameterSet.package_revision_id.is_(None)
                    if package_revision_id is None
                    else StoredParameterSet.package_revision_id == package_revision_id
                ),
            )
            .order_by(StoredParameterSet.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if previous is not None:
            rows = list(
                session.execute(
                    select(StoredParameterValue).where(
                        StoredParameterValue.parameter_set_id == previous.id
                    )
                ).scalars()
            )
            # **A resent list replaces the whole earlier list (#1081)**, so four cabinets saved
            # again as two are two: carrying `#2` and `#3` forward would hand the checks a width
            # the reviewer removed. Only a list sent in this save is replaced; one left out keeps
            # its entries, as every other value does. Nothing is deleted — the earlier version
            # still holds all four.
            resent = {name.partition(LIST_MARKER)[0] for name in values if LIST_MARKER in name}
            carried = {
                name: value
                for name, value in from_rows(previous, rows).parameters.items()
                if name not in values and name.partition(LIST_MARKER)[0] not in resent
            }
            carried_from = {row.name: row.id for row in rows if row.name in carried}

    chosen = dict(sources or {})
    cited = dict(citations or {})
    parameters = ParameterSet(
        # `None` for the company layer (#812), which belongs to no project.
        project_id=None if project_id is None else str(project_id),
        layer=layer,
        version=next_version,
        parameters={
            **carried,
            **{
                name: ParameterValue(
                    value=Quantity(value=measurement.exact, unit=measurement.unit),
                    # The source the reviewer named for a setting (#827), checked against what the
                    # setting allows before it reached here; otherwise `provenance` — MEASURED for a
                    # dimension a person read, COMPANY_STANDARD for the company layer (#812). Every
                    # member of `Provenance` is a person's or the rulebook's; none is one a model could
                    # claim, which is what keeps a model's number out of here — not a check in this
                    # module.
                    provenance=chosen.get(name, (provenance, None))[0],
                    set_by=actor,
                    set_at=now,
                    reference=chosen.get(name, (provenance, None))[1],
                )
                for name, measurement in values.items()
            },
        },
    )

    existing = session.execute(
        select(StoredParameterSet).where(StoredParameterSet.set_id == parameters.set_id)
    ).scalar_one_or_none()
    if existing is not None:
        version = existing.version
    else:
        stored, rows = to_rows(parameters, package_revision_id=package_revision_id)
        session.add(stored)
        for row in rows:
            session.add(row)
        _record_citations(session, rows, fresh=cited, carried_from=carried_from)
        version = next_version

    return version, tuple(
        StoredValue(
            name=name,
            numerator=str(measurement.exact.numerator),
            denominator=str(measurement.exact.denominator),
            unit=measurement.unit.value,
            as_typed=typed[name],
            source=chosen[name][0].value if name in chosen else None,
            reference=chosen[name][1] if name in chosen else None,
            citation=cited[name].proposal_id if name in cited else None,
        )
        for name, measurement in sorted(values.items())
    )


def _published_rules(session: Session) -> list[Rule]:
    """The rulebook as published, which is what `run_checks` reads.

    Shared by the form and by the submission that answers it, so a category the form offered cannot
    be one the submission refuses.
    """
    store = snapshot_store(session)
    return [
        snapshot.rule
        for snapshot in (store.latest(rule_id) for rule_id in store.rule_ids())
        if snapshot is not None
    ]
