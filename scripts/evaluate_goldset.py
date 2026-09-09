"""Run the pipeline over a reviewed package and score it against the answer key.

**What this joins up.** `eval/gold_set/schema.py` defines the answer-key format and parses it,
`eval/gold_set/store.py` binds each answer to the drawing bytes it was written against,
`eval/metrics.py` computes the nine release metrics, and `eval/scorecard.py` compares a run with a
key. Until now nothing took a *package* — a drawing plus its answers — put it through the pipeline
that exists, and printed the result. `eval/harness.py:run_gold` refuses precisely because there was
no such path, and this is it.

It does not replace `scripts/run_gold_set.py`, which runs the **synthetic** lane: authored engine
operands, no drawing, proving the deterministic engine turns known inputs into the right verdict.
This runs the **package** lane: real bytes, the real reader, the real checks. Both lanes score
through the same `eval/metrics.py`, so neither has its own idea of what a metric means.

    # A synthetic package, generated here so the tool is runnable today with no client data:
    python scripts/evaluate_goldset.py --make-fixture data/goldset/synthetic-01
    python scripts/evaluate_goldset.py data/goldset/synthetic-01

**It is offline and it cannot change a verdict.** It creates a schema of its own, migrates it, runs
the stages, reads what they wrote, reports, and drops the schema. Nothing it does touches a live
package: the revision it creates is its own, and the scorecard is printed rather than stored.
`AGENTS.md` §2 keeps the deciding path free of anything that could be steered by a measurement of
it, and a grader that could write into that path would be exactly that.

*The private schema is not a nicety.* The first version migrated straight into whatever
`DATABASE_URL` named, which put 54 tables into `public` on the shared test database — and every
per-test schema sees `public` through its `search_path`, so the next test run starts finding rows it
did not create. It presents as `assert 7 == 2` or fifty failures that look like a regression in
whatever you wrote last. `tests/app/postgres_fixture.py` solved this for the suite; this does the
same thing for the same reason.

**The answer key supplies the reviewer's labels and never the values.** A reading has to be typed
before a rule can use it, and in production a person does that (`app/evidence/confirm.py`). Here the
answer key's `semantic_type` plays that part — it *is* a human label, written by whoever annotated
the case — so the run gets its types the way it would in a review, while every *number* still comes
from the pipeline's own reading of the drawing. Confirming a type is not supplying an answer, and
`confirm_candidate_type` carries the extractor's value through unchanged, which is what keeps reading
accuracy an honest measurement rather than a tautology.

**Nothing here is tuned against a real drawing.** The fixture is generated, and the thresholds the
reader needs are passed in from the command line with no defaults invented — #541 stays blocked and
this tool does not become the place a number quietly acquires a value.

Source: issue #553 · Verification: `tests/eval/test_evaluate_goldset.py`
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.gold_set.schema import GoldCase, ManifestLoadError
from eval.gold_set.store import content_hash
from eval.scorecard import render, score_package
from verdict.outcomes import Outcome, Severity

#: Where a project's reviewed packages live. Git-ignored, because they are client material: the
#: format and the loader are tracked code and the answers never are (`AGENTS.md` §9).
PROJECT_GOLDSET_DIRECTORY = Path("data/goldset")

#: The file inside a package directory that holds the answer key.
ANSWER_KEY = "answer_key.json"


def _pdf(text: str, *, box: bytes = b"[0 0 200 100]") -> bytes:
    """A one-page PDF whose content stream says `text`, assembled by hand.

    The same construction `tests/extraction/test_reader.py` uses, and for the same reason: every byte
    has a reason to be there, the cross-reference offsets are real, and nothing about it is a
    captured client drawing. A generated fixture is the only kind this project may hold — `AGENTS.md`
    §9 forbids inventing a *drawing* to tune against, and this invents a document to prove plumbing.
    """
    lines = text.split("|")
    content = b"".join(
        f"BT /F1 12 Tf 1 0 0 1 20 {70 - index * 30} Tm ("
        f"{line.replace(chr(92), chr(92) * 2).replace('(', chr(92) + '(').replace(')', chr(92) + ')')}"
        ") Tj ET\n".encode("latin-1")
        for index, line in enumerate(lines)
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox "
        + box
        + b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(start).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def make_fixture(directory: Path) -> Path:
    """Write a synthetic package — two drawings and an answer key — and return its path.

    **A fixture, not a drawing.** It exists so this tool is runnable and testable today with no
    client material, and so the format has a worked example Raj's answers can be mapped onto. The
    numbers in it are arbitrary and nothing is tuned against them.

    **Two answers, on purpose: one the reader should get and one it should not.** The drawing says
    `25.5"` — one token, which the reader handles — and `28 3/4"`, a mixed fraction with a space in
    it, which `extract_words` splits. A fixture where every answer matched would only prove the
    scorecard can say "yes", and the number that matters here is the one it gets wrong.

    The architectural drawing is present because `GoldCase` requires both — a match pairs an arch
    item with a shop item, and a case naming only one would be half unbound.
    """
    directory.mkdir(parents=True, exist_ok=True)
    shop = directory / "shop.pdf"
    arch = directory / "arch.pdf"
    shop.write_bytes(_pdf('25.5"|28 3/4"'))
    arch.write_bytes(_pdf('25.5"|28 3/4"'))

    key = {
        "id": directory.name,
        "product_type": "countertop",
        "arch": "arch.pdf",
        "shop": "shop.pdf",
        "ground_truth": {
            "observations": [
                {
                    # The reviewer's own label. This is how the system "learns the words": nothing
                    # infers a type, and this file is where a human's label is written down.
                    "semantic_type": "CT007",
                    "source": "SHOP",
                    "value": {"exact": "51/2", "unit": "in", "raw_text": '25.5"'},
                    "page": 1,
                    # Image pixels at the grader's dpi, the way an annotator boxes a region. These
                    # are where the generated drawing puts each label.
                    "polygon": [40, 55, 130, 85],
                    "item_id": "synthetic-item-1",
                },
                {
                    # **Deliberately a mixed fraction.** The generated drawing writes `28 3/4"` with
                    # a space in it, which is how a drawing writes one — and it is the shape that
                    # exposes a real reader weakness rather than a harness bug. A fixture where
                    # every answer matches would prove the scorecard can say "yes" and nothing else.
                    "semantic_type": "CT008",
                    "source": "SHOP",
                    "value": {"exact": "115/4", "unit": "in", "raw_text": '28 3/4"'},
                    "page": 1,
                    "polygon": [40, 118, 140, 148],
                    "item_id": "synthetic-item-1",
                },
            ],
            "matches": [],
            "expected_findings": [
                {
                    # **The reviewer's verdict, and the only check this fixture can decide.**
                    # `CT-SINK-OFFSET-FRONT-001` compares the front offset with the required value,
                    # which the rulebook defaults to 4 inches — so a reviewer looking at a 25.5-inch
                    # front offset fails it, and stating `FAIL` here is what makes verdict accuracy
                    # and the critical false-PASS rate measurable at all.
                    #
                    # Every other published check abstains on this fixture because its operands are
                    # not present, and the scorecard reports that as an abstention rather than as an
                    # error — which is the behaviour the whole harness exists to keep honest.
                    "check": "CT-SINK-OFFSET-FRONT-001",
                    "outcome": "FAIL",
                    "reason": "the front offset is 25.5 inches against a required 4",
                }
            ],
        },
        "provenance": {
            "annotator": "synthetic-fixture",
            "annotated_on": datetime.now(UTC).date().isoformat(),
            "documents": [
                {
                    "source": "SHOP",
                    "document_version_id": str(uuid4()),
                    "content_hash": content_hash(shop),
                }
            ],
        },
    }
    (directory / ANSWER_KEY).write_text(json.dumps(key, indent=2), encoding="utf-8")
    return directory


def load_package(directory: Path) -> GoldCase:
    """Read one package's answer key, refusing one whose drawing has changed underneath it.

    The staleness check is the same one `eval/gold_set/store.py` makes and for the same reason: an
    annotation is a statement about specific bytes, and scored against different bytes it is not
    missing or noisy but *confidently wrong* — and it reads as a passing gate.
    """
    path = directory / ANSWER_KEY
    if not path.is_file():
        raise ManifestLoadError(
            f"{path} does not exist. A package is a directory holding the drawings and an "
            f"{ANSWER_KEY} — see docs/GOLD_SET_FORMAT.md, or run --make-fixture to see one."
        )
    case = GoldCase.model_validate(json.loads(path.read_text(encoding="utf-8")))
    for document in case.provenance.documents:
        drawing = directory / (case.shop if document.source.value == "SHOP" else case.arch)
        if not drawing.is_file():
            raise ManifestLoadError(f"case {case.id!r} names {drawing}, which does not exist")
        actual = content_hash(drawing)
        if actual != document.content_hash:
            raise ManifestLoadError(
                f"case {case.id!r} was annotated against {document.content_hash} but {drawing} now "
                f"hashes to {actual}. The answers describe bytes that are no longer there, and "
                "scoring against them would manufacture evidence that the system works."
            )
    return case


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "package",
        nargs="?",
        type=Path,
        help=f"a package directory holding the drawings and {ANSWER_KEY}",
    )
    parser.add_argument(
        "--make-fixture",
        type=Path,
        metavar="DIR",
        help="write a synthetic package there and exit. Generated, never a client drawing",
    )
    parser.add_argument(
        "--candidate-scaffold-dir",
        type=Path,
        help=(
            "load pending candidate scaffolds and render an unmeasured scorecard without running the "
            "pipeline or applying semantic labels"
        ),
    )
    parser.add_argument(
        "--database-url",
        help="where to run the pipeline. Defaults to $DATABASE_URL. A schema of its own, then dropped",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=150,
        help="the reader's resolution. Stated, because stored coordinates depend on it",
    )
    return parser.parse_args()


@contextmanager
def _private_schema(database_url: str) -> Iterator[str]:
    """A migrated schema of this grader's own, dropped when it is done.

    Yields a URL whose `search_path` is `<schema>,public`. `public` stays on the path because the
    extensions' operator classes and types live there (migration `0034`), and a path without it
    cannot resolve `gin_trgm_ops` or `vector` — the schema comes first, so nothing it defines is
    shadowed.

    Dropped in a `finally` so an interrupted run leaves nothing behind. `CASCADE`, because the
    tables reference each other and any order that satisfied every foreign key would be one more
    thing to keep in step with the schema.
    """
    from sqlalchemy import create_engine
    from sqlalchemy import text as sql
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    schema = f"gv_eval_{uuid4().hex}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sql(f'CREATE SCHEMA "{schema}"'))
        try:
            yield url.update_query_dict(
                {"options": f"-csearch_path={schema},public"}
            ).render_as_string(hide_password=False)
        finally:
            with admin.connect() as connection:
                connection.execute(sql(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        admin.dispose()


def _run_pipeline(case: GoldCase, directory: Path, database_url: str, *, dpi: int) -> Any:
    """Put the package's shop drawing through the real stages and return what they wrote.

    Imported inside the function so `--make-fixture` and `--help` work without a database or the
    application's dependency tree.

    The chain is the production one: `extract_pages` reads the PDF and writes candidates,
    the answer key's human labels type them through `confirm_candidate_type`, and `run_checks`
    produces findings. Nothing is skipped and nothing is simulated — that is what makes the score a
    statement about the pipeline rather than about this script.
    """
    import yaml  # type: ignore[import-untyped]
    from sqlalchemy import create_engine, select

    from alembic import command
    from app.api.documents import storage_key
    from app.db.session import session_factory
    from app.evidence.confirm import confirm_candidate_type
    from app.models.document import (
        Document,
        DocumentVersion,
        PackageRevisionDocument,
        SourceArtifact,
    )
    from app.models.evidence import CanonicalObservation, ObservationCandidate
    from app.models.package import Package, PackageRevision, PackageState, Project
    from app.models.rules import RuleDefinition
    from app.models.rules import RuleSnapshot as RuleSnapshotRow
    from app.models.runs import TaskRun, WorkflowRun
    from app.models.verdicts import CheckRun
    from app.models.verdicts import Finding as FindingRow
    from rules.schema import Rule
    from rules.snapshot import publish
    from storage.local import LocalStore
    from tests.app.postgres_fixture import alembic_config
    from workflow.idempotency import stage_idempotency_key
    from workflow.review import ENGINE_VERSION
    from workflow.stages import DatabaseStages

    RULEBOOK = Path(__file__).resolve().parents[1] / "rules" / "rulebook"

    engine = create_engine(database_url)
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")

    data = (directory / case.shop).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    session = session_factory(engine)()
    with tempfile.TemporaryDirectory() as root:
        store = LocalStore(root=Path(root), ticket_secret=b"offline grader")

        project = Project(name=f"goldset {case.id}")
        session.add(project)
        session.flush()
        package = Package(project_id=project.id, vendor=None)
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
        # **The key the stage will look under, not one invented here.** `extract_pages` fetches by
        # `storage_key(document.id, digest)`; a grader that chose its own key would fail with a
        # missing integrity record and look like a storage bug.
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
        workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
        session.add(workflow_run)
        session.flush()
        for stage in ("extract_pages", "run_checks"):
            session.add(
                TaskRun(
                    workflow_run_id=workflow_run.id,
                    idempotency_key=stage_idempotency_key(
                        package_revision_id=revision.id,
                        stage=stage,
                        engine_version=ENGINE_VERSION,
                    ),
                    task_type=stage,
                    attempt=1,
                    outcome="claimed",
                )
            )
        session.flush()
        store.put(key, io.BytesIO(data), content_type="application/pdf")
        session.commit()

        # **The rulebook, published into this grader's own database.** `run_checks` reads
        # `rule_snapshots`, so a fresh schema has no rules and every check would be silently absent —
        # producing a scorecard with no verdicts and no false-PASS rate, which reads like a clean run.
        # The column names come from `scripts/run_checks.py`, which does this for real.
        published = 0
        for rule_path in sorted(RULEBOOK.glob("*.yaml")):
            rule = Rule.model_validate(yaml.safe_load(rule_path.read_text(encoding="utf-8")))
            snapshot = publish(rule)
            definition = RuleDefinition(rule_id=rule.id)
            session.add(definition)
            session.flush()
            session.add(
                RuleSnapshotRow(
                    rule_definition_id=definition.id,
                    snapshot_id=snapshot.snapshot_id,
                    version=rule.version,
                    canonical_json=snapshot.canonical_json,
                    product_type=rule.product_type.value,
                    check_type=rule.check_type.value,
                    unconfirmed_tolerance_count=0,
                )
            )
            published += 1
        session.commit()

        stages = DatabaseStages(store=store, dpi=dpi)
        stages.extract_pages(session, revision.id)
        session.commit()

        # **The answer key's labels do the typing a reviewer would do**, and each answer is paired
        # with a candidate **by where it sits on the sheet** — not by value, and not by position in a
        # list.
        #
        # The polygon is what an annotator's file carries for exactly this: it says *this reading,
        # here*. Pairing by value would be scoring the key against itself, and pairing by list order
        # would hand the first answer whichever reading the extractor happened to emit first, so a
        # mislabelled reading would then be graded as a wrong number.
        #
        # One candidate per answer: a candidate already labelled is not offered again, because a
        # reviewer confirming two different quantities from one reading is not a thing that happens.
        typed = 0
        candidates = [
            candidate
            for candidate in session.execute(
                select(ObservationCandidate).where(
                    ObservationCandidate.document_version_id == version.id
                )
            ).scalars()
            # Only readings that carry a value. A bare number was recorded without one deliberately,
            # and there is nothing for a reviewer to confirm about it.
            if candidate.value_numerator is not None and candidate.polygon
        ]
        used: set[UUID] = set()
        for answer in case.ground_truth.observations:
            nearest = _nearest_candidate(answer, candidates, used)
            if nearest is None:
                continue
            result = confirm_candidate_type(
                session,
                candidate_id=nearest.id,
                semantic_type=answer.semantic_type.value,
                confirmed_by="offline-grader",
            )
            if isinstance(result, CanonicalObservation):
                used.add(nearest.id)
                typed += 1
        session.commit()

        stages.run_checks(session, revision.id)
        session.commit()

        # **The rule id comes through the snapshot, not off the finding.** A finding names its
        # `check_run`, which names the published snapshot, which names the rule definition — the same
        # join `app/api/findings.py` makes. Reading a `rule_id` attribute off the row would be
        # inventing a column, and the answer key pairs its expectations on that id.
        findings = list(
            session.execute(
                select(
                    RuleDefinition.rule_id,
                    RuleSnapshotRow.version,
                    RuleSnapshotRow.snapshot_id,
                    FindingRow.outcome,
                    FindingRow.severity,
                    FindingRow.reason,
                    CheckRun.engine_version,
                )
                .select_from(FindingRow)
                .join(CheckRun, CheckRun.id == FindingRow.check_run_id)
                .join(RuleSnapshotRow, RuleSnapshotRow.id == CheckRun.rule_snapshot_id)
                .join(RuleDefinition, RuleDefinition.id == RuleSnapshotRow.rule_definition_id)
                .where(FindingRow.package_revision_id == revision.id)
            ).all()
        )
        observations = list(
            session.execute(
                select(CanonicalObservation).where(
                    CanonicalObservation.document_version_id == version.id
                )
            ).scalars()
        )
        return findings, observations, typed, published, session


def _nearest_candidate(answer: Any, candidates: Any, used: set[UUID]) -> Any:
    """The unlabelled candidate whose box centre is nearest this answer's, or `None`.

    Distances are compared **squared**, so no square root and no float decides which reading gets a
    reviewer's label — the same reason `extraction/geometry/text_association.py` keeps its distances
    squared. Both boxes are in image pixels: a candidate's `polygon` column is image space by
    construction, and `GoldObservation.polygon` is how an annotator boxed the region.

    No proximity limit. This is a grader with a stated answer per reading, not the association step
    deciding which line a number belongs to — there is nothing here to abstain in favour of, and an
    answer left unpaired is reported as a reading the system did not make.
    """
    left, top, right, bottom = answer.polygon
    target = ((left + right) / 2, (top + bottom) / 2)

    best = None
    best_distance = None
    for candidate in candidates:
        if candidate.id in used:
            continue
        xs = [int(point[0]) for point in candidate.polygon]
        ys = [int(point[1]) for point in candidate.polygon]
        centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
        distance = (centre[0] - target[0]) ** 2 + (centre[1] - target[1]) ** 2
        if best_distance is None or distance < best_distance:
            best, best_distance = candidate, distance
    return best


def main() -> int:
    arguments = _arguments()

    if arguments.make_fixture is not None:
        written = make_fixture(arguments.make_fixture)
        print(f"wrote a synthetic package to {written}")
        print(f"  {written / ANSWER_KEY} — the answer key, in the documented format")
        print("  run it:  python scripts/evaluate_goldset.py " + str(written))
        return 0

    if arguments.candidate_scaffold_dir is not None:
        if arguments.package is not None:
            print("give either a package or --candidate-scaffold-dir, not both", file=sys.stderr)
            return 2
        from scripts.scaffold_reviewed_goldset_candidates import dry_run_candidate_scaffolds

        try:
            print(dry_run_candidate_scaffolds(arguments.candidate_scaffold_dir))
        except (OSError, ValueError, json.JSONDecodeError) as refused:
            print(f"the candidate scaffold was refused: {refused}", file=sys.stderr)
            return 2
        return 0

    if arguments.package is None:
        print("give a package directory, or --make-fixture DIR to generate one", file=sys.stderr)
        return 2

    database_url = arguments.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "no database. This grader runs the real pipeline, which stores what it reads, so it "
            "needs one:\n  DATABASE_URL=postgresql+psycopg://... python "
            "scripts/evaluate_goldset.py <package>\nIt uses its own rows and never touches a live "
            "package.",
            file=sys.stderr,
        )
        return 2

    try:
        case = load_package(arguments.package)
    except ManifestLoadError as refused:
        print(f"the package was refused: {refused}", file=sys.stderr)
        return 2

    # Scoring happens inside the `with`: `_as_gold` reads the page transform back out of the rows
    # the run wrote, so the schema has to outlive the pipeline call itself.
    with _private_schema(database_url) as scoped_url:
        findings, observations, typed, published, session = _run_pipeline(
            case, arguments.package, scoped_url, dpi=arguments.dpi
        )
        print(
            f"ran the pipeline: {published} rule(s) published, {typed} answer-key label(s) "
            f"applied, {len(observations)} confirmed observation(s), {len(findings)} finding(s)\n"
        )
        scorecard = score_package(
            case, _as_domain(findings), observations=_as_gold(session, observations, case)
        )
        session.close()
    print(render(scorecard))
    # Exit 1 on a critical false PASS — the one result that must stop something. Every other number
    # is information; this one is the ship gate.
    return 1 if scorecard.critical_false_passes else 0


@dataclass(frozen=True, slots=True)
class StoredFinding:
    """One finding as the database holds it, carrying exactly what scoring reads.

    Not a `verdict.finding.Finding`: that type refuses to exist without a `CalculationTrace` for any
    non-abstention, and the stored trace is JSON with no inverse. Building one would mean inventing
    operands and a comparison this code never performed — fabricating a calculation to satisfy a
    type whose whole purpose is to prevent fabricated calculations. `eval.scorecard.ScoredFinding`
    states the three fields the metrics read, and this satisfies it honestly.
    """

    rule_id: str
    outcome: Outcome
    severity: Severity
    reason: str


def _as_domain(rows: Any) -> Any:
    """Stored finding rows in the shape the scorer reads.

    The rule id, version and engine version came off the joined query rather than off the finding
    row, because a finding names its check run and the check run names the published snapshot — a
    `rule_id` attribute on the row would be a column that does not exist.
    """
    return [
        StoredFinding(
            rule_id=row.rule_id,
            outcome=Outcome(row.outcome),
            severity=Severity(row.severity),
            reason=row.reason or "",
        )
        for row in rows
    ]


def _as_gold(session: Any, rows: Any, case: GoldCase) -> Any:
    """The system's own confirmed readings, expressed in the answer key's shape.

    Converted so the two are comparable, and **never** filled in from the case: an answer the
    pipeline did not produce stays absent, which `eval/scorecard.py` records as `missing` rather than
    as a wrong value.

    **The polygon is converted, not approximated.** A canonical observation's polygon is in *stored*
    coordinates — normalised `0..1` against the visible crop box — while an answer key's is in image
    pixels, the way an annotator boxes a region. Migration `0037` persists the media box, the crop box
    and the run's dpi for exactly this trip, and `PageTransform.from_stored` makes it. Scaling by the
    page size instead is right for most PDFs and silently wrong for the rest, and being silently
    wrong here would place a reading on a region nobody wrote and then score localisation against it.

    A reading whose transform was never recorded is **dropped rather than guessed**: it is one the
    grader cannot place, and a fabricated box would go straight into `evidence_localisation_rate`.
    """
    from fractions import Fraction

    from sqlalchemy import select

    from app.models.document import Page
    from app.models.runs import ExtractionRun
    from eval.gold_set.schema import GoldObservation
    from evidence.coordinates import PageTransform, StoredPoint
    from rules.semantic_types import OperandSource, SemanticType
    from units.measurement import Measurement, Unit

    by_item = {answer.semantic_type: answer.item_id for answer in case.ground_truth.observations}
    built = []
    for row in rows:
        if row.semantic_type is None or row.value_numerator is None:
            continue
        page = session.get(Page, row.page_id)
        run_dpi = session.execute(
            select(ExtractionRun.dpi).where(ExtractionRun.dpi.isnot(None)).limit(1)
        ).scalar_one_or_none()
        if page is None or page.media_box is None or page.crop_box is None or run_dpi is None:
            continue
        media = [Decimal(value) for value in page.media_box]
        crop = [Decimal(value) for value in page.crop_box]
        if len(media) != 4 or len(crop) != 4:
            # A stored box that is not four numbers is a row this grader cannot place a reading
            # from. Dropped, for the reason the docstring gives.
            continue
        transform = PageTransform(
            dpi=int(run_dpi),
            rotation=page.rotation or 0,
            media_box=(media[0], media[1], media[2], media[3]),
            crop_box=(crop[0], crop[1], crop[2], crop[3]),
        )
        pixels = [
            transform.from_stored(StoredPoint(Decimal(str(point[0])), Decimal(str(point[1]))))
            for point in row.polygon
        ]
        xs = [point.x for point in pixels]
        ys = [point.y for point in pixels]
        semantic = SemanticType(row.semantic_type)
        built.append(
            GoldObservation(
                semantic_type=semantic,
                source=OperandSource.SHOP,
                value=Measurement(
                    exact=Fraction(int(row.value_numerator), int(row.value_denominator)),
                    unit=Unit(row.unit),
                    raw_text=None,
                ),
                page=int(page.index) + 1,
                polygon=(min(xs), min(ys), max(xs), max(ys)),
                # The annotator's own name for the item, matched on the reviewer's type. A type the
                # key does not mention gets a marker rather than a plausible id: `GoldObservation`
                # requires one, and a made-up identifier would enter the file every metric is
                # measured against.
                item_id=by_item.get(semantic, "unmatched-item"),
            )
        )
    return built


if __name__ == "__main__":
    raise SystemExit(main())
