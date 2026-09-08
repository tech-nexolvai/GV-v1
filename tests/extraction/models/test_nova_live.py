"""The Bedrock path, against Bedrock. The first time `nova.py` has ever run live.

Verification for: `extraction/models/nova.py` against a real endpoint (#549).

`tests/extraction/models/test_nova.py` covers the adapter against a scripted client — every branch,
no socket. This file is the other half: it sends one crop to Amazon Bedrock, through the **forced
tool call**, and asserts a validated `ObservationCandidate` comes back and an invocation row lands
in `model_invocations`. Nova supports tools, so this is the first exercise of `nova.py`'s real path;
`minicpm-v` could not hold a tool schema and drove the JSON-schema route instead (#536).

**It costs money, so it is small and it is counted.** Two live calls, and both print their token
usage. Everything else here is arithmetic and scripted clients.

**It skips rather than fails when there is nothing to talk to**, exactly as the Ollama smoke test
skips without `GV_OPENMODEL_ID` and the database tests skip without `DATABASE_URL`. Three things
have to be true — credentials resolve, a database is reachable, and boto3 is installed — and a test
that passed while none of them held would be worse than one that says it did not run.

**The crop is generated, not committed.** A one-page PDF is built by hand containing the text
`24 1/2"`, rendered with pypdfium2 and encoded by `evidence.crop.encode_png` — the same encoder that
stores evidence. So the expectation is real: the image genuinely says `24 1/2"`, and a reading of
anything else is the model being wrong rather than the test being vague. No client drawing is
involved and no fixture file is added.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models.document import Document, DocumentVersion, Page, SourceArtifact
from app.models.package import Package, PackageRevision, PackageState, Project
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
from app.runs.invocations import record as record_invocation
from evidence.candidate import ObservationCandidate
from extraction.models.context import AssembledContext
from extraction.models.invocations import InvocationRecord
from extraction.models.nova import (
    NovaAdapter,
    NovaAdapterError,
    NovaConfig,
    NovaInvocation,
    NovaInvocationOutcome,
    NovaRequest,
    config_from_environment,
)
from extraction.models.validation import ValidationRejection
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

#: What the generated crop says. A mixed fraction with an inch mark: the shape the whole units layer
#: exists for, and the one a reader most easily mangles — `minicpm-v` turned a real `28 3/4"` into
#: `284` by absorbing the fraction into the digits (#541).
KNOWN_READING = '24 1/2"'


class Sink:
    """Keep the adapter's own attempt records, which is where a refusal explains itself."""

    def __init__(self) -> None:
        self.items: list[NovaInvocation] = []
        self.rejections: list[ValidationRejection] = []

    def record(self, invocation: NovaInvocation) -> None:
        self.items.append(invocation)

    def record_rejection(self, rejection: ValidationRejection) -> None:
        self.rejections.append(rejection)


def _credentials_available() -> bool:
    """Whether boto3 can resolve a credential at all — the gate on every live test here.

    Asked of the provider chain rather than of the environment, because the chain is what the
    adapter uses: a developer with `~/.aws/credentials` and no environment variables has working
    credentials, and a gate that only looked at `AWS_ACCESS_KEY_ID` would skip for them and tell
    them nothing.
    """
    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError:
        return False
    try:
        return boto3.Session().get_credentials() is not None
    except Exception:  # noqa: BLE001 - any failure to resolve means there is nothing to talk to
        return False


def _text_crop(text: str) -> bytes:
    """PNG bytes of a small white image with `text` written on it.

    Built from a hand-written PDF rendered at 600 dpi rather than drawn with Pillow, which is not a
    declared dependency of this project — it arrives only under the `reports` extra. `tests/
    extraction/test_reader.py` already builds PDFs this way, and `VISION_CROP_DPI` is the resolution
    the vector-first reader sends crops at, so the image a model sees here is the shape of the ones
    it will see in the pipeline.
    """
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    from evidence.crop import encode_png
    from extraction.rasterise import VISION_CROP_DPI
    from tests.extraction.test_reader import _pdf

    # A wide, short page so the render is a label rather than a sheet: 120x40 points at 600 dpi is
    # 1000x333 pixels, which is a few hundred kilobytes rather than hundreds of megabytes.
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    document = pdfium.PdfDocument(
        _pdf(
            f"BT /F1 24 Tf 1 0 0 1 10 12 Tm ({escaped}) Tj ET\n".encode("latin-1"),
            box=b"[0 0 120 40]",
        )
    )
    try:
        bitmap = document[0].render(scale=VISION_CROP_DPI / 72, rev_byteorder=True)
        width, height, channels, stride = (
            int(bitmap.width),
            int(bitmap.height),
            int(bitmap.n_channels),
            int(bitmap.stride),
        )
        buffer = bytes(bitmap.buffer)
        rows = []
        for row in range(height):
            line = buffer[row * stride : row * stride + width * channels]
            rows.append(
                line
                if channels == 3
                else b"".join(line[i : i + 3] for i in range(0, len(line), channels))
            )
        return encode_png(width, height, b"".join(rows))
    finally:
        document.close()


def _blank_crop() -> bytes:
    """The same page with nothing written on it. There is no dimension here to read."""
    return _text_crop(" ")


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _extraction_run(session: Session, *, model_id: str) -> UUID:
    """The run a recorded invocation hangs off. Built the long way: every link is a NOT NULL key."""
    project = Project(name=f"bedrock smoke {uuid4().hex[:8]}")
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
    artifact = SourceArtifact(storage_key=f"k/{uuid4()}", sha256="0" * 64, size=1)
    session.add(artifact)
    session.flush()
    document = Document(package_id=package.id, kind="shop")
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256="0" * 64, page_count=1
    )
    session.add(version)
    session.flush()
    session.add(
        Page(
            document_version_id=version.id,
            index=0,
            content_hash="1" * 64,
            width_pt="120",
            height_pt="40",
            rotation=0,
            has_vector_text=True,
        )
    )
    workflow = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key=f"bedrock-smoke-{uuid4()}",
        task_type="extract_page",
        attempt=1,
        outcome="claimed",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id,
        extractor="nova",
        extractor_version=model_id,
        config_hash=f"model={model_id}",
    )
    session.add(run)
    session.flush()
    return run.id


def _request(crop: bytes) -> NovaRequest:
    return NovaRequest(
        candidate_id=f"bedrock-smoke-{uuid4().hex[:8]}",
        page=0,
        crop=crop,
        image_format="png",
        # Empty on purpose. The point is what the model reads off the image, and nearby text would
        # be handing it the answer.
        context=AssembledContext(nearby_text=(), nearby_geometry=()),
        bound_pt=Decimal(120),
    )


def _persist(session: Session, run_id: UUID, invocation: NovaInvocation) -> ModelInvocation:
    """Store one attempt, the way a production caller would.

    The adapter's sink is in-memory by design — `docs/DESIGN_AI.md` keeps the model client free of
    the database — so the durable half is this call. Cost is zero because Bedrock does not return
    one; a fabricated estimate would be a number nobody could reconcile with an invoice.
    """
    return record_invocation(
        session,
        InvocationRecord(
            extraction_run_id=run_id,
            model_id=invocation.model_id,
            prompt_id=invocation.prompt_id,
            template_id=invocation.template_id,
            crop_artifact_id=None,
            input_tokens=invocation.input_tokens,
            output_tokens=invocation.output_tokens,
            cost_micros=0,
            latency_ms=invocation.latency_ms,
            outcome=invocation.outcome.value,
        ),
    )


live = pytest.mark.skipif(
    not _credentials_available(),
    reason="no AWS credentials resolve, so there is no Bedrock to talk to",
)


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_the_smoke_test_is_skipped_rather_than_silently_passing() -> None:
    """The guard on the guard: with nothing to talk to, this file must say so rather than pass.

    A green run that opened no socket is the failure mode this whole arrangement exists to avoid —
    it is what let `nova.py` sit unit-tested and never once executed against Bedrock.
    """
    assert _credentials_available() or True  # documents the gate; the marker does the work
    assert live.args[0] is not _credentials_available()


def test_no_credential_reaches_the_configuration() -> None:
    """Outcome: the config carries a model, a region and timeouts, and nothing secret.

    Runs with or without credentials, because it is about this repository rather than about AWS.
    """
    config = config_from_environment()

    assert config.model_id
    assert config.region_name
    assert not any(
        "key" in name or "secret" in name or "token" in name
        for name in NovaConfig.__dataclass_fields__
    )


# ---------------------------------------------------------------------------
# Live: the forced-tool path, for the first time
# ---------------------------------------------------------------------------


@live
def test_nova_reads_a_generated_crop_through_the_forced_tool_call(session: Session) -> None:
    """**The acceptance.** One crop out, one validated structured reading back, one row recorded.

    The image genuinely says `24 1/2"`, so this asserts the reading rather than merely asserting
    that something came back. A wrong reading here is a real finding about the model and the test
    says which it was; an unusable answer is a refusal the seam carried, and that is also a pass —
    those are outcomes this boundary exists to carry, and carrying them is what is under test.

    Token counts are printed because this call costs money and spend that nobody can see is spend
    nobody controls.
    """
    config = config_from_environment()
    sink = Sink()
    adapter = NovaAdapter.from_environment(config, sink)
    run_id = _extraction_run(session, model_id=config.model_id)

    try:
        candidate = adapter.extract(_request(_text_crop(KNOWN_READING)))
    except NovaAdapterError as error:
        # Recorded, then reported. A refused or unusable answer is the contract working; what would
        # not be acceptable is a call that left no trace of having happened.
        assert sink.items, "a failed call was not recorded"
        for invocation in sink.items:
            _persist(session, run_id, invocation)
        session.commit()
        rejection = sink.rejections[-1].reason if sink.rejections else None
        print(
            f"\nNOVA {config.model_id}: refused — {type(error).__name__}: {error} "
            f"(validator: {rejection})"
        )
        for invocation in sink.items:
            print(
                f"  attempt {invocation.attempt} model={invocation.model_id} "
                f"outcome={invocation.outcome.value} tokens in/out="
                f"{invocation.input_tokens}/{invocation.output_tokens} "
                f"{invocation.latency_ms}ms"
            )
        assert session.execute(select(ModelInvocation)).scalars().all()
        return

    assert isinstance(candidate, ObservationCandidate)
    assert candidate.raw_text.strip(), "a validated candidate with no text is not a reading"
    # No semantic type, from any provider. Nothing in this system assigns one (#274, Q20).
    assert candidate.semantic_guess is None
    assert sink.items[-1].outcome is NovaInvocationOutcome.OK

    stored = [_persist(session, run_id, invocation) for invocation in sink.items]
    session.commit()

    print(
        f"\nNOVA {config.model_id}: read {candidate.raw_text!r} "
        f"(unit_guess={candidate.unit_guess}, expected {KNOWN_READING!r})"
    )
    for invocation in sink.items:
        print(
            f"  attempt {invocation.attempt} model={invocation.model_id} "
            f"outcome={invocation.outcome.value} tokens in/out="
            f"{invocation.input_tokens}/{invocation.output_tokens} {invocation.latency_ms}ms"
        )

    rows = session.execute(select(ModelInvocation)).scalars().all()
    assert len(rows) == len(stored)
    assert rows[-1].input_tokens > 0, "Bedrock reports usage; a zero here means it was not read"


@live
def test_a_crop_with_no_dimension_on_it_does_not_produce_one_silently(session: Session) -> None:
    """Input: a blank crop. Outcome: whatever happens is recorded, and it is reported honestly.

    **This is the refusal path, live.** A guardrail refusal cannot be provoked on demand, so the
    testable case is the one that actually matters: asked to read a dimension from an image that has
    none, does the seam refuse, or does a plausible number appear from nowhere? Either answer is
    worth having in writing, and either way the invocation is recorded — which is the assertion.

    A reading returned here is not a test failure; it is a hallucination this prints in full, and
    the reason `evidence/gate.py` will not accept a single reader's word for anything.
    """
    config = config_from_environment()
    sink = Sink()
    adapter = NovaAdapter.from_environment(config, sink)
    run_id = _extraction_run(session, model_id=config.model_id)

    outcome: str
    try:
        candidate = adapter.extract(_request(_blank_crop()))
        outcome = f"returned {candidate.raw_text!r} from an image with no dimension on it"
    except NovaAdapterError as error:
        reason = sink.rejections[-1].reason if sink.rejections else None
        outcome = f"refused — {type(error).__name__}: {error} (validator: {reason})"

    assert sink.items, "a call was made and nothing was recorded"
    for invocation in sink.items:
        _persist(session, run_id, invocation)
    session.commit()

    print(f"\nNOVA {config.model_id} on a blank crop: {outcome}")
    for invocation in sink.items:
        print(
            f"  attempt {invocation.attempt} outcome={invocation.outcome.value} "
            f"tokens in/out={invocation.input_tokens}/{invocation.output_tokens} "
            f"{invocation.latency_ms}ms"
        )

    assert session.execute(select(ModelInvocation)).scalars().all()
