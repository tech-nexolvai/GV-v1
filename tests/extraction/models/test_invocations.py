"""The contract for recording every model call, from issue #251.

Covers both halves of the story: `extraction/models/invocations.py`, which owns what must be recorded
and validates it, and `app/runs/invocations.py`, which stores it and reads it back. They are tested
together from here because they are one behaviour split across an architectural boundary, and issue
#251 names this file as the verification for it. A test file may import both; the production code may
not, and `test_extraction_models_cannot_reach_the_rules_package` is why.

Four things are proved here that a happy-path test would not.

**The failure paths are the subject, not an afterthought.** Every outcome in the closed set is
recorded end to end, because the record exists to explain money spent going nowhere as much as money
spent well.

**The integer guard is shown to be load-bearing.** `test_the_column_alone_would_round_a_float_cost`
writes a fractional cost straight to the ORM and demonstrates PostgreSQL storing a different number
without complaint. Without that test, the guard in `InvocationRecord` looks like a redundant re-check
of a database constraint, and the next person to tidy it away would have no way to know it is the
only thing there.

**Negative tests fail for the named reason.** Each one asserts on the constraint PostgreSQL actually
rejected, read from the driver's diagnostics, so a row rejected by some other rule cannot be
mistaken for the rule under test.

**The import boundary is asserted transitively.** `docs/DESIGN_AI.md` §2 forbids `extraction/models/`
from reaching `rules/`, and it measures that by reachability rather than by direct imports. A direct
check would have passed the first version of this module, which reached `rules/` in two hops.

The schema is built by running the real migrations rather than `Base.metadata.create_all`. Model
introspection cannot see triggers, and `alembic/versions/0013_append_only.py` installs one on this
table; building the schema the way production builds it is the only way these tests meet the same
rules CI does.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, Float
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base
from app.db.session import session_factory, unit_of_work
from app.models import (
    Document,
    DocumentKind,
    DocumentVersion,
    EvidenceArtifact,
    EvidenceArtifactKind,
    ExtractionRun,
    ModelInvocation,
    ModelInvocationOutcome,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageState,
    Page,
    Project,
    SourceArtifact,
    TaskRun,
    WorkflowRun,
)
from app.runs.invocations import (
    candidate_id_for,
    crop_for,
    invocations_for_candidate,
    record,
)
from extraction.models.invocations import InvocationRecord
from units.measurement import Unit
from vocabulary.semantic_types import SemanticType

pytest_plugins = ("tests.app.postgres_fixture",)

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCUMENT_HASH = "a" * 64
PAGE_HASH = "b" * 64
CROP_BYTES = b"the crop the model was shown"


def _violated(error: IntegrityError) -> str | None:
    """The constraint PostgreSQL rejected on, from the driver's own diagnostics.

    The `ck_model_invocations_` prefix is stripped before comparing. `app/models/runs.py` names its
    checks bare and `Base` renders them through the project's naming convention, while
    `alembic/versions/0005_run_records.py` writes the same names into `op.create_table`, which
    builds its own metadata and applies no convention. Which spelling is installed depends on how
    the schema was built, so asserting one of them would be asserting the build route rather than
    the constraint. Everything that tells one constraint from another survives the strip.
    """
    diagnostic = getattr(getattr(error, "orig", None), "diag", None)
    name: str | None = getattr(diagnostic, "constraint_name", None)
    if name is None:
        return None
    return name.removeprefix("ck_model_invocations_")


def _upgrade(engine: Engine) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


# ---------------------------------------------------------------------------
# One package, one run chain, one candidate and the crop it was read from
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Context:
    """The ids a test needs from the persisted scenario, named rather than positional."""

    extraction_run_id: UUID
    document_version_id: UUID
    page_id: UUID


def _persist_context(session: Session) -> Context:
    project = Project(name="GV Invocation Test")
    package = Package(project_id=project.id, vendor=None)
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    source = SourceArtifact(
        storage_key=f"originals/{project.id}/drawing.pdf",
        sha256=DOCUMENT_HASH,
        size=100,
        backend_version_id=None,
    )
    session.add(project)
    session.flush()
    session.add(package)
    session.flush()
    session.add(revision)
    session.flush()
    document = Document(package_revision_id=revision.id, kind=DocumentKind.SHOP)
    session.add_all((source, document))
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=source.id,
        sha256=DOCUMENT_HASH,
        page_count=1,
    )
    session.add(version)
    session.flush()
    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash=PAGE_HASH,
        width_pt=612,
        height_pt=792,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        sheet_number="A-101",
        page_type=None,
        revision_label=None,
        revision_date_raw=None,
        revision_date_interpretations=None,
        revision_sequence_index=None,
    )
    workflow = WorkflowRun(package_revision_id=revision.id, engine_run_id=f"run-{project.id}")
    session.add_all((page, workflow))
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key=f"extract-{project.id}",
        task_type="extract_page",
        attempt=1,
        outcome="completed",
    )
    session.add(task)
    session.flush()
    extraction = ExtractionRun(
        task_run_id=task.id,
        extractor="nova-2-lite",
        extractor_version="1.0.0",
        config_hash="config-v1",
    )
    session.add(extraction)
    session.flush()
    return Context(extraction.id, version.id, page.id)


def _persist_candidate(session: Session, context: Context) -> ObservationCandidate:
    value = Fraction(1, 3)
    candidate = ObservationCandidate(
        document_version_id=context.document_version_id,
        page_id=context.page_id,
        extraction_run_id=context.extraction_run_id,
        raw_text='24 1/3"',
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit=Unit.INCH,
        unit_guess=Unit.INCH,
        semantic_guess=SemanticType.CT001,
        polygon=[[10, 10], [20, 10], [20, 20]],
        coordinate_space="image",
        confidence=None,
        ambiguity_flags=[],
    )
    session.add(candidate)
    session.flush()
    return candidate


def _persist_crop(
    session: Session, context: Context, candidate_id: UUID, content: bytes = CROP_BYTES
) -> EvidenceArtifact:
    artifact = EvidenceArtifact(
        candidate_id=candidate_id,
        canonical_observation_id=None,
        document_version_id=context.document_version_id,
        page_id=context.page_id,
        kind=EvidenceArtifactKind.CROP,
        storage_key=f"evidence/crops/{uuid4().hex}.png",
        sha256=hashlib.sha256(content).hexdigest(),
        media_type="image/png",
        coordinate_space="image",
    )
    session.add(artifact)
    session.flush()
    return artifact


def _built(extraction_run_id: UUID, **changes: object) -> InvocationRecord:
    values: dict[str, object] = {
        "extraction_run_id": extraction_run_id,
        "model_id": "nova-2-lite-2026-05-14",
        "prompt_id": "dimension-reader-v3",
        "template_id": "crop-dimension-v2",
        "crop_artifact_id": None,
        "input_tokens": 612,
        "output_tokens": 44,
        "cost_micros": 137,
        "latency_ms": 850,
        "outcome": ModelInvocationOutcome.OK,
    }
    values.update(changes)
    return InvocationRecord(**values)  # type: ignore[arg-type]


def _record(session: Session, context: Context, **changes: object) -> ModelInvocation:
    return record(session, _built(context.extraction_run_id, **changes))


# ---------------------------------------------------------------------------
# The import boundary the design sets, measured the way the design measures it
# ---------------------------------------------------------------------------


def _project_imports(path: Path, packages: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return {name for name in found if name.split(".")[0] in packages}


def _reachable_from(module: str, packages: set[str]) -> dict[str, list[str]]:
    """Every project module reachable from `module`, with the chain that reached it."""

    chains: dict[str, list[str]] = {}
    seen = {module}
    queue: list[tuple[str, list[str]]] = [(module, [module])]
    while queue:
        current, chain = queue.pop()
        target = REPO_ROOT / current.replace(".", "/")
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target.with_suffix(".py")]
        for file in files:
            if not file.exists():
                continue
            for imported in _project_imports(file, packages):
                chains.setdefault(imported, [*chain, imported])
                if imported not in seen:
                    seen.add(imported)
                    queue.append((imported, [*chain, imported]))
    return chains


#: Every top-level package in this repo, so the walk knows what to step into.
PROJECT_PACKAGES = {
    "app",
    "eval",
    "evidence",
    "extraction",
    "reports",
    "retrieval",
    "rules",
    "units",
    "verdict",
    "vocabulary",
    "workflow",
}


@pytest.mark.parametrize("forbidden", ["rules", "app"])
def test_extraction_models_cannot_reach_a_package_it_is_not_permitted(forbidden: str) -> None:
    """Input: the import closure. Outcome: no route out. Why: reachability is the design's standard.

    `docs/DESIGN_AI.md` §2 permits `extraction/models/` to import `evidence/` and `storage/`, and
    forbids `rules/` and `verdict/`. `app/` is not in the permitted column either. The section is
    explicit that the test is reachability rather than direct imports: *"the prohibited capabilities
    are absent from the agent's reachable surface, not refused at call time."*

    The first version of this story imported `app.models` for the ORM class, which reached `rules/`
    in two hops via `app.models.evidence -> rules.semantic_types`.
    `tests/test_verdict_isolation.py` did not catch it, because for `extraction/` it checks direct
    imports only — a real gap in that guard, noted on PR #352 and left for its own story rather than
    widened from here. This test closes the hole for this package.

    **`app/` is asserted as well as `rules/`, and that matters.** Asserting only `rules/` would let
    the violation back in a shape that happens not to reach it today: `app.models.runs` on its own
    imports nothing from `rules/`, so an `app` import would pass a rules-only check and leave the
    breach one new import away from being real again. `app/` is the rule the design actually states,
    and it is verified safe to assert — neither `evidence/` nor `storage/`, the two packages this one
    may import, reaches `app/`.

    **`verdict/` is deliberately not asserted, and that is not an oversight.** `DESIGN_EXTRACTION.md`
    §10 has `evidence/` import the contract types from `verdict/operands.py` on purpose, and
    `extraction/models/` is permitted to import `evidence/`. So `verdict` is transitively reachable
    on a sanctioned path regardless of what this module does, and asserting it unreachable would be
    asserting a rule the design contradicts. `rules/` and `app/` have no such sanctioned route.
    """

    chains = _reachable_from("extraction.models", PROJECT_PACKAGES)
    offenders = [
        " -> ".join(chain)
        for module, chain in sorted(chains.items())
        if module.split(".")[0] == forbidden
    ]
    assert not offenders, f"extraction/models/ can reach {forbidden}/:\n  " + "\n  ".join(offenders)


def test_the_guard_above_would_notice_a_two_hop_route() -> None:
    """Input: this repo's real closure. Outcome: it walks past hop one. Why: prove it transits.

    A reachability test that silently only looked one hop deep would pass exactly as happily as a
    correct one. `app/runs/invocations.py` imports `extraction.models.invocations`, which imports
    nothing from the project — so `app.runs` reaching `rules/` at all can only be a multi-hop result,
    and finding it proves the walk does not stop at the first level.
    """

    chains = _reachable_from("app.runs", PROJECT_PACKAGES)
    routes = [chain for module, chain in chains.items() if module.split(".")[0] == "rules"]
    assert routes, "expected app/runs/ to reach rules/ transitively; the walk found nothing"
    assert all(len(chain) > 2 for chain in routes), f"no multi-hop route was walked: {routes}"


# ---------------------------------------------------------------------------
# Cost is an integer, and the guard is the only thing that makes that true
# ---------------------------------------------------------------------------


def test_no_invocation_column_uses_binary_floating_point() -> None:
    """Input: the table definition. Outcome: no Float. Why: money must not be approximate."""

    for column in Base.metadata.tables["model_invocations"].columns:
        assert not isinstance(column.type, Float), f"{column.name} is approximate"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cost_micros", 0.6),
        ("cost_micros", Decimal("137.4")),
        ("cost_micros", True),
        ("input_tokens", 612.0),
        ("output_tokens", Decimal(44)),
        ("latency_ms", 850.5),
    ],
)
def test_a_cost_or_count_that_is_not_a_plain_int_is_refused(field: str, value: object) -> None:
    """Input: a float, Decimal or bool. Outcome: TypeError naming it. Why: no rounding, silent.

    No session and no database, because the check is not part of persistence. Refusing at
    construction is what makes it unskippable: `record` takes an `InvocationRecord`, and there is no
    such object holding a cost that was never an exact integer.
    """

    with pytest.raises(TypeError, match=field):
        _built(uuid4(), **{field: value})


def test_the_column_alone_would_round_a_float_cost(postgres_engine: Engine) -> None:
    """Input: 137.6 written past `record`. Outcome: 138 stored. Why: the guard is not redundant.

    This is the defect the guard in `record` exists to stop, demonstrated rather than asserted. The
    `cost_micros` column is a PostgreSQL `integer`; the driver hands it a float, PostgreSQL rounds,
    and no error is raised at any layer. A ceiling reading the total afterwards would be reading a
    number nobody computed.
    """

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    invocation_id: UUID
    with unit_of_work(factory) as session:
        context = _persist_context(session)
        invocation = ModelInvocation(
            extraction_run_id=context.extraction_run_id,
            model_id="nova-2-lite-2026-05-14",
            prompt_id="dimension-reader-v3",
            template_id="crop-dimension-v2",
            crop_artifact_id=None,
            input_tokens=612,
            output_tokens=44,
            cost_micros=137.6,  # type: ignore[arg-type]
            latency_ms=850,
            outcome=ModelInvocationOutcome.OK,
        )
        invocation_id = invocation.id
        session.add(invocation)
    with unit_of_work(factory) as session:
        stored = session.get(ModelInvocation, invocation_id)
        assert stored is not None
        assert stored.cost_micros == 138, "PostgreSQL rounded silently, as expected"
        assert stored.cost_micros != 137.6


# ---------------------------------------------------------------------------
# Every call is recorded, including the ones that produced nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", list(ModelInvocationOutcome))
def test_every_outcome_is_recorded_with_its_full_cost(
    postgres_engine: Engine, outcome: ModelInvocationOutcome
) -> None:
    """Input: each outcome. Outcome: a complete row. Why: a failed call still cost money."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    invocation_id: UUID
    with unit_of_work(factory) as session:
        context = _persist_context(session)
        # A refused or timed-out call typically returns nothing, so output tokens are zero while
        # input tokens and cost are not. That combination is the one a success-only record loses.
        invocation = _record(
            session,
            context,
            outcome=outcome,
            input_tokens=612,
            output_tokens=0 if outcome is not ModelInvocationOutcome.OK else 44,
            cost_micros=91,
        )
        invocation_id = invocation.id
    with unit_of_work(factory) as session:
        stored = session.get(ModelInvocation, invocation_id)
        assert stored is not None
        assert stored.outcome == outcome
        assert stored.input_tokens == 612
        assert stored.cost_micros == 91
        assert stored.latency_ms == 850
        assert stored.model_id == "nova-2-lite-2026-05-14"
        assert stored.prompt_id == "dimension-reader-v3"
        assert stored.template_id == "crop-dimension-v2"


def test_created_at_is_timezone_aware(postgres_engine: Engine) -> None:
    """Input: a recorded call. Outcome: an aware UTC time. Why: cost windows need a real clock."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    invocation_id: UUID
    with unit_of_work(factory) as session:
        invocation = _record(session, _persist_context(session))
        invocation_id = invocation.id
    with unit_of_work(factory) as session:
        stored = session.get(ModelInvocation, invocation_id)
        assert stored is not None
        assert stored.created_at.tzinfo is not None
        assert stored.created_at.utcoffset() == timedelta(0)


def test_record_does_not_commit(postgres_engine: Engine) -> None:
    """Input: a rolled-back transaction. Outcome: no row. Why: the caller owns the boundary.

    The invocation and the candidate it explains must stand or fall together. A function that
    committed on its own would leave the invocation behind when the candidate's transaction failed,
    and the cost report would then describe work that officially never happened.
    """

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    session = factory()
    try:
        context = _persist_context(session)
        invocation_id = _record(session, context).id
        session.rollback()
    finally:
        session.close()
    with unit_of_work(factory) as verify:
        assert verify.get(ModelInvocation, invocation_id) is None


# ---------------------------------------------------------------------------
# The database refuses an incomplete or dishonest row, by name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"model_id": ""}, "model_invocation_model_id"),
        ({"prompt_id": ""}, "model_invocation_prompt_id"),
        ({"template_id": ""}, "model_invocation_template_id"),
        ({"cost_micros": -1}, "model_invocation_cost"),
        ({"input_tokens": -1}, "model_invocation_input_tokens"),
        ({"output_tokens": -1}, "model_invocation_output_tokens"),
        ({"latency_ms": -1}, "model_invocation_latency"),
        ({"outcome": "partially-ok"}, "model_invocation_outcome"),
    ],
)
def test_an_invalid_invocation_is_refused_by_the_named_constraint(
    postgres_engine: Engine, changes: dict[str, object], constraint: str
) -> None:
    """Input: one broken field. Outcome: that constraint fires. Why: right reason, not any reason.

    `outcome` is checked here rather than in Python on purpose: the closed set lives in one place,
    the database, and a second copy in this module would be free to drift from it.
    """

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError) as raised, unit_of_work(factory) as session:
        _record(session, _persist_context(session), **changes)
    assert _violated(raised.value) == constraint


def test_an_invocation_cannot_be_attributed_to_a_run_that_does_not_exist(
    postgres_engine: Engine,
) -> None:
    """Input: an unknown run id. Outcome: rejection. Why: unattributable cost is not cost control."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError) as raised, unit_of_work(factory) as session:
        _record(session, Context(uuid4(), uuid4(), uuid4()))
    diagnostic = getattr(getattr(raised.value, "orig", None), "diag", None)
    name = getattr(diagnostic, "constraint_name", "") or ""
    assert "extraction_run_id" in name, f"a different rule rejected this row: {name!r}"


# ---------------------------------------------------------------------------
# What the model saw, and the link back to the candidate
# ---------------------------------------------------------------------------


def test_the_crop_reference_resolves_to_the_exact_bytes(postgres_engine: Engine) -> None:
    """Input: a recorded crop id. Outcome: the artifact and its digest. Why: reproducibility."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    invocation_id: UUID
    with unit_of_work(factory) as session:
        context = _persist_context(session)
        candidate = _persist_candidate(session, context)
        crop = _persist_crop(session, context, candidate.id)
        invocation_id = _record(session, context, crop_artifact_id=crop.id).id
    with unit_of_work(factory) as session:
        stored = session.get(ModelInvocation, invocation_id)
        assert stored is not None
        artifact = crop_for(session, stored)
        assert artifact is not None
        assert artifact.kind == EvidenceArtifactKind.CROP
        assert artifact.content_matches(CROP_BYTES) is True
        assert artifact.content_matches(b"different bytes entirely") is False


def test_a_crop_reference_naming_nothing_returns_none_rather_than_raising(
    postgres_engine: Engine,
) -> None:
    """Input: an id with no artifact. Outcome: None. Why: the column carries no foreign key.

    Recorded deliberately as a limitation rather than tidied away. `crop_artifact_id` has no foreign
    key — `alembic/versions/0005_run_records.py` leaves it a bare `uuid` because evidence artifacts
    landed in a later migration — so nothing stops an id that resolves to no row. Returning `None`
    says the crop is not recoverable; raising would imply the database had promised it would be.
    """

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        context = _persist_context(session)
        invocation = _record(session, context, crop_artifact_id=uuid4())
        assert crop_for(session, invocation) is None
        assert candidate_id_for(session, invocation) is None


def test_a_call_with_no_crop_recorded_resolves_to_nothing(postgres_engine: Engine) -> None:
    """Input: crop_artifact_id=None. Outcome: None. Why: absence must not read as a broken link."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        invocation = _record(session, _persist_context(session), crop_artifact_id=None)
        assert crop_for(session, invocation) is None


def test_candidate_and_invocation_resolve_to_each_other(postgres_engine: Engine) -> None:
    """Input: a candidate, its crop and the call. Outcome: each finds the other. Why: audit."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    candidate_id: UUID
    invocation_id: UUID
    with unit_of_work(factory) as session:
        context = _persist_context(session)
        candidate = _persist_candidate(session, context)
        candidate_id = candidate.id
        crop = _persist_crop(session, context, candidate.id)
        invocation_id = _record(session, context, crop_artifact_id=crop.id).id
    with unit_of_work(factory) as session:
        stored = session.get(ModelInvocation, invocation_id)
        assert stored is not None
        assert candidate_id_for(session, stored) == candidate_id
        found = invocations_for_candidate(session, candidate_id)
        assert [one.id for one in found] == [invocation_id]


def test_the_failed_attempts_on_a_candidate_are_returned_too(postgres_engine: Engine) -> None:
    """Input: a timeout then a success. Outcome: both. Why: four attempts cost four calls.

    A reverse lookup that returned only successful calls would make the candidates that were hardest
    to read look like the cheapest ones in the package.
    """

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    candidate_id: UUID
    with unit_of_work(factory) as session:
        context = _persist_context(session)
        candidate = _persist_candidate(session, context)
        candidate_id = candidate.id
        crop = _persist_crop(session, context, candidate.id)
        _record(
            session,
            context,
            crop_artifact_id=crop.id,
            outcome=ModelInvocationOutcome.TIMEOUT,
            output_tokens=0,
            cost_micros=204,
        )
        _record(session, context, crop_artifact_id=crop.id, cost_micros=137)
    with unit_of_work(factory) as session:
        found = invocations_for_candidate(session, candidate_id)
        assert {one.outcome for one in found} == {
            ModelInvocationOutcome.TIMEOUT,
            ModelInvocationOutcome.OK,
        }
        # Integer micros throughout, so the total is exact rather than nearly right.
        assert sum(one.cost_micros for one in found) == 341


def test_another_candidates_calls_are_not_returned(postgres_engine: Engine) -> None:
    """Input: two candidates with their own crops. Outcome: no bleed. Why: attribution must be true."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    first_id: UUID
    second_invocation_id: UUID
    with unit_of_work(factory) as session:
        context = _persist_context(session)
        first = _persist_candidate(session, context)
        second = _persist_candidate(session, context)
        first_id = first.id
        first_crop = _persist_crop(session, context, first.id, content=b"first crop")
        second_crop = _persist_crop(session, context, second.id, content=b"second crop")
        _record(session, context, crop_artifact_id=first_crop.id)
        second_invocation_id = _record(session, context, crop_artifact_id=second_crop.id).id
    with unit_of_work(factory) as session:
        found = invocations_for_candidate(session, first_id)
        assert second_invocation_id not in {one.id for one in found}
        assert len(found) == 1
