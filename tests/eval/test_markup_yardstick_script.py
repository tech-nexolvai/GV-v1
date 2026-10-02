"""The yardstick's script, end to end on a synthetic package in a real database (#850).

The package is one invented sheet carrying four reviewer notes, read by the real extraction stage so
the markup route records the notes as it does on a client set. A vendor reader is then given two
readings, and a key is written beside them:

- under `30 1/4"`, a vendor reading of `30 1/4"`: GV's number taken as the vendor's;
- under `18 1/2"`, a reading of `19"` a person confirmed into evidence and a check passed, where the
  key says the vendor drew `20"`: sealed wrong, and a PASS under a box;
- under `12"`, nothing, where the key says `11"`: not found, never correct;
- `TAG-9`, which holds no dimension and is no site.

The key also holds the architect's `30 1/4"` under the first box. It is an `ARCH` observation — the
number a box usually holds — and must not make that box keyed.
"""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import scripts.markup_yardstick as script
from app.db.session import session_factory
from app.evidence.sides import MARKUP_ROUTE, reading_transform
from app.models import (
    CanonicalObservation,
    CheckRun,
    Finding,
    FindingEvidence,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    RuleDefinition,
    RuleSnapshot,
)
from app.models.evidence import EvidenceSupportingCandidate
from app.models.runs import ExtractionRun
from evidence.canonical import Authority
from evidence.coordinates import ImagePoint
from rules.semantic_types import DocumentRole, SemanticType
from scripts.markup_yardstick import KeySource, YardstickError, collect, main, parse_key
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.evidence.test_sides import _read
from tests.extraction.test_annotations import _free_text
from tests.extraction.test_annotations import _pdf as _annotated_pdf
from units.measurement import Unit

pytest_plugins = ("tests.app.postgres_fixture",)

SHEET = _annotated_pdf(
    annotations=[
        _free_text('30 1/4"', rect=b"[40 240 120 260]"),
        _free_text('18 1/2"', rect=b"[200 240 280 260]"),
        _free_text('12"', rect=b"[40 40 120 60]"),
        _free_text("TAG-9", rect=b"[200 40 280 60]"),
    ]
)


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


def _box_reading(session: Session, revision: PackageRevision, text: str) -> ObservationCandidate:
    """The markup route's row for one of this revision's notes."""
    return session.execute(
        select(ObservationCandidate)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == ObservationCandidate.document_version_id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == revision.id,
            ExtractionRun.extractor == MARKUP_ROUTE,
            ObservationCandidate.raw_text == text,
        )
    ).scalar_one()


def _vendor_reading(
    session: Session, under: ObservationCandidate, run: ExtractionRun, text: str, value: Fraction
) -> ObservationCandidate:
    """A vendor reader's row exactly where a box is."""
    reading = ObservationCandidate(
        document_version_id=under.document_version_id,
        page_id=under.page_id,
        extraction_run_id=run.id,
        raw_text=text,
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit=Unit.INCH.value,
        polygon=under.polygon,
        coordinate_space="image",
        ambiguity_flags=[],
    )
    session.add(reading)
    session.flush()
    return reading


def _sealed_and_passed(
    session: Session, revision: PackageRevision, reading: ObservationCandidate
) -> None:
    """Seal `reading` into evidence as a person's confirmation, and record a PASS that cites it."""
    page = session.get(Page, reading.page_id)
    run = session.get(ExtractionRun, reading.extraction_run_id)
    assert page is not None and run is not None
    transform = reading_transform(page, run)
    assert transform is not None
    observation = CanonicalObservation(
        document_version_id=reading.document_version_id,
        page_id=reading.page_id,
        document_role=DocumentRole.SHOP,
        polygon=[
            [str(point.x), str(point.y)]
            for point in (transform.to_stored(ImagePoint(x, y)) for x, y in reading.polygon)
        ],
        coordinate_space="stored",
        semantic_type=SemanticType.CT001,
        value_numerator=reading.value_numerator,
        value_denominator=reading.value_denominator,
        unit=Unit.INCH,
        status="HUMAN_CONFIRMED",
        authority=Authority.AUTHORITATIVE,
        evidence_crop_uri=None,
    )
    session.add(observation)
    session.flush()
    session.add(
        EvidenceSupportingCandidate(
            canonical_observation_id=observation.id, candidate_id=reading.id, role="primary"
        )
    )
    definition = RuleDefinition(rule_id=f"CT-{uuid4().hex[:6]}")
    session.add(definition)
    session.flush()
    body = f'{{"id":"{definition.rule_id}"}}'
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=body,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    check_run = CheckRun(
        package_revision_id=revision.id, rule_snapshot_id=snapshot.id, engine_version="test"
    )
    session.add(check_run)
    session.flush()
    finding = Finding(
        check_run_id=check_run.id,
        package_revision_id=revision.id,
        outcome="PASS",
        severity="FLAG",
        trace={},
        parameter_set_versions={},
    )
    session.add(finding)
    session.flush()
    session.add(
        FindingEvidence(
            finding_id=finding.id, canonical_observation_id=observation.id, role="evidence"
        )
    )
    session.flush()


def _write_key(
    directory: Path, digest: str, entries: list[tuple[str, ObservationCandidate, str, str, str]]
) -> None:
    """A key a person read: `(item, where, source, exact, as typed)` per entry, at 300 dpi."""
    observations: list[dict[str, Any]] = []
    rows: list[dict[str, str]] = []
    for item, under, source, exact, typed in entries:
        xs = [x for x, _ in under.polygon]
        ys = [y for _, y in under.polygon]
        polygon = [min(xs), min(ys), max(xs), max(ys)]
        observations.append(
            {
                "semantic_type": "cabinet_width",
                "source": source,
                "value": {"exact": exact, "unit": "in", "raw_text": typed},
                "page": 1,
                "polygon": polygon,
                "item_id": item,
            }
        )
        rows.append(
            {
                "crop_id": item,
                "page": "1",
                "left_px": str(polygon[0]),
                "top_px": str(polygon[1]),
                "right_px": str(polygon[2]),
                "bottom_px": str(polygon[3]),
                "value": typed,
                "unreadable": "",
                "not_a_single_value": "",
                "rotated": "",
                "note": "read by a person",
            }
        )
    documents = [
        {"source": side, "document_version_id": str(uuid4()), "content_hash": f"sha256:{digest}"}
        for side in ("SHOP", "ARCH")
    ]
    (directory / "answer_key.json").write_text(
        json.dumps(
            {
                "id": "synthetic-yardstick-key",
                "product_type": "cabinet",
                "arch": "sheet.pdf",
                "shop": "sheet.pdf",
                "ground_truth": {
                    "observations": observations,
                    "matches": [],
                    "expected_findings": [],
                },
                "provenance": {
                    "annotator": "a person",
                    "annotated_on": "2026-10-03",
                    "documents": documents,
                },
            }
        ),
        encoding="utf-8",
    )
    with (directory / "crops.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (directory / "model_bakeoff_metadata.json").write_text(
        json.dumps({"frame": {"polygon_dpi": 300, "margin_pt": "0"}}), encoding="utf-8"
    )


@pytest.fixture
def package(session: Session, store: LocalStore, tmp_path: Path) -> tuple[PackageRevision, Path]:
    """The sheet read, the vendor's three readings recorded, the key written; all committed."""
    revision = _read(session, store, kind="shop", data=SHEET)
    taken_box = _box_reading(session, revision, '30 1/4"')
    sealed_box = _box_reading(session, revision, '18 1/2"')
    empty_box = _box_reading(session, revision, '12"')
    markup_run = session.get(ExtractionRun, taken_box.extraction_run_id)
    assert markup_run is not None
    vendor_run = ExtractionRun(
        task_run_id=markup_run.task_run_id,
        extractor="synthetic-vision-reader",
        extractor_version="1",
        config_hash=f"dpi={markup_run.dpi}",
        dpi=markup_run.dpi,
    )
    session.add(vendor_run)
    session.flush()
    _vendor_reading(session, taken_box, vendor_run, '30 1/4"', Fraction(121, 4))
    sealed = _vendor_reading(session, sealed_box, vendor_run, '19"', Fraction(19))
    _sealed_and_passed(session, revision, sealed)
    session.commit()

    key = tmp_path / "key"
    key.mkdir()
    _write_key(
        key,
        hashlib.sha256(SHEET).hexdigest(),
        [
            ("a1", taken_box, "ARCH", "121/4", '30 1/4"'),
            ("k1", sealed_box, "SHOP", "20", '20"'),
            ("k2", empty_box, "SHOP", "11", '11"'),
        ],
    )
    return revision, key


def test_the_synthetic_package_scores_as_the_issue_says(
    postgres_engine: Engine, package: tuple[PackageRevision, Path]
) -> None:
    revision, key = package

    collected = collect(
        postgres_engine, revision_id=revision.id, drawing=SHEET, keys=[KeySource(key, None)]
    )
    counts = collected.counts()

    assert counts["notes"] == 4
    assert counts["sites"] == 3
    # GV's number recorded as a vendor reading is counted.
    assert counts["gv_taken_readings"] == 1
    # A sealed wrong reading is counted, and the PASS it fed is listed.
    assert counts["agreed_or_sealed_wrong_readings"] == 1
    assert counts["pass_at_site"] == 1
    # A site with no reading is not found, not correct.
    empty = next(s for s in collected.yardstick.sites if s.site.gv_value == 12)
    assert not empty.found
    assert empty.read.value == "not_found"
    assert counts["read_right"] == 0
    # The architect's number under the first box is not read as a key entry at all: read as one, it
    # would be refused for repeating the box, and the box would not count as unkeyed.
    taken = next(s for s in collected.yardstick.sites if s.gv_taken)
    assert taken.vendor_value is None
    assert counts["key_repeats_box"] == 0
    assert counts["no_key"] == 1
    assert counts["keyed"] == 2


def test_a_drawing_the_revision_does_not_hold_is_refused(
    postgres_engine: Engine, package: tuple[PackageRevision, Path]
) -> None:
    revision, _key = package
    other = _annotated_pdf(annotations=[_free_text('30 1/4"', rect=b"[40 40 120 60]")])

    with pytest.raises(YardstickError, match="not a document of this revision"):
        collect(postgres_engine, revision_id=revision.id, drawing=other)


def test_a_key_read_off_another_drawing_is_refused(
    postgres_engine: Engine, package: tuple[PackageRevision, Path]
) -> None:
    revision, key = package
    answer = json.loads((key / "answer_key.json").read_text(encoding="utf-8"))
    for document in answer["provenance"]["documents"]:
        document["content_hash"] = "sha256:" + "0" * 64
    (key / "answer_key.json").write_text(json.dumps(answer), encoding="utf-8")

    with pytest.raises(YardstickError, match="not read off this drawing"):
        collect(
            postgres_engine, revision_id=revision.id, drawing=SHEET, keys=[KeySource(key, None)]
        )


def test_every_read_runs_in_a_transaction_that_refuses_writes(
    postgres_engine: Engine,
    package: tuple[PackageRevision, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first read is swapped for a write. Outcome: the database refuses it."""
    revision, _key = package

    def write_instead(session: Session, revision_id: object, digest: str) -> None:
        session.execute(text("CREATE TABLE yardstick_must_never_exist (id integer)"))

    monkeypatch.setattr(script, "_drawing", write_instead)

    with pytest.raises(DBAPIError, match="read-only transaction"):
        collect(postgres_engine, revision_id=revision.id, drawing=SHEET)


def test_the_command_prints_counts_as_json(
    postgres_engine: Engine,
    package: tuple[PackageRevision, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    revision, key = package
    drawing = tmp_path / "sheet.pdf"
    drawing.write_bytes(SHEET)

    main(
        [
            "--database-url",
            postgres_engine.url.render_as_string(hide_password=False),
            "--revision",
            str(revision.id),
            "--drawing",
            str(drawing),
            "--key",
            str(key),
            "--json",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert report["counts"]["gv_taken_readings"] == 1
    assert len(report["pass_at_site"]) == 1


@pytest.mark.parametrize(
    ("argument", "directory", "dpi"),
    [("keys/pilot:300", "keys/pilot", 300), ("keys/pilot", "keys/pilot", None)],
)
def test_a_key_argument_names_its_frame_only_when_given(
    argument: str, directory: str, dpi: int | None
) -> None:
    assert parse_key(argument) == KeySource(Path(directory), dpi)
