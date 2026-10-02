"""Score the pipeline at every place GV's reviewer flagged the vendor, and print counts (#850).

The scoring is `eval/markup_yardstick.py`. This reads what it scores — the reviewer's notes from the
drawing, the run's readings from the database, and the vendor's values from people's keys — and
places every one of them in the same frame.

**Read-only.** Every statement runs in one transaction opened `READ ONLY`, so the database refuses a
write before anything here could make one. No model is called.

**GV's text is read from the file**, through `read_markup_layer`, the reader the pipeline's markup
route uses. The file has to be the one the revision holds: its SHA-256 must match exactly one of
that revision's document versions, or nothing is read. **Only `/FreeText` notes are boxes.** The
markup route also records a reviewer's measurement lines (#805), which say what the reviewer
measured, not where the vendor was wrong.

**A key binds to the drawing too.** Its provenance must name this drawing's hash for the vendor's
side, and only its `SHOP` observations are read: an `ARCH` observation is the architect's number —
what a box usually holds — and never the vendor's value. A key nobody read is refused
(`load_key`), and so is one whose pixel frame is unknown (`key_polygon_dpi`).

**Everything is placed in stored space**, the visible page normalised to one, where
`app/evidence/sides.py` places readings: each note where the markup reader puts it, each reading
through the transform its own run recorded, each key entry through its key's frame, and each finding
through the observations it used.

**Its output is counts**, apart from the id and page of any PASS finding listed for a person. No
text and no value from the drawing is printed.

Usage:

    .venv/bin/python scripts/markup_yardstick.py \\
        --database-url postgresql+psycopg://gv:gv@localhost:5433/<scratch> \\
        --revision <package-revision-id> --drawing <the-reviewed.pdf> \\
        --key data/goldset/<key-dir>:<dpi> [--key ...] [--json]

`:<dpi>` names the frame of a key written before keys recorded one (#835); leave it off a key that
records its own.

Source: issue #850. Verification: `tests/eval/test_markup_yardstick_script.py`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.evidence.sides import MARKUP_ROUTE, reading_transform
from app.models import (
    CanonicalObservation,
    DocumentVersion,
    Finding,
    FindingEvidence,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    VerdictInput,
)
from app.models.evidence import EvidenceCandidateRole, EvidenceSupportingCandidate
from app.models.runs import ExtractionRun
from eval.experiments.agent_scorecard import Kind, ScorecardError, load_key
from eval.experiments.model_bakeoff import ModelBakeoffError, key_polygon_dpi
from eval.gold_set.schema import GoldCase
from eval.markup_yardstick import (
    Box,
    GvNote,
    KeyEntry,
    PlacedFinding,
    Reading,
    Seal,
    Yardstick,
    measure,
    render,
)
from evidence.coordinates import ImagePoint, PageTransform, StoredPoint
from extraction.annotations import read_markup_layer
from extraction.reader import UnreadablePdf
from rules.semantic_types import OperandSource
from units.measurement import Measurement, Unit

#: The annotation subtype a reviewer's box is.
FREE_TEXT: Final = "FreeText"

#: The support roles that hold a reading in an observation. `conflicting` is recorded against an
#: observation, not for it.
_SUPPORT: Final = (EvidenceCandidateRole.PRIMARY.value, EvidenceCandidateRole.CORROBORATING.value)


class YardstickError(ValueError):
    """The inputs cannot be scored as given, and why."""


@dataclass(frozen=True, slots=True)
class KeySource:
    """A key directory, and the frame its caller names for it, if any."""

    directory: Path
    polygon_dpi: int | None


@dataclass(frozen=True, slots=True)
class Collected:
    """The yardstick, and what could not be placed in it."""

    yardstick: Yardstick
    readings: int
    """Readings recorded on the drawing."""
    readings_unplaced: int
    """Readings whose run recorded no transform, so where they lie is unknown."""
    pages_unreadable: int
    """Pages whose notes could not be read from the file."""
    findings: int
    passes: int
    """PASS findings recorded for the revision, under a box or not."""
    findings_unplaced: int
    """Findings none of whose observations lies on this drawing — a value typed on the form has
    no place on any drawing — so no box can be checked for them."""

    def counts(self) -> dict[str, int]:
        return {
            **self.yardstick.counts(),
            "readings": self.readings,
            "readings_unplaced": self.readings_unplaced,
            "pages_unreadable": self.pages_unreadable,
            "findings": self.findings,
            "passes": self.passes,
            "findings_unplaced": self.findings_unplaced,
        }


def parse_key(value: str) -> KeySource:
    """`DIR` or `DIR:DPI` from the command line."""
    directory, colon, dpi = value.rpartition(":")
    if colon and dpi.isdigit():
        return KeySource(Path(directory), int(dpi))
    return KeySource(Path(value), None)


def _box(points: Iterable[StoredPoint]) -> Box:
    listed = list(points)
    xs = [point.x for point in listed]
    ys = [point.y for point in listed]
    return (min(xs), min(ys), max(xs), max(ys))


def _page_transform(page: Page, dpi: int) -> PageTransform:
    """The page at `dpi`, from the boxes the database recorded for it."""
    if page.media_box is None or page.crop_box is None:
        raise YardstickError(f"page {page.index + 1} was recorded without its page boxes")
    return PageTransform(
        dpi=dpi,
        rotation=page.rotation,
        media_box=tuple(Decimal(value) for value in page.media_box),  # type: ignore[arg-type]
        crop_box=tuple(Decimal(value) for value in page.crop_box),  # type: ignore[arg-type]
    )


def _inches(numerator: int, denominator: int, unit: str) -> Fraction:
    return Measurement(Fraction(numerator, denominator), Unit(unit), None).to(Unit.INCH).exact


def _drawing(session: Session, revision_id: UUID, digest: str) -> DocumentVersion:
    """The revision's document version whose bytes are the drawing's."""
    if session.get(PackageRevision, revision_id) is None:
        raise YardstickError(f"there is no package revision {revision_id}")
    versions = (
        session.execute(
            select(DocumentVersion)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == DocumentVersion.id,
            )
            .where(
                PackageRevisionDocument.package_revision_id == revision_id,
                DocumentVersion.sha256 == digest,
            )
        )
        .scalars()
        .all()
    )
    if len(versions) != 1:
        raise YardstickError(
            "the drawing given is not a document of this revision: its SHA-256 matches "
            f"{len(versions)} of the revision's document versions, and must match exactly one"
        )
    return versions[0]


def _seals(session: Session, version_id: UUID) -> dict[UUID, list[Seal]]:
    """The observations holding each reading on the drawing."""
    rows = session.execute(
        select(EvidenceSupportingCandidate.candidate_id, CanonicalObservation)
        .join(
            CanonicalObservation,
            CanonicalObservation.id == EvidenceSupportingCandidate.canonical_observation_id,
        )
        .join(
            ObservationCandidate,
            ObservationCandidate.id == EvidenceSupportingCandidate.candidate_id,
        )
        .where(
            ObservationCandidate.document_version_id == version_id,
            EvidenceSupportingCandidate.role.in_(_SUPPORT),
        )
    ).all()
    seals: dict[UUID, list[Seal]] = {}
    for candidate_id, observation in rows:
        seals.setdefault(candidate_id, []).append(
            Seal(
                observation_id=str(observation.id),
                document_role=observation.document_role,
                status=observation.status,
                value=_inches(
                    observation.value_numerator, observation.value_denominator, observation.unit
                ),
            )
        )
    return seals


def _readings(
    session: Session, version_id: UUID, pages: dict[UUID, Page]
) -> tuple[list[Reading], int, set[int]]:
    """Every reading on the drawing that can be placed, how many could not, and the runs' dpis."""
    seals = _seals(session, version_id)
    rows = session.execute(
        select(ObservationCandidate, ExtractionRun)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .where(ObservationCandidate.document_version_id == version_id)
    ).all()
    placed: list[Reading] = []
    unplaced = 0
    dpis: set[int] = set()
    for candidate, run in rows:
        if run.dpi is not None:
            dpis.add(run.dpi)
        page = pages[candidate.page_id]
        transform = reading_transform(page, run)
        try:
            if transform is None:
                raise ValueError("no transform was recorded")
            box = _box(
                transform.to_stored(ImagePoint(int(x), int(y))) for x, y in candidate.polygon
            )
        except (ArithmeticError, TypeError, ValueError):
            unplaced += 1
            continue
        placed.append(
            Reading(
                reading_id=str(candidate.id),
                page_index=page.index,
                box=box,
                route=run.extractor,
                from_markup=run.extractor == MARKUP_ROUTE,
                value=(
                    None
                    if candidate.value_numerator is None
                    or candidate.value_denominator is None
                    or candidate.unit is None
                    else _inches(
                        candidate.value_numerator, candidate.value_denominator, candidate.unit
                    )
                ),
                lane=candidate.corroboration_lane,
                status=candidate.corroboration_status,
                seals=tuple(seals.get(candidate.id, ())),
            )
        )
    return placed, unplaced, dpis


def _findings(
    session: Session, revision_id: UUID, version_id: UUID, pages: dict[UUID, Page]
) -> tuple[list[PlacedFinding], int, int, int]:
    """Each finding at every place on the drawing one of its observations lies.

    Through both links a finding has to an observation: the evidence it cites, and the inputs its
    check was run on. Every finding recorded for the revision is read, superseded or not: a PASS a
    later run replaced still happened. Returns the placements, and how many findings there were,
    how many were PASS, and how many had no observation on this drawing.
    """
    findings = session.execute(
        select(Finding.id, Finding.outcome).where(Finding.package_revision_id == revision_id)
    ).all()
    cited = session.execute(
        select(FindingEvidence.finding_id, CanonicalObservation)
        .join(
            CanonicalObservation,
            CanonicalObservation.id == FindingEvidence.canonical_observation_id,
        )
        .join(Finding, Finding.id == FindingEvidence.finding_id)
        .where(Finding.package_revision_id == revision_id)
    ).all()
    inputs = session.execute(
        select(Finding.id, CanonicalObservation)
        .join(VerdictInput, VerdictInput.check_run_id == Finding.check_run_id)
        .join(
            CanonicalObservation,
            CanonicalObservation.id == VerdictInput.canonical_observation_id,
        )
        .where(Finding.package_revision_id == revision_id)
    ).all()
    outcomes = {finding_id: outcome for finding_id, outcome in findings}
    placed: list[PlacedFinding] = []
    seen: set[tuple[UUID, UUID]] = set()
    for finding_id, observation in (*cited, *inputs):
        if observation.document_version_id != version_id or (finding_id, observation.id) in seen:
            continue
        seen.add((finding_id, observation.id))
        try:
            box = _box(
                StoredPoint(Decimal(str(x)), Decimal(str(y))) for x, y in observation.polygon
            )
        except (ArithmeticError, TypeError, ValueError):
            continue
        placed.append(
            PlacedFinding(
                finding_id=str(finding_id),
                outcome=outcomes[finding_id],
                page_index=pages[observation.page_id].index,
                box=box,
            )
        )
    placed_ids = {finding.finding_id for finding in placed}
    return (
        placed,
        len(outcomes),
        sum(1 for outcome in outcomes.values() if outcome == "PASS"),
        sum(1 for finding_id in outcomes if str(finding_id) not in placed_ids),
    )


def _notes(
    drawing: bytes, version_id: UUID, pages: Sequence[Page], dpi: int
) -> tuple[list[GvNote], int]:
    """Every `/FreeText` note on the drawing, placed, and how many pages would not read."""
    notes: list[GvNote] = []
    unreadable = 0
    for page in pages:
        try:
            layers = read_markup_layer(drawing, page.index, document_version_id=version_id, dpi=dpi)
        except UnreadablePdf:
            unreadable += 1
            continue
        notes += [
            GvNote(page_index=page.index, text=note.text, box=_box(note.extent.points))
            for note in layers.markup
            if note.subtype == FREE_TEXT
        ]
    return notes, unreadable


def load_key_entries(source: KeySource, digest: str, pages: dict[int, Page]) -> list[KeyEntry]:
    """The vendor's values a key holds for this drawing, placed through the key's own frame."""
    directory = source.directory
    try:
        crops = load_key(directory)
        dpi = key_polygon_dpi(directory, polygon_dpi=source.polygon_dpi)
        case = GoldCase.model_validate(
            json.loads((directory / "answer_key.json").read_text(encoding="utf-8"))
        )
    except (ScorecardError, ModelBakeoffError, OSError, ValueError, ValidationError) as error:
        raise YardstickError(f"the key in {directory} cannot be used: {error}") from error
    if case.provenance.hash_for(OperandSource.SHOP) != f"sha256:{digest}":
        raise YardstickError(
            f"the key in {directory} was not read off this drawing: its provenance names another "
            "file for the vendor's side"
        )
    sides = {
        observation.item_id: observation.source for observation in case.ground_truth.observations
    }
    entries: list[KeyEntry] = []
    for crop in crops:
        if crop.kind is not Kind.SCORED or crop.expected is None:
            continue
        if sides.get(crop.crop_id) is not OperandSource.SHOP:
            continue
        page = pages.get(crop.page_index)
        if page is None:
            raise YardstickError(
                f"key entry {crop.crop_id} in {directory} is on page {crop.page_index + 1}, which "
                "this drawing does not have"
            )
        transform = _page_transform(page, dpi)
        left, top, right, bottom = crop.crop_px
        entries.append(
            KeyEntry(
                key=directory.name,
                item_id=crop.crop_id,
                page_index=crop.page_index,
                box=_box(
                    transform.to_stored(ImagePoint(x, y))
                    for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))
                ),
                value=crop.expected.to(Unit.INCH).exact,
                raw_text=crop.expected.raw_text,
            )
        )
    return entries


def collect(
    engine: Engine, *, revision_id: UUID, drawing: bytes, keys: Sequence[KeySource] = ()
) -> Collected:
    """Read the revision and the drawing, and score every box on it."""
    digest = hashlib.sha256(drawing).hexdigest()
    with Session(engine) as session:
        # First, so the transaction it opens is the one every read below runs in.
        session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            version = _drawing(session, revision_id, digest)
            pages = {
                page.id: page
                for page in session.execute(
                    select(Page).where(Page.document_version_id == version.id)
                ).scalars()
            }
            readings, unplaced, dpis = _readings(session, version.id, pages)
            placed, findings, passes, findings_unplaced = _findings(
                session, revision_id, version.id, pages
            )
            by_index = {page.index: page for page in pages.values()}
            entries = [entry for key in keys for entry in load_key_entries(key, digest, by_index)]
            if not dpis:
                raise YardstickError(
                    "nothing on this drawing was read at a recorded resolution, so there is "
                    "nothing to score under its boxes"
                )
            # The finest resolution any reading was made at, so a box is placed at least as
            # finely as the readings it is compared with.
            notes, unreadable = _notes(
                drawing,
                version.id,
                sorted(by_index.values(), key=lambda page: page.index),
                max(dpis),
            )
        finally:
            session.rollback()
    return Collected(
        yardstick=measure(notes, readings, entries, placed),
        readings=len(readings) + unplaced,
        readings_unplaced=unplaced,
        pages_unreadable=unreadable,
        findings=findings,
        passes=passes,
        findings_unplaced=findings_unplaced,
    )


def render_collected(collected: Collected) -> str:
    """The yardstick's report, after what was read and what could not be placed."""
    counts = collected.counts()

    def row(label: str, name: str) -> str:
        return f"  {label:<56}{counts[name]:>6}"

    return "\n".join(
        [
            "WHAT WAS READ",
            "=" * 64,
            row("readings recorded on the drawing", "readings"),
            row("    with no recorded transform, so not placed", "readings_unplaced"),
            row("pages whose notes would not read", "pages_unreadable"),
            row("findings recorded for the revision", "findings"),
            row("    PASS", "passes"),
            row("    with no observation on this drawing", "findings_unplaced"),
            "",
            render(collected.yardstick),
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--database-url",
        help="overrides GV_DATABASE_URL; the application setting is used when absent",
    )
    parser.add_argument("--revision", type=UUID, required=True, help="the package revision")
    parser.add_argument(
        "--drawing", type=Path, required=True, help="the reviewed PDF the revision holds"
    )
    parser.add_argument(
        "--key",
        type=parse_key,
        action="append",
        default=[],
        metavar="DIR[:DPI]",
        help="a human-read key of the vendor's values; repeat for more than one",
    )
    parser.add_argument("--json", action="store_true", help="emit the counts as JSON")
    args = parser.parse_args(argv)

    url = args.database_url
    if url is None:
        from app.config import Settings

        url = Settings().database_url  # type: ignore[call-arg]

    engine = create_engine(url)
    try:
        collected = collect(
            engine,
            revision_id=args.revision,
            drawing=args.drawing.read_bytes(),
            keys=args.key,
        )
    except YardstickError as error:
        parser.error(str(error))
    finally:
        engine.dispose()

    if args.json:
        report: dict[str, Any] = {
            "counts": collected.counts(),
            "pass_at_site": sorted(
                {f.finding_id for s in collected.yardstick.sites for f in s.passes}
            ),
        }
        print(json.dumps(report, indent=2))
    else:
        print(render_collected(collected))
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main(sys.argv[1:]))
