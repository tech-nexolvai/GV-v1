"""Where each countertop piece was read, for "Show on drawing" (#1049).

Every value here is synthetic. The slot box is the reader's own record of the slot (`slot-box:` in
the page picture's pixels); it is placed with the transform the reading was made under, exactly as
the row outline is, and is `null` whenever it cannot be placed exactly.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.evidence.sides import reading_transform
from app.main import API_PREFIX, create_app
from app.models import (
    CanonicalObservation,
    EvidenceArtifact,
    EvidenceSupportingCandidate,
    Finding,
    ObservationCandidate,
    ReviewAction,
    VerdictInput,
)
from app.models.document import Page
from app.models.evidence import EvidenceArtifactKind
from app.models.runs import ExtractionRun
from evidence.coordinates import ImagePoint
from storage.local import LocalStore
from tests.api import test_slot_rows as slot_row_tests
from tests.api.test_slot_rows import _package_rows, _reader_support, _run_current_checks
from tests.api.test_visual_ui import session  # noqa: F401

pytest_plugins = ("tests.app.postgres_fixture",)


def _box(page_index: int, row_offset: int, slot: str) -> tuple[int, int, int, int]:
    """A distinct synthetic box per slot, inside a 612 x 792 pt page read at 150 dpi."""
    left = 900 if slot == "overall" else 100 + 200 * int(slot)
    top = 200 + 120 * row_offset + 10 * page_index
    return left, top, left + 150, top + 60


def _boxes(page_index: int, row_offset: int, slot: str) -> list[str]:
    x0, y0, x1, y1 = _box(page_index, row_offset, slot)
    return [f"slot-box:{x0},{y0},{x1},{y1}"]


def _client(db: Session, project_id: UUID, store: LocalStore | None = None) -> TestClient:
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    app.dependency_overrides[get_session] = lambda: db
    if store is not None:
        app.dependency_overrides[get_artifact_store] = lambda: store
    return TestClient(app)


def _results(db: Session, project_id: UUID, package_id: UUID) -> list[dict[str, Any]]:
    response = _client(db, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results"
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert isinstance(items, list)
    return items


def _expected(db: Session, row_id: str, box: tuple[int, int, int, int]) -> list[list[str]]:
    anchor = db.get(ObservationCandidate, UUID(row_id))
    assert anchor is not None
    page = db.get(Page, anchor.page_id)
    run = db.get(ExtractionRun, anchor.extraction_run_id)
    assert page is not None and run is not None
    transform = reading_transform(page, run)
    assert transform is not None
    a = transform.to_stored(ImagePoint(box[0], box[1]))
    b = transform.to_stored(ImagePoint(box[2], box[3]))
    left, right = min(a.x, b.x), max(a.x, b.x)
    top, bottom = min(a.y, b.y), max(a.y, b.y)
    return [
        [str(x), str(y)] for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))
    ]


def _slot_candidate(db: Session, row_id: str, slot: str) -> ObservationCandidate:
    anchor = db.get(ObservationCandidate, UUID(row_id))
    assert anchor is not None
    rank = next(flag for flag in anchor.ambiguity_flags if flag.startswith("row-rank:"))
    return next(
        candidate
        for candidate in db.scalars(
            select(ObservationCandidate).where(
                ObservationCandidate.extraction_run_id == anchor.extraction_run_id,
                ObservationCandidate.page_id == anchor.page_id,
            )
        )
        if "slot-reader" in candidate.ambiguity_flags
        and rank in candidate.ambiguity_flags
        and f"slot:{slot}" in candidate.ambiguity_flags
    )


def _assert_located(db: Session, item: dict[str, Any], page_index: int, offset: int) -> None:
    row_id = str(item["row_id"])
    pieces = item["pieces"]
    assert isinstance(pieces, list) and len(pieces) == 2
    for piece in pieces:
        location = piece["location"]
        assert location is not None, piece
        assert location["coordinate_space"] == "stored"
        assert location["page_number"] == item["page_number"]
        assert location["polygon"] == _expected(
            db, row_id, _box(page_index, offset, str(piece["index"]))
        )
    overall = item["printed_overall_location"]
    assert isinstance(overall, dict)
    assert overall["polygon"] == _expected(db, row_id, _box(page_index, offset, "overall"))
    row_location = item["row_location"]
    assert isinstance(row_location, dict)
    assert overall["page_id"] == row_location["page_id"]


def test_every_piece_of_a_held_and_an_unchecked_row_gets_its_slot_polygon(
    session: Session,  # noqa: F811
) -> None:
    project_id, package_id, anchors = _package_rows(
        session, piece_count=2, held_page=0, slot_flags=_boxes
    )
    items = {item["row_id"]: item for item in _results(session, project_id, package_id)}
    held, unchecked = items[str(anchors[0])], items[str(anchors[1])]
    assert held["hold"] is not None and held["finding_id"] is None
    assert unchecked["hold"] is None and unchecked["finding_id"] is None
    _assert_located(session, held, 0, 0)
    _assert_located(session, unchecked, 1, 0)


def test_checking_the_rows_changes_no_location(
    session: Session, tmp_path: Path  # noqa: F811
) -> None:
    project_id, package_id, anchors = _package_rows(
        session, piece_count=2, held_page=0, slot_flags=_boxes
    )
    _run_current_checks(session, package_id, tmp_path)
    items = {item["row_id"]: item for item in _results(session, project_id, package_id)}
    assert items[str(anchors[1])]["finding_id"] is not None
    _assert_located(session, items[str(anchors[0])], 0, 0)
    _assert_located(session, items[str(anchors[1])], 1, 0)


def test_a_missing_or_malformed_slot_box_is_null_never_a_guess(
    session: Session,  # noqa: F811
) -> None:
    broken: dict[tuple[int, str], list[str]] = {
        (0, "0"): [],  # no box at all
        (0, "1"): ["slot-box:100,200,250"],  # three numbers
        (0, "overall"): ["slot-box:900,200,900,260"],  # no width
        (1, "0"): ["slot-box:100,210,250,270", "slot-box:300,210,450,270"],  # two boxes
        (1, "1"): ["slot-box:100,210,99999,270"],  # off the page
        (1, "overall"): ["slot-box:a,b,c,d"],  # not numbers
    }

    def flags(page_index: int, _offset: int, slot: str) -> list[str]:
        return broken[(page_index, slot)]

    project_id, package_id, _ = _package_rows(session, piece_count=2, slot_flags=flags)
    for item in _results(session, project_id, package_id):
        pieces = item["pieces"]
        assert isinstance(pieces, list)
        assert [piece["location"] for piece in pieces] == [None, None], item["row_id"]
        assert item["printed_overall_location"] is None


def test_one_good_slot_box_is_placed_even_when_its_neighbour_is_broken(
    session: Session,  # noqa: F811
) -> None:
    def flags(page_index: int, offset: int, slot: str) -> list[str]:
        return ["slot-box:1,2,3"] if slot == "0" else _boxes(page_index, offset, slot)

    project_id, package_id, anchors = _package_rows(session, piece_count=2, slot_flags=flags)
    item = next(
        row for row in _results(session, project_id, package_id) if row["row_id"] == str(anchors[0])
    )
    pieces = item["pieces"]
    assert isinstance(pieces, list)
    assert pieces[0]["location"] is None
    assert pieces[1]["location"]["polygon"] == _expected(session, str(anchors[0]), _box(0, 0, "1"))


def _canonical(
    db: Session, candidate: ObservationCandidate, *, status: str = "CORROBORATED"
) -> CanonicalObservation:
    canonical = CanonicalObservation(
        document_version_id=candidate.document_version_id,
        page_id=candidate.page_id,
        document_role="SHOP",
        polygon=[["0.1", "0.1"], ["0.2", "0.1"], ["0.2", "0.2"], ["0.1", "0.2"]],
        coordinate_space="stored",
        semantic_type="countertop_piece_width",
        value_numerator=candidate.value_numerator or 1,
        value_denominator=candidate.value_denominator or 1,
        unit="in",
        status=status,
        authority="AUTHORITATIVE",
    )
    db.add(canonical)
    db.flush()
    return canonical


def _support(db: Session, candidate: ObservationCandidate) -> ObservationCandidate:
    """A per-reader child that supports `candidate`, the way the automatic lane records it."""
    child = ObservationCandidate(
        document_version_id=candidate.document_version_id,
        page_id=candidate.page_id,
        extraction_run_id=candidate.extraction_run_id,
        raw_text=candidate.raw_text,
        value_numerator=candidate.value_numerator,
        value_denominator=candidate.value_denominator,
        unit=candidate.unit,
        polygon=candidate.polygon,
        coordinate_space="image",
        ambiguity_flags=["slot-reader-support", f"supports:{candidate.id}", "reader-id:a"],
    )
    db.add(child)
    db.flush()
    return child


def _crop(
    db: Session,
    store: LocalStore,
    page_candidate: ObservationCandidate,
    *,
    candidate_id: UUID | None = None,
    canonical_id: UUID | None = None,
) -> None:
    content = b"\x89PNG synthetic crop " + uuid4().bytes
    digest = hashlib.sha256(content).hexdigest()
    key = f"evidence-crops/synthetic/{digest}.png"
    store.put(key, BytesIO(content), content_type="image/png")
    db.add(
        EvidenceArtifact(
            candidate_id=candidate_id,
            canonical_observation_id=canonical_id,
            document_version_id=page_candidate.document_version_id,
            page_id=page_candidate.page_id,
            kind=EvidenceArtifactKind.CROP.value,
            storage_key=key,
            sha256=digest,
            media_type="image/png",
            coordinate_space="image",
        )
    )
    db.flush()


def test_a_crop_id_is_given_only_for_a_sealed_reading_with_a_stored_crop(
    session: Session, tmp_path: Path  # noqa: F811
) -> None:
    store = LocalStore(root=tmp_path / "store", ticket_secret=b"synthetic-test")
    project_id, package_id, anchors = _package_rows(
        session, piece_count=2, unsealed_page=1, slot_flags=_boxes
    )
    first, second = str(anchors[0]), str(anchors[1])

    # Row 1, piece 1: sealed; canonical supported by a reader child that owns a crop -> shown.
    piece = _slot_candidate(session, first, "0")
    child = _support(session, piece)
    shown = _canonical(session, piece)
    session.add(
        EvidenceSupportingCandidate(
            canonical_observation_id=shown.id, candidate_id=child.id, role="primary"
        )
    )
    _crop(session, store, piece, candidate_id=child.id)
    # Row 1, piece 2: sealed and canonical, but no crop stored -> null.
    plain = _slot_candidate(session, first, "1")
    no_crop = _canonical(session, plain)
    session.add(
        EvidenceSupportingCandidate(
            canonical_observation_id=no_crop.id, candidate_id=plain.id, role="primary"
        )
    )
    # Row 1, overall: sealed; canonical-owned crop -> shown.
    overall = _slot_candidate(session, first, "overall")
    owned = _canonical(session, overall)
    session.add(
        EvidenceSupportingCandidate(
            canonical_observation_id=owned.id, candidate_id=overall.id, role="primary"
        )
    )
    _crop(session, store, overall, canonical_id=owned.id)
    # Row 2, piece 1: NOT sealed, even with a canonical and a crop -> null.
    unsealed = _slot_candidate(session, second, "0")
    stray = _canonical(session, unsealed)
    session.add(
        EvidenceSupportingCandidate(
            canonical_observation_id=stray.id, candidate_id=unsealed.id, role="primary"
        )
    )
    _crop(session, store, unsealed, canonical_id=stray.id)
    # Row 2, piece 2: sealed but two canonicals, each with a crop -> ambiguous -> null.
    doubled = _slot_candidate(session, second, "1")
    for _ in range(2):
        twin = _canonical(session, doubled)
        session.add(
            EvidenceSupportingCandidate(
                canonical_observation_id=twin.id, candidate_id=doubled.id, role="primary"
            )
        )
        _crop(session, store, doubled, canonical_id=twin.id)
    session.flush()

    items = {item["row_id"]: item for item in _results(session, project_id, package_id)}
    row1, row2 = items[first], items[second]
    assert [p["canonical_observation_id"] for p in row1["pieces"]] == [str(shown.id), None]
    assert row1["printed_overall_canonical_observation_id"] == str(owned.id)
    assert [p["canonical_observation_id"] for p in row2["pieces"]] == [None, None]
    assert row2["pieces"][0]["source"] != "sealed"

    # Each id given is one the crop endpoint serves for this package.
    client = _client(session, project_id, store)
    for canonical_id in (shown.id, owned.id):
        crop = client.get(
            f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/evidence/{canonical_id}/crop"
        )
        assert crop.status_code == 200, crop.text


def test_a_checked_rows_crop_ids_are_the_recorded_operands_own_observations(
    session: Session, tmp_path: Path  # noqa: F811
) -> None:
    store = LocalStore(root=tmp_path / "store", ticket_secret=b"synthetic-test")
    project_id, package_id, anchors = _package_rows(
        session,
        piece_count=2,
        widths_add_up=True,
        wall_source="vendor-drawing-clues",
        slot_flags=_boxes,
    )
    for candidate in session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"])
        )
    ).all():
        _reader_support(session, candidate)
    findings = _run_current_checks(session, package_id, tmp_path)
    finding = next(item for item in findings if item.scope_row_candidate_id == anchors[0])
    assert finding.outcome == "PASS", finding.reason
    recorded = {
        item.operand_name: item.canonical_observation_id
        for item in session.scalars(
            select(VerdictInput).where(VerdictInput.check_run_id == finding.check_run_id)
        )
    }
    piece = _slot_candidate(session, str(anchors[0]), "0")
    overall = _slot_candidate(session, str(anchors[0]), "overall")
    # Piece 1: a crop owned by its observation. Overall: by one of its supporting readers.
    _crop(session, store, piece, canonical_id=recorded["piece_widths[0]"])
    reader = session.scalars(
        select(EvidenceSupportingCandidate.candidate_id).where(
            EvidenceSupportingCandidate.canonical_observation_id == recorded["countertop_width"]
        )
    ).first()
    _crop(session, store, overall, candidate_id=reader)

    item = next(
        row for row in _results(session, project_id, package_id) if row["row_id"] == str(anchors[0])
    )
    assert [p["source"] for p in item["pieces"]] == ["sealed", "sealed"]
    assert [p["canonical_observation_id"] for p in item["pieces"]] == [
        str(recorded["piece_widths[0]"]),
        None,  # no crop stored for piece 2
    ]
    assert item["printed_overall_canonical_observation_id"] == str(recorded["countertop_width"])
    _assert_located(session, item, 0, 0)


def test_another_projects_reviewer_sees_no_locations(session: Session) -> None:  # noqa: F811
    project_id, package_id, _ = _package_rows(session, piece_count=2, slot_flags=_boxes)
    response = _client(session, uuid4()).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/countertop-results"
    )
    assert response.status_code == 404
    assert "polygon" not in response.text
    elsewhere = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{uuid4()}/packages/{package_id}/countertop-results"
    )
    assert elsewhere.status_code == 404
    assert "polygon" not in elsewhere.text


def _statements(db: Session, call: Callable[[], object]) -> list[str]:
    seen: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        seen.append(statement)

    event.listen(db.bind, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(db.bind, "before_cursor_execute", record)
    return seen


def test_locations_and_crops_are_read_only_and_do_not_grow_with_rows(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    small_project, small_package, _ = _package_rows(
        session, piece_count=2, rows_per_page=1, slot_flags=_boxes
    )
    # The rulebook is published once; the second package reuses it.
    monkeypatch.setattr(slot_row_tests, "_publish_rulebook", lambda _session: None)
    project_id, package_id, _ = _package_rows(
        session, piece_count=2, rows_per_page=10, slot_flags=_boxes
    )

    def tables() -> dict[str, int | None]:
        return {
            model.__name__: session.scalar(select(func.count()).select_from(model))
            for model in (Finding, ReviewAction, CanonicalObservation, ObservationCandidate)
        }

    before = tables()
    small = _statements(session, lambda: _results(session, small_project, small_package))
    large = _statements(session, lambda: _results(session, project_id, package_id))
    assert len(_results(session, project_id, package_id)) == 20
    assert len(large) == len(small) <= 12
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in small + large)
    assert tables() == before

    # Checked rows too: the same plan for 2 rows and for 20.
    _run_current_checks(session, small_package, tmp_path / "small")
    _run_current_checks(session, package_id, tmp_path / "large")
    before = tables()
    small = _statements(session, lambda: _results(session, small_project, small_package))
    large = _statements(session, lambda: _results(session, project_id, package_id))
    assert len(large) == len(small)
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in small + large)
    assert tables() == before
