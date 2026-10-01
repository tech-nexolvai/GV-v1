"""A comparison that pairs by identifier is never fed readings in the order they were labelled (#794).

Verification for: `workflow/evidence_operands.py` (`identifier_paired_inputs`, the evidence path) and
the reason `DatabaseStages.run_checks` gives when such a check abstains.

The one that matters most is `test_two_swapped_cabinets_labelled_in_another_order_never_pass`: the
false PASS #794 found, end to end. The architect's drawing has cabinet A at 24" and B at 36"; the
vendor's has them the other way round, so both are wrong. A reviewer labels the architect's A then B
and the vendor's B then A. Paired by labelling order that is two matches; it must never be PASS.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
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
from app.models.rules import RuleSnapshot
from app.models.runs import TaskRun, WorkflowRun
from app.models.verdicts import CheckRun, Finding
from app.verdicts.rulebook import from_row
from rules.derivations import Derivation
from rules.schema import Rule
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.extraction.test_reader import _pdf
from tests.workflow.test_stages import RULEBOOK, _publish_rulebook
from units.measurement import Measurement, Unit
from verdict.operations.pairwise import (
    IDENTIFIER_PAIRED_OPERATIONS,
    PAIRWISE_SPECS,
    pairwise_within_tolerance,
)
from verdict.outcomes import Outcome
from workflow.evidence_operands import (
    IDENTIFIER_PAIRING_WITHHELD,
    identifier_paired_inputs,
    operands_from_evidence,
)
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

RULE = "CAB-ARCH-VS-SHOP-001"


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


def test_the_rulebook_pairs_by_identifier_in_exactly_one_check() -> None:
    """Outcome: `CAB-ARCH-VS-SHOP-001`'s two cabinet runs, and no other rule's inputs. A sum of
    cabinets or a count is indifferent to order, so `CT-WIDTH-001` and `CAB-FILLER-001` keep theirs.
    """
    paired = {rule.id: identifier_paired_inputs(rule) for rule in _rules()}

    assert paired.pop(RULE) == {"architectural_cabinets", "shop_cabinets"}
    assert all(inputs == frozenset() for inputs in paired.values()), paired


def test_a_rule_that_pairs_through_a_derivation_is_caught_too() -> None:
    """Outcome: inputs bound to a pairing derivation are paired inputs, a tuple binding included."""
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

    assert identifier_paired_inputs(through_a_derivation) == {
        "architectural_cabinets",
        "shop_cabinets",
    }


def test_every_pairwise_operation_is_declared_as_pairing_by_identifier() -> None:
    """**The drift guard.** A second pairing operation added to the module without the declaration
    would take a labelled list in labelling order again."""
    assert {spec.name for spec in PAIRWISE_SPECS} <= IDENTIFIER_PAIRED_OPERATIONS


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


def _label(session: Session, revision: PackageRevision, kind: str, text: str) -> None:
    """A reviewer names the reading `text` on the `kind` drawing a cabinet width."""
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
        session, candidate_id=candidate.id, semantic_type="cabinet_width", confirmed_by="a reviewer"
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


def test_labelled_cabinet_widths_are_held_back_only_from_the_pairing_check(
    session: Session, store: LocalStore
) -> None:
    """Outcome: the pairing check gets neither run from evidence; `CAB-FILLER-001`, which sums
    the same readings, still gets both."""
    revision = _swapped_and_labelled(session, store)

    operands = operands_from_evidence(session, revision.id, _rules())

    assert RULE not in operands
    assert {"architectural_cabinets", "shop_cabinets"} <= operands["CAB-FILLER-001"].keys()
