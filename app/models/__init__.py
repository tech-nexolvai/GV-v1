"""Every persisted model, imported so `Base.metadata` actually knows about them.

This module exists to be imported for its side effects. A SQLAlchemy model only registers itself in
`Base.metadata` when its module is imported, so without this Alembic sees an empty schema:
autogenerate produces a migration creating nothing, and a models-versus-migrations round-trip check
passes because it is comparing two empty sets.

That is a silent failure of exactly the kind worth avoiding — the check is green and it is checking
nothing. `alembic/env.py` imports this module, and `tests/app/test_models_registered.py` asserts the
registration is real.

**Add every new model module here.** A model that is not imported is a table that will not be
migrated, and the first sign of it is a missing table in production.
"""

from __future__ import annotations

from app.models.document import (
    Document,
    DocumentKind,
    DocumentVersion,
    Page,
    PageType,
    SourceArtifact,
)
from app.models.drawing import (
    Alias,
    DrawingItem,
    DrawingView,
    ItemIdentifier,
    duplicate_identifiers,
)
from app.models.evaluation import (
    CaseResult,
    EvaluationRun,
    GoldCase,
    GoldSet,
    MetricResult,
)
from app.models.evidence import (
    CanonicalObservation,
    EvidenceArtifact,
    EvidenceArtifactKind,
    EvidenceCandidateRole,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    ObservationCandidate,
)
from app.models.matching import (
    ApprovalSource,
    ApprovedMatch,
    MatchCandidate,
    MatchReviewEvent,
)
from app.models.outbox import OutboxEntry
from app.models.package import Package, PackageRevision, PackageState, PackageStateEvent, Project
from app.models.review import (
    Approval,
    ApprovedFinding,
    CorrectionLedgerEntry,
    ExceptionScope,
    ReviewAction,
    ReviewActionKind,
    ReviewException,
    ReviewSession,
)
from app.models.rules import (
    RuleApplicabilityScope,
    RuleDefinition,
    RuleSnapshot,
)
from app.models.runs import (
    ExtractionRun,
    ModelInvocation,
    ModelInvocationOutcome,
    TaskRun,
    WorkflowRun,
)
from app.models.verdicts import (
    CheckRun,
    Finding,
    FindingEvidence,
    VerdictInput,
)

__all__ = [
    "Alias",
    "Approval",
    "ApprovalSource",
    "ApprovedFinding",
    "ApprovedMatch",
    "CanonicalObservation",
    "CaseResult",
    "CheckRun",
    "CorrectionLedgerEntry",
    "Document",
    "DocumentKind",
    "DocumentVersion",
    "DrawingItem",
    "DrawingView",
    "EvaluationRun",
    "EvidenceArtifact",
    "EvidenceArtifactKind",
    "EvidenceCandidateRole",
    "EvidenceCorroborationLane",
    "EvidenceSupportingCandidate",
    "ExceptionScope",
    "ExtractionRun",
    "Finding",
    "FindingEvidence",
    "GoldCase",
    "GoldSet",
    "ItemIdentifier",
    "MatchCandidate",
    "MatchReviewEvent",
    "MetricResult",
    "ModelInvocation",
    "ModelInvocationOutcome",
    "ObservationCandidate",
    "OutboxEntry",
    "Package",
    "PackageRevision",
    "PackageState",
    "PackageStateEvent",
    "Page",
    "PageType",
    "Project",
    "ReviewAction",
    "ReviewActionKind",
    "ReviewException",
    "ReviewSession",
    "RuleApplicabilityScope",
    "RuleDefinition",
    "RuleSnapshot",
    "SourceArtifact",
    "TaskRun",
    "VerdictInput",
    "WorkflowRun",
    "duplicate_identifiers",
]
