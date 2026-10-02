"""A check that compares runs member by member never gets them in labelling order (#794, #833).

Verification for: `workflow/evidence_operands.py` (`position_sensitive_inputs`, the evidence path)
and the reason `DatabaseStages.run_checks` gives when such a check abstains.

Two tests matter most, one for each check that compares by position. The first is
`test_two_swapped_cabinets_labelled_in_another_order_never_pass`: the false PASS #794 found, end to
end. The architect's drawing has cabinet A at 24" and B at 36"; the vendor's has them the other way
round, so both are wrong. A reviewer labels the architect's A then B and the vendor's B then A.
Paired by labelling order that is two matches; it must never be PASS.

The second is `test_a_run_with_two_cabinets_swapped_never_passes_the_filler_check`: the same swap
through `CAB-FILLER-001` (#833), with every reading of both runs labelled and everything else the
check needs entered on the form. Before #833 that was a PASS.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.documents import storage_key
from app.db.session import session_factory
from app.evidence.confirm import ConfirmationRefused, confirm_candidate_type
from app.models import (
    Document,
    DocumentVersion,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Project,
    SourceArtifact,
)
from app.models.parameters import to_rows
from app.models.rules import RuleSnapshot
from app.models.runs import TaskRun, WorkflowRun
from app.models.verdicts import CheckRun, Finding
from app.verdicts.rulebook import from_row
from rules.derivations import Derivation
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity, Rule
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.extraction.test_reader import _pdf
from tests.workflow.test_stages import RULEBOOK, _publish_rulebook
from units.measurement import Measurement, Unit
from verdict.operations import POSITION_SENSITIVE_OPERATIONS
from verdict.operations.pairwise import pairwise_within_tolerance
from verdict.outcomes import Outcome
from workflow.classifications import record_classifications
from workflow.evidence_operands import (
    IDENTIFIER_PAIRING_WITHHELD,
    RUN_ORDER_WITHHELD,
    WITHHELD_FOR_ORDER,
    operands_from_evidence,
    position_sensitive_inputs,
)
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

RULE = "CAB-ARCH-VS-SHOP-001"
FILLER_RULE = "CAB-FILLER-001"


def _rules() -> list[Rule]:
    return [
        Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(RULEBOOK.glob("*.yaml"))
    ]


def _inch(value: int) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, f'{value}"')


# ---------------------------------------------------------------------------
# Which inputs are paired — read from the rules, no database
# ---------------------------------------------------------------------------


def test_position_pairing_passes_two_swapped_cabinets_and_identifier_pairing_does_not() -> None:
    """**Why the rule exists**, on the operation itself: the same four numbers, PASS by position and
    FAIL by identifier. The operation is right for both inputs; the evidence path must not hand it a
    list whose order nobody stated."""
    by_position = pairwise_within_tolerance(
        left=[_inch(24), _inch(36)], right=[_inch(24), _inch(36)], tolerance=_inch(0)
    )
    by_identifier = pairwise_within_tolerance(
        left={"A": _inch(24), "B": _inch(36)},
        right={"A": _inch(36), "B": _inch(24)},
        tolerance=_inch(0),
    )

    assert (by_position.outcome, by_identifier.outcome) == (Outcome.PASS, Outcome.FAIL)


def test_the_rulebook_compares_by_position_in_exactly_two_checks() -> None:
    """Outcome: `CAB-ARCH-VS-SHOP-001`'s two cabinet runs and `CAB-FILLER-001`'s four runs, each
    with its own check's sentence, and no other rule's inputs. A sum of cabinets is indifferent to
    order, so `CT-WIDTH-001` keeps its runs.

    The filler check's site width, design width and cabinet classifications are not on the list. A
    design width is one reading, with no order to get wrong; the other two are typed by the
    reviewer, and a sentence about labelled readings would not be true of them.
    """
    ordered = {rule.id: position_sensitive_inputs(rule) for rule in _rules()}

    assert ordered.pop(RULE) == dict.fromkeys(
        ("architectural_cabinets", "shop_cabinets"), IDENTIFIER_PAIRING_WITHHELD
    )
    assert ordered.pop(FILLER_RULE) == dict.fromkeys(
        ("architectural_fillers", "shop_fillers", "architectural_cabinets", "shop_cabinets"),
        RUN_ORDER_WITHHELD,
    )
    assert all(inputs == {} for inputs in ordered.values()), ordered


def test_a_rule_that_pairs_through_a_derivation_is_caught_too() -> None:
    """Outcome: inputs bound to a pairing derivation are withheld, a tuple binding included."""
    (rule,) = [rule for rule in _rules() if rule.id == RULE]
    through_a_derivation = rule.model_copy(
        update={
            "derivations": (
                Derivation.model_construct(
                    name="pairs",
                    operation="pairwise_within_tolerance",
                    operands={"left": "architectural_cabinets", "right": ("shop_cabinets",)},
                ),
            ),
            "operation": rule.operation.model_copy(update={"type": "equals", "operands": {}}),
        }
    )

    assert set(position_sensitive_inputs(through_a_derivation)) == {
        "architectural_cabinets",
        "shop_cabinets",
    }


def test_every_position_sensitive_operation_has_a_sentence_of_its_own() -> None:
    """**The drift guard for the reviewer's sentence.** An operation declared without one would
    raise the first time a rule used it, and take the package's other checks down with it; one
    sharing another check's sentence would tell the reviewer something untrue about why their labels
    were not used."""
    assert set(WITHHELD_FOR_ORDER) == POSITION_SENSITIVE_OPERATIONS
    assert len(set(WITHHELD_FOR_ORDER.values())) == len(WITHHELD_FOR_ORDER)


# ---------------------------------------------------------------------------
# End to end — labelled readings, then the checks
# ---------------------------------------------------------------------------


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _two_cabinets(left: str, right: str) -> bytes:
    """One drawing stating two cabinet widths, left to right."""
    return _pdf(
        f"BT /F1 10 Tf 1 0 0 1 20 70 Tm ({left}) Tj ET\n"
        f"BT /F1 10 Tf 1 0 0 1 140 70 Tm ({right}) Tj ET\n".encode()
    )


def _revision(session: Session, store: LocalStore, drawings: dict[str, bytes]) -> PackageRevision:
    """A revision of one architectural and one shop drawing, keyed by `Document.kind`."""
    project = Project(name=f"pairing {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.EXTRACTING
    )
    session.add(revision)
    session.flush()
    for kind, data in drawings.items():
        digest = hashlib.sha256(data).hexdigest()
        document = Document(package_id=package.id, kind=kind)
        session.add(document)
        session.flush()
        key = storage_key(document.id, digest)
        artifact = SourceArtifact(storage_key=key, sha256=digest, size=len(data))
        session.add(artifact)
        session.flush()
        version = DocumentVersion(
            document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=1
        )
        session.add(version)
        session.flush()
        session.add(
            PackageRevisionDocument(
                package_revision_id=revision.id,
                package_id=package.id,
                document_id=document.id,
                document_version_id=version.id,
            )
        )
        store.put(key, io.BytesIO(data), content_type="application/pdf")
    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    session.add(
        TaskRun(
            workflow_run_id=workflow_run.id,
            idempotency_key=stage_idempotency_key(
                package_revision_id=revision.id,
                stage="extract_pages",
                engine_version=ENGINE_VERSION,
            ),
            task_type="extract_pages",
            attempt=1,
            outcome="claimed",
        )
    )
    session.flush()
    return revision


def _run(*labels: str) -> bytes:
    """One drawing stating `labels` left to right, far enough apart to read separately."""
    return _pdf(
        b"".join(
            f"BT /F1 10 Tf 1 0 0 1 {20 + 70 * i} 70 Tm ({label}) Tj ET\n".encode()
            for i, label in enumerate(labels)
        ),
        box=b"[0 0 400 100]",
    )


def _label(
    session: Session,
    revision: PackageRevision,
    kind: str,
    text: str,
    semantic_type: str = "cabinet_width",
) -> None:
    """A reviewer names the reading `text` on the `kind` drawing — a cabinet width unless told."""
    candidate = (
        session.execute(
            select(ObservationCandidate)
            .join(DocumentVersion, DocumentVersion.id == ObservationCandidate.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == DocumentVersion.id,
            )
            .where(
                PackageRevisionDocument.package_revision_id == revision.id,
                Document.kind == kind,
                ObservationCandidate.raw_text == text,
                ObservationCandidate.value_numerator.is_not(None),
            )
            .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
        )
        .scalars()
        .first()
    )
    assert candidate is not None, (kind, text)
    confirmed = confirm_candidate_type(
        session, candidate_id=candidate.id, semantic_type=semantic_type, confirmed_by="a reviewer"
    )
    assert not isinstance(confirmed, ConfirmationRefused), confirmed


def _finding(session: Session, revision: PackageRevision, rule_id: str) -> Finding:
    rows = session.execute(
        select(Finding, RuleSnapshot)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .where(Finding.package_revision_id == revision.id)
    ).all()
    (finding,) = [row for row, snapshot in rows if from_row(snapshot).rule.id == rule_id]
    return finding


def _swapped_and_labelled(session: Session, store: LocalStore) -> PackageRevision:
    """The #794 package: A=24" and B=36" drawn by the architect, the other way round by the vendor,
    and labelled in the order that pairs them as matches."""
    revision = _revision(
        session,
        store,
        {"architectural": _two_cabinets('24"', '36"'), "shop": _two_cabinets('36"', '24"')},
    )
    DatabaseStages(store).extract_pages(session, revision.id)
    _label(session, revision, "architectural", '24"')
    _label(session, revision, "architectural", '36"')
    _label(session, revision, "shop", '24"')
    _label(session, revision, "shop", '36"')
    return revision


def test_two_swapped_cabinets_labelled_in_another_order_never_pass(
    session: Session, store: LocalStore
) -> None:
    """**The point.** Outcome: NOT_FOUND, never PASS, and the reviewer is told it is the cabinets'
    tags that are missing and that the form will compare the two lists today."""
    revision = _swapped_and_labelled(session, store)
    _publish_rulebook(session)

    DatabaseStages(store).run_checks(session, revision.id)

    finding = _finding(session, revision, RULE)
    assert finding.outcome == Outcome.NOT_FOUND.value
    assert finding.reason is not None and finding.reason.startswith(IDENTIFIER_PAIRING_WITHHELD)


def test_labelled_cabinet_widths_are_held_back_only_from_the_checks_that_compare_by_position(
    session: Session, store: LocalStore
) -> None:
    """Outcome: neither the pairing check nor `CAB-FILLER-001` gets a cabinet run from evidence;
    `CT-WIDTH-001`, which sums the same readings, still gets the vendor's."""
    revision = _swapped_and_labelled(session, store)

    operands = operands_from_evidence(session, revision.id, _rules())

    assert RULE not in operands
    assert not {"architectural_cabinets", "shop_cabinets"} & operands.get(FILLER_RULE, {}).keys()
    summed = operands["CT-WIDTH-001"]["cabinet_widths"].value
    assert isinstance(summed, tuple)
    assert sorted(width.exact for width in summed if isinstance(width, Measurement)) == [24, 36]


# ---------------------------------------------------------------------------
# CAB-FILLER-001 — the same hole in a check that compares runs in wall order (#833)
# ---------------------------------------------------------------------------

#: The per-type cabinet width bounds CAB-FILLER-001 needs and the rulebook gives no default for, as
#: a project records them. The check will not run without them. Both cabinets lie inside their own
#: type's bounds, and the probe's site matches the drawing, so no cabinet has to move.
CABINET_BOUNDS = {
    "single_door_cab_width_min": 9,
    "single_door_cab_width_max": 36,
    "double_door_cab_width_min": 24,
    "double_door_cab_width_max": 48,
    "drawer_cab_width_min": 12,
    "drawer_cab_width_max": 36,
}


def _values(inches: dict[str, int | tuple[int, ...]]) -> dict[str, ParameterValue]:
    """Values as the form stores them: a run is one entry per place, `#0` upward."""
    entries: dict[str, int] = {}
    for name, value in inches.items():
        if isinstance(value, tuple):
            entries.update({f"{name}#{place}": member for place, member in enumerate(value)})
        else:
            entries[name] = value
    return {
        key: ParameterValue(
            value=Quantity(value=Fraction(member), unit=Unit.INCH),
            provenance=Provenance.MEASURED,
            set_by="a reviewer",
            set_at=datetime(2026, 10, 3, tzinfo=UTC),
        )
        for key, member in entries.items()
    }


def _form(session: Session, revision: PackageRevision, **runs: tuple[int, ...]) -> None:
    """What a reviewer enters for CAB-FILLER-001 beside the labels: the project's six cabinet
    bounds, the site's 63", a classification per cabinet, and any `runs` typed in wall order."""
    package = session.get(Package, revision.package_id)
    assert package is not None
    typed: dict[str, int | tuple[int, ...]] = {f"{FILLER_RULE}:field_width": 63}
    typed.update({f"{FILLER_RULE}:{name}": run for name, run in runs.items()})
    for layer, values, owner in (
        (ParameterLayer.PROJECT, _values(dict(CABINET_BOUNDS)), None),
        (ParameterLayer.RUN, _values(typed), revision.id),
    ):
        stored, rows = to_rows(
            ParameterSet(
                project_id=str(package.project_id), layer=layer, version=1, parameters=values
            ),
            package_revision_id=owner,
        )
        session.add(stored)
        session.add_all(rows)
    record_classifications(
        session,
        package_revision_id=revision.id,
        rule_id=FILLER_RULE,
        input_name="cabinet_type",
        categories=("single_door", "double_door"),
        confirmed_by="a reviewer",
    )
    session.flush()


def _swapped_run_labelled(session: Session, store: LocalStore) -> PackageRevision:
    """The #794 swap inside a whole run. The architect draws a 63" top over a 1" filler, A = 24",
    B = 36" and a 2" filler; the vendor draws the fillers where they were and B before A. A reviewer
    labels every reading, the vendor's 24" first — so both cabinet runs arrive as 24", 36"."""
    revision = _revision(
        session,
        store,
        {
            "architectural": _run('63"', '1"', '24"', '36"', '2"'),
            "shop": _run('1"', '36"', '24"', '2"'),
        },
    )
    DatabaseStages(store).extract_pages(session, revision.id)
    _label(session, revision, "architectural", '63"', "CT001")
    for kind in ("architectural", "shop"):
        _label(session, revision, kind, '1"', "filler_width")
        _label(session, revision, kind, '2"', "filler_width")
        _label(session, revision, kind, '24"')
        _label(session, revision, kind, '36"')
    return revision


def test_a_run_with_two_cabinets_swapped_never_passes_the_filler_check(
    session: Session, store: LocalStore
) -> None:
    """**The point of #833.** Outcome: NOT_FOUND, never PASS, and the reviewer is told it is the
    order along the wall that is missing and that the form will compare the runs today.

    Before #833 this was PASS — "shop drawing matches the exact distribution" — because the
    vendor's cabinets arrived in the order they were labelled, 24" then 36", the architect's order.
    """
    revision = _swapped_run_labelled(session, store)
    _form(session, revision)
    _publish_rulebook(session)

    DatabaseStages(store).run_checks(session, revision.id)

    finding = _finding(session, revision, FILLER_RULE)
    assert finding.outcome == Outcome.NOT_FOUND.value, finding.reason
    assert finding.reason is not None and finding.reason.startswith(RUN_ORDER_WITHHELD)


def test_the_same_swap_typed_on_the_form_in_wall_order_fails(
    session: Session, store: LocalStore
) -> None:
    """**The form path is unchanged.** The same labelled package, with both runs typed in wall order
    as well: the vendor's 36" then 24". Outcome: FAIL, the vendor's swap caught — and the probe's
    NOT_FOUND above is the missing order, not a check that can no longer decide."""
    revision = _swapped_run_labelled(session, store)
    _form(
        session,
        revision,
        architectural_fillers=(1, 2),
        shop_fillers=(1, 2),
        architectural_cabinets=(24, 36),
        shop_cabinets=(36, 24),
    )
    _publish_rulebook(session)

    DatabaseStages(store).run_checks(session, revision.id)

    finding = _finding(session, revision, FILLER_RULE)
    assert finding.outcome == Outcome.FAIL.value, finding.reason
