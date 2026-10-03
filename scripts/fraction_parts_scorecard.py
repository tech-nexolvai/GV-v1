#!/usr/bin/env python3
"""Score the fraction-parts route on a human-read key's stacked crops and #848's by-eye labels (#865).

**It runs the route itself**: the key's drawing goes through `DatabaseStages.extract_pages` with the
route on, the production OCR engine reading each piece first and the named vision reader seconding
it through the production adapter. Every call that reader makes is recorded in `model_invocations`
and counted against the budget stated here, through the stage's own meter; once it is spent, no
more calls are made. The vision route is not what is measured, so its whole-crop requests are
declined without a call.

**Nothing is defaulted** — the drawing settings, the reader settings, the second reader, the budget
and the price file are all stated, and the scorecard says which were used. Give it a scratch
database of its own: the script migrates it and writes the run into it, where every call stays on
the record. The by-eye labels and the scorecard hold the client's values: keep both under
`data/`, which is never committed.

    python scripts/fraction_parts_scorecard.py data/goldset/reading-key-2026-09-30 \\
        --by-eye data/goldset/reading-key-2026-09-30/fraction_parts_by_eye.csv \\
        --database-url postgresql+psycopg://gv:gv@localhost:5433/gv865 \\
        --reader-settings scripts/demo.sh --key-dpi 600 --stage-dpi 300 \\
        --height-px 40 --stroke-px 4 --bezier-steps 8 --margin-px 32 \\
        --second-reader bedrock-ministral-3-3b --budget-usd 1 \\
        --rates deploy/model_rates.us-east-1.json \\
        --output data/goldset/reading-key-2026-09-30/fraction_parts_scorecard.md

Source: issue #865 · Verification: `tests/eval/test_fraction_parts_scorecard.py`
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from eval.experiments.agent_scorecard import ScorecardError, key_frame_dpi, load_key
from eval.experiments.fraction_parts_scorecard import (
    PreFill,
    key_labels,
    load_by_eye,
    render_markdown,
    results_json,
    score,
)
from evidence.candidate import ObservationCandidate as DomainCandidate
from extraction.models.nova import (
    NovaConfig,
    NovaDigitsRequest,
    NovaRequest,
    NovaServiceError,
)

if TYPE_CHECKING:
    from extraction.fraction_parts import PieceDrawing
    from workflow.association import AssociationSettings, LocalizedOcrSettings

#: The reader settings the stage needs, read from a `scripts/demo.sh`-style file, each required.
READER_SETTINGS: Final = (
    "GV_READER_LINE_MINIMUM_PT",
    "GV_READER_GLYPH_MAXIMUM_PT",
    "GV_READER_GLYPH_GAP_PT",
    "GV_READER_PROXIMITY_LIMIT",
    "GV_READER_AMBIGUITY_MARGIN",
    "GV_READER_WITNESS_TOLERANCE",
    "GV_READER_MINIMUM_SPAN",
    "GV_READER_STRAIGHTNESS",
    "GV_READER_CROSSING_MARGIN",
    "GV_READER_LOCALIZED_MINIMUM_PATHS",
    "GV_READER_LOCALIZED_MAXIMUM_SPAN",
    "GV_READER_LOCALIZED_CROP_MARGIN_PT",
    "GV_READER_FRACTION_BAR_THICKNESS_MAX_PT",
    "GV_READER_FRACTION_BAR_LENGTH_MIN_PT",
    "GV_READER_FRACTION_REACH_PT",
    "GV_READER_FRACTION_GLYPH_MIN_PT",
    "GV_READER_FRACTION_GLYPH_MAX_PT",
    "GV_READER_FRACTION_PROPORTION_MAX",
    "GV_READER_FRACTION_CHARACTER_GAP_PT",
)


def read_stated(path: Path, names: tuple[str, ...]) -> dict[str, str]:
    """`NAME=value` lines from a demo.sh-style file, for exactly `names`; each one required."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ScorecardError(f"could not read the reader settings in {path}: {error}") from error
    found: dict[str, str] = {}
    for name in names:
        match = re.search(rf"^\s*{name}=(\S+)", text, flags=re.MULTILINE)
        if match is None:
            raise ScorecardError(f"{path} does not state {name}, and it has no default")
        found[name] = match.group(1).rstrip("\\").strip()
    return found


@dataclass(frozen=True, slots=True)
class PiecesOnly:
    """The gate reader for this measurement, asked only for drawn pieces.

    `read_digits` is the production reader's. `extract` — the vision route's whole-crop request —
    is declined before any call, because this scorecard measures the fraction-parts route alone and
    a call it does not need is money it does not need to spend.
    """

    config: NovaConfig

    def extract(self, request: NovaRequest, recorder: object) -> DomainCandidate:
        del request, recorder
        raise NovaServiceError(
            "declined without a call: this scorecard measures the fraction-parts route only"
        )

    def read_digits(self, request: NovaDigitsRequest, recorder: object) -> str:
        from workflow.stages import BedrockVisionReader

        return BedrockVisionReader(self.config).read_digits(request, recorder)  # type: ignore[arg-type]


def _box(polygon: list[list[int]]) -> tuple[Fraction, Fraction, Fraction, Fraction]:
    xs = [Fraction(point[0]) for point in polygon]
    ys = [Fraction(point[1]) for point in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("key", type=Path, help="a key directory: answer_key.json, crops.csv, PDF")
    parser.add_argument("--by-eye", type=Path, required=True, help="#848's by-eye labels (CSV)")
    parser.add_argument("--database-url", required=True, help="a scratch database of its own")
    parser.add_argument("--reader-settings", type=Path, required=True)
    parser.add_argument(
        "--key-dpi",
        type=int,
        help="the frame crops.csv is in, only for a key that does not record one (#835)",
    )
    parser.add_argument("--stage-dpi", type=int, required=True)
    parser.add_argument("--height-px", type=int, required=True)
    parser.add_argument("--stroke-px", type=int, required=True)
    parser.add_argument("--bezier-steps", type=int, required=True)
    parser.add_argument("--margin-px", type=int, required=True)
    parser.add_argument("--second-reader", required=True, help="a defined vision reader's name")
    parser.add_argument("--budget-usd", type=Decimal, required=True)
    parser.add_argument("--rates", type=Path, required=True, help="the price file the cap reads")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    from app.runs.rates import MODEL_RATES_ENV, load_model_rates
    from extraction.fraction_parts import PieceDrawing
    from extraction.glyph_bands import FractionBarGeometry
    from extraction.models.nova import vision_config_for_extractor
    from workflow.association import AssociationSettings, LocalizedOcrSettings
    from workflow.stages import VISION_CROP_CONTEXT_MARGIN_PT

    try:
        crops = load_key(args.key)
        key_dpi = key_frame_dpi(
            args.key, key_dpi=args.key_dpi, margin_pt=VISION_CROP_CONTEXT_MARGIN_PT
        )
        key = key_labels(crops, key_dpi=key_dpi, run_dpi=args.stage_dpi)
        by_eye = load_by_eye(args.by_eye, run_dpi=args.stage_dpi)
        case = json.loads((args.key / "answer_key.json").read_text(encoding="utf-8"))
        pdf = (args.key / case["shop"]).read_bytes()
        stated = read_stated(args.reader_settings, READER_SETTINGS)
        drawing = PieceDrawing(
            height_px=args.height_px,
            stroke_px=args.stroke_px,
            bezier_steps=args.bezier_steps,
            margin_px=args.margin_px,
        )
        config = vision_config_for_extractor(args.second_reader)
        if config is None:
            raise ScorecardError(f"{args.second_reader} is not a defined reader")
        rates = load_model_rates(args.rates)
        # **The cap holds only for priced calls** (#700): an unpriced call adds nothing to the
        # meter, so a reader the price file does not name could spend without limit.
        if rates.rate_for(config.model_id) is None:
            raise ScorecardError(f"{args.rates} has no price for {config.model_id}")
        if not args.budget_usd.is_finite() or args.budget_usd <= 0:
            raise ScorecardError("the budget must be more than zero dollars")
    except (ScorecardError, OSError, KeyError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2

    os.environ[MODEL_RATES_ENV] = str(args.rates)
    association = AssociationSettings(
        line_minimum_pt=Decimal(stated["GV_READER_LINE_MINIMUM_PT"]),
        glyph_maximum_pt=Decimal(stated["GV_READER_GLYPH_MAXIMUM_PT"]),
        glyph_gap_pt=Decimal(stated["GV_READER_GLYPH_GAP_PT"]),
        proximity_limit=Decimal(stated["GV_READER_PROXIMITY_LIMIT"]),
        ambiguity_margin=Decimal(stated["GV_READER_AMBIGUITY_MARGIN"]),
        witness_tolerance=Decimal(stated["GV_READER_WITNESS_TOLERANCE"]),
        minimum_span=Decimal(stated["GV_READER_MINIMUM_SPAN"]),
        straightness=Decimal(stated["GV_READER_STRAIGHTNESS"]),
        crossing_margin=Decimal(stated["GV_READER_CROSSING_MARGIN"]),
        fraction_bar=FractionBarGeometry(
            bar_thickness_max_pt=Decimal(stated["GV_READER_FRACTION_BAR_THICKNESS_MAX_PT"]),
            bar_length_min_pt=Decimal(stated["GV_READER_FRACTION_BAR_LENGTH_MIN_PT"]),
            reach_pt=Decimal(stated["GV_READER_FRACTION_REACH_PT"]),
            glyph_min_pt=Decimal(stated["GV_READER_FRACTION_GLYPH_MIN_PT"]),
            glyph_max_pt=Decimal(stated["GV_READER_FRACTION_GLYPH_MAX_PT"]),
            proportion_max=Decimal(stated["GV_READER_FRACTION_PROPORTION_MAX"]),
            character_gap_pt=Decimal(stated["GV_READER_FRACTION_CHARACTER_GAP_PT"]),
        ),
    )
    localized = LocalizedOcrSettings(
        minimum_paths=int(stated["GV_READER_LOCALIZED_MINIMUM_PATHS"]),
        maximum_span=Decimal(stated["GV_READER_LOCALIZED_MAXIMUM_SPAN"]),
        crop_margin_pt=Decimal(stated["GV_READER_LOCALIZED_CROP_MARGIN_PT"]),
    )

    pre_fills, refusals, spend = _run(
        args.database_url,
        pdf,
        dpi=args.stage_dpi,
        association=association,
        localized=localized,
        drawing=drawing,
        reader=PiecesOnly(config),
        budget=args.budget_usd,
    )
    card = score(pre_fills, key, by_eye)
    header = (
        f"## Fraction-parts scorecard — {datetime.datetime.now().astimezone().date().isoformat()}"
        "\n\n"
        f"Drawing: {case['shop']}, through `DatabaseStages.extract_pages` at {args.stage_dpi} dpi "
        f"with the route on. Pieces drawn at {drawing.config_hash}. First reader: the production "
        f"OCR engine. Second reader: {config.extractor} ({config.model_id}), through the production "
        f"adapter, on a ${args.budget_usd} budget priced by {args.rates}; the vision route's "
        "whole-crop requests were declined without a call. Reader settings: "
        f"{args.reader_settings}. Judged against the person's key in {args.key} (stacked group, "
        f"{len(key)} crops) and #848's by-eye labels in {args.by_eye} ({len(by_eye)})."
    )
    args.output.write_text(
        render_markdown(card, header=header, refusals=refusals, spend=spend), encoding="utf-8"
    )
    args.output.with_suffix(".json").write_text(
        json.dumps({**results_json(card), "refusals": refusals, "spend": spend}, indent=1),
        encoding="utf-8",
    )
    print(f"wrote {args.output} and {args.output.with_suffix('.json')}", file=sys.stderr)
    return 0


def _run(
    database_url: str,
    pdf: bytes,
    *,
    dpi: int,
    association: AssociationSettings,
    localized: LocalizedOcrSettings,
    drawing: PieceDrawing,
    reader: PiecesOnly,
    budget: Decimal,
) -> tuple[list[PreFill], dict[str, int], dict[str, object]]:
    """Put the drawing through the stage with the route on; return its pre-fills, the page results'
    refusal reasons, and what the second reader's calls cost, read back from `model_invocations`."""
    from alembic.config import Config
    from sqlalchemy import create_engine, func, select

    from alembic import command
    from app.api.documents import storage_key
    from app.db.session import session_factory
    from app.models.document import (
        Document,
        DocumentVersion,
        PackageRevisionDocument,
        Page,
        SourceArtifact,
    )
    from app.models.evidence import ObservationCandidate
    from app.models.package import Package, PackageRevision, PackageState, Project
    from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
    from extraction.fraction_parts import FRACTION_PARTS_EXTRACTOR
    from extraction.ocr import RapidOcrEngine
    from storage.local import LocalStore
    from workflow.idempotency import stage_idempotency_key
    from workflow.review import ENGINE_VERSION
    from workflow.stages import DatabaseStages

    engine = create_engine(database_url)
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")

    digest = hashlib.sha256(pdf).hexdigest()
    session = session_factory(engine)()
    with tempfile.TemporaryDirectory() as root:
        store = LocalStore(root=Path(root), ticket_secret=b"fraction-parts scorecard")
        project = Project(name="fraction-parts scorecard")
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
        key = storage_key(document.id, digest)
        artifact = SourceArtifact(storage_key=key, sha256=digest, size=len(pdf))
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
        task_run = TaskRun(
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
        session.add(task_run)
        session.flush()
        store.put(key, io.BytesIO(pdf), content_type="application/pdf")
        session.commit()

        stages = DatabaseStages(
            store,
            dpi=dpi,
            association=association,
            localized_ocr=localized,
            ocr_engine=RapidOcrEngine(),
            vision_readers=(reader,),
            vision_gate=reader.config.extractor,
            layout_readers=(),
            ai_budget_usd=budget,
            fraction_parts=drawing,
        )
        results = stages.extract_pages(session, revision.id)
        session.commit()

    task_run_id: UUID = task_run.id
    rows = session.execute(
        select(ObservationCandidate, Page.index)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .where(
            ExtractionRun.extractor == FRACTION_PARTS_EXTRACTOR,
            ExtractionRun.task_run_id == task_run_id,
        )
    ).all()
    pre_fills = [
        PreFill(
            page_index=index,
            box=_box(row.polygon),
            text=row.raw_text,
            value=Fraction(row.value_numerator, row.value_denominator),
        )
        for row, index in rows
    ]
    refusals: Counter[str] = Counter()
    for result in results:
        lines = result.payload.get("fraction_parts_refusal_reasons")
        for line in lines if isinstance(lines, list) else []:
            count, reason = str(line).split(" × ", 1)
            refusals[reason] += int(count)
    calls = session.execute(
        select(
            ModelInvocation.prompt_id,
            ModelInvocation.outcome,
            func.count(),
            func.coalesce(func.sum(ModelInvocation.cost_micros), 0),
            func.count().filter(ModelInvocation.cost_micros.is_(None)),
        )
        .join(ExtractionRun, ExtractionRun.id == ModelInvocation.extraction_run_id)
        .where(ExtractionRun.task_run_id == task_run_id)
        .group_by(ModelInvocation.prompt_id, ModelInvocation.outcome)
    ).all()
    spent = sum(int(row[3]) for row in calls)
    spend: dict[str, object] = {
        "database task run": str(task_run_id),
        "calls": sum(int(row[2]) for row in calls),
        "calls by prompt and outcome": {f"{row[0]} {row[1]}": int(row[2]) for row in calls},
        "calls without a price": sum(int(row[4]) for row in calls),
        "spent (USD)": str(Decimal(spent) / 1_000_000),
        "budget (USD)": str(budget),
        "budget reached": spent >= int(budget * 1_000_000),
    }
    session.close()
    return pre_fills, dict(refusals.most_common()), spend


if __name__ == "__main__":
    raise SystemExit(main())
