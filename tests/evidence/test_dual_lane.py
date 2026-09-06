"""The corroboration lane that needs no second reader (#528).

Verification for: the dual-unit path through `app/evidence/record.py` — `_parse`, `_dual` and
`_corroboration` — and the columns migration `0036` added.

A drawing that writes `984 [38 3/4]` states one dimension twice, in two units, in one token. Comparing
the two corroborates that the inch reading was *read* correctly, which `AGENTS.md` names as one of the
only two ways a single reader can qualify evidence. Everything that decides agreement was already
built and had no caller.

**What this does not do.** It attaches no meaning. A corroborated reading is still a reading of an
unknown quantity, and `test_the_lane_never_assigns_a_meaning` is the guard.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.db.session import session_factory
from app.evidence.record import UNKNOWN_UNIT_FLAG, UNPARSED_FLAG
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
from app.models.runs import TaskRun, WorkflowRun
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import _pdf
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

#: `984 mm` and `38 3/4"` are the same dimension: 984 mm is 38.74 inches, and each reading is
#: rounded to its own precision. The lane's job is to notice that they agree *within* that.
AGREEING = '38 3/4" is written as 984 [38 3/4] on this sheet'

#: A token whose two halves cannot both be true. 984 mm is not 12 1/2 inches by any rounding.
DISAGREEING = "984 [12 1/2]"


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


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _drawing(*tokens: str) -> bytes:
    """A one-page PDF whose text runs are exactly the tokens given."""
    body = b""
    for index, token in enumerate(tokens):
        y = 70 - index * 15
        body += f"BT /F1 10 Tf 1 0 0 1 20 {y} Tm ({token}) Tj ET\n".encode()
    return _pdf(body)


def _revision(session: Session, store: LocalStore, *, data: bytes) -> PackageRevision:
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name=f"dual {uuid4()}")
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
    document = Document(package_id=package.id, kind="shop")
    session.add(document)
    session.flush()
    key = storage_key(document.id, digest)
    session.add(SourceArtifact(storage_key=key, sha256=digest, size=len(data)))
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=session.execute(
            select(SourceArtifact.id).where(SourceArtifact.storage_key == key)
        ).scalar_one(),
        sha256=digest,
        page_count=1,
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
    store.put(key, io.BytesIO(data), content_type="application/pdf")
    return revision


def _by_text(session: Session) -> dict[str, ObservationCandidate]:
    return {
        candidate.raw_text: candidate
        for candidate in session.execute(select(ObservationCandidate)).scalars()
    }


def _read(session: Session, store: LocalStore, *tokens: str) -> dict[str, ObservationCandidate]:
    revision = _revision(session, store, data=_drawing(*tokens))
    DatabaseStages(store).extract_pages(session, revision.id)
    return _by_text(session)


# ---------------------------------------------------------------------------
# The reading itself
# ---------------------------------------------------------------------------


def test_a_dual_token_keeps_the_inch_reading_as_its_value(
    session: Session, store: LocalStore
) -> None:
    """Q12: inches govern. Millimetres are the vendor's machine reference and never a verdict operand.

    Before this the whole token was lost — `normalise_to_inches` cannot read `984 [38 3/4]`, so the
    candidate was stored with no value at all and an `unparsed` flag. The drawing stated the
    dimension twice and the system kept neither half.
    """
    candidates = _read(session, store, "984 [38 3/4]")

    candidate = candidates["984 [38 3/4]"]
    assert candidate.unit == "in", "the millimetre half was stored as the value"
    assert candidate.value_numerator == 155
    assert candidate.value_denominator == 4
    assert UNPARSED_FLAG not in candidate.ambiguity_flags


def test_a_bare_number_still_has_no_value(session: Session, store: LocalStore) -> None:
    """**The regression this lane could easily have caused.**

    `parse_dual` accepts a lone millimetre number, so reading every token through it would make a
    bare `984` parse as 984 mm — and then as 984 *inches*, 82 feet, which is the exact bug
    `UNKNOWN_UNIT_FLAG` exists to document. Only a bracketed pair counts as dual.
    """
    candidates = _read(session, store, "984")

    candidate = candidates["984"]
    assert candidate.value_numerator is None
    assert candidate.unit is None
    assert UNKNOWN_UNIT_FLAG in candidate.ambiguity_flags
    assert candidate.corroboration_lane is None


# ---------------------------------------------------------------------------
# What the lane concludes
# ---------------------------------------------------------------------------


def test_two_agreeing_readings_are_recorded_as_the_lane_having_run(
    session: Session, store: LocalStore
) -> None:
    """Agreement is detected, and named as the dual-unit lane.

    It does **not** promote to `CORROBORATED`, and that is correct rather than a shortfall:
    `evidence/corroborate.py` promotes only when a semantic type is known, and candidates are
    deliberately untyped until #274 and Q20. The lane ran and agreed; what it corroborated is a
    reading whose meaning nobody has established. Promotion lights up on its own the day typing
    exists, with no change here.
    """
    candidates = _read(session, store, "984 [38 3/4]")

    candidate = candidates["984 [38 3/4]"]
    assert candidate.corroboration_lane == "DUAL_UNIT"
    assert candidate.corroboration_status == "RAW_CANDIDATE"


def test_two_disagreeing_readings_are_surfaced_as_conflicting(
    session: Session, store: LocalStore
) -> None:
    """**The half that is useful today.** A drawing contradicting itself is a reviewer's problem now.

    984 mm is not 12 1/2 inches by any rounding. Nothing here resolves it — not by confidence, not by
    preferring one unit — because a conflict resolved by preference is a conflict hidden. It is
    recorded as `CONFLICTING` and stays that way until a person looks.
    """
    candidates = _read(session, store, DISAGREEING)

    candidate = candidates[DISAGREEING]
    assert candidate.corroboration_status == "CONFLICTING"
    assert candidate.corroboration_lane == "DUAL_UNIT"
    # The reading is still kept. A conflicting reading that vanished would be indistinguishable
    # from a dimension the drawing never stated.
    assert candidate.value_numerator == 25
    assert candidate.value_denominator == 2


def test_a_single_reading_token_says_no_lane_applied(session: Session, store: LocalStore) -> None:
    """Most tokens state one reading, and both columns stay null.

    Null rather than a status meaning "nothing found": no lane examined this reading, which is a
    different fact from a lane examining it and reaching no conclusion.
    """
    # `extract_words` splits `38 3/4"` at the space, so the reader emits `38` and `3/4"` — only the
    # second carries its own unit marker and therefore a value. That splitting is exactly what the
    # dual-token path above exists to undo, and leaving it visible here is the point: this is what an
    # ordinary single-reading dimension looks like coming out of the reader today.
    candidates = _read(session, store, '38 3/4"')

    candidate = candidates['3/4"']
    assert candidate.value_numerator == 3
    assert candidate.value_denominator == 4
    assert candidate.corroboration_status is None
    assert candidate.corroboration_lane is None


def test_conflicts_and_agreements_survive_together_on_one_page(
    session: Session, store: LocalStore
) -> None:
    """One page, both outcomes, neither swallowing the other.

    Asserted because the tempting implementation short-circuits: a page with any conflict reporting
    only the conflict, or a page with any agreement reporting only that, would each pass the two
    single-token tests above.
    """
    candidates = _read(session, store, "984 [38 3/4]", DISAGREEING, "1000 [39 3/8]")

    statuses = {text: row.corroboration_status for text, row in candidates.items()}
    assert statuses["984 [38 3/4]"] == "RAW_CANDIDATE"
    assert statuses[DISAGREEING] == "CONFLICTING"
    assert statuses["1000 [39 3/8]"] == "RAW_CANDIDATE"


# ---------------------------------------------------------------------------
# The line this lane does not cross
# ---------------------------------------------------------------------------


def test_the_lane_never_assigns_a_meaning(session: Session, store: LocalStore) -> None:
    """**The hard stop.** Corroborating a reading says nothing about what was read.

    The dual-unit lane qualifies that `38 3/4` was read correctly off the sheet. Whether that number
    is a countertop depth, a cabinet width or a sink offset is the semantic question, and it needs
    the real drawings (#274) and the vocabulary Q20 defers. A lane that quietly filled in
    `semantic_guess` on its way past would look like progress and be a fabricated fact.
    """
    candidates = _read(session, store, "984 [38 3/4]", DISAGREEING)

    assert {row.semantic_guess for row in candidates.values()} == {None}


def test_a_status_and_a_lane_are_written_together_or_not_at_all(
    session: Session, store: LocalStore
) -> None:
    """A lane with no finding, or a finding from no lane, is a row nobody can interpret.

    The database enforces the pairing; this asserts the writer honours it, over a page carrying both
    kinds of token.
    """
    candidates = _read(session, store, "984 [38 3/4]", '38 3/4"', "984")

    for row in candidates.values():
        assert (row.corroboration_status is None) == (row.corroboration_lane is None)


def test_a_dual_token_is_one_reading_not_four(session: Session, store: LocalStore) -> None:
    """The fragments the word splitter made are not recorded beside the token it made them from.

    `984 [38 3/4]` becomes `984`, `[38` and `3/4]` under `extract_words`. Recording those *as well*
    as the merged token would put one dimension on the page four times: four candidates, four crops,
    and three of them readings of half a bracket. Two of the fragments carry no value at all, so they
    would sit in a reviewer's list as unread dimensions that were never dimensions.

    Asserted by counting, because every other test here looks a candidate up by its text and passes
    whether or not the fragments are there beside it.
    """
    candidates = _read(session, store, "DEPTH 984 [38 3/4] TYP")

    assert set(candidates) == {"DEPTH", "984 [38 3/4]", "TYP"}
    assert len(candidates) == 3, f"the token was recorded in pieces as well: {sorted(candidates)}"
