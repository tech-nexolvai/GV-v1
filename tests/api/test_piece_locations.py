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
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.evidence.sides import reading_transform
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
from tests.app.countertop_results import countertop_results

pytest_plugins = ("tests.app.postgres_fixture",)


def _box(page_index: int, row_offset: int, slot: str) -> tuple[int, int, int, int]:
    """A distinct synthetic box per slot, inside a 612 x 792 pt page read at 150 dpi."""
    left = 900 if slot == "overall" else 100 + 200 * int(slot)
    top = 200 + 120 * row_offset + 10 * page_index
    return left, top, left + 150, top + 60


def _boxes(page_index: int, row_offset: int, slot: str) -> list[str]:
    x0, y0, x1, y1 = _box(page_index, row_offset, slot)
    return [f"slot-box:{x0},{y0},{x1},{y1}"]


def _results(db: Session, project_id: UUID, package_id: UUID) -> list[dict[str, Any]]:
    response = countertop_results(db, project_id, package_id)
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


def test_a_checked_rows_crop_ids_are_the_recorded_operands_own_observations(
    session: Session, tmp_path: Path  # noqa: F811
) -> None:
    store = LocalStore(root=tmp_path / "store", ticket_secret=b"synthetic-test")
    project_id, package_id, anchors = _package_rows(
        session,
        piece_count=2,
        widths_add_up=True,
        wall_source="readers",
        walls_confirmed=True,
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
    # 14 since #1202: the field cut a run would use now is asked once (a second statement only
    # when no stored setting holds it and the rulebook's default is read); same for any row count.
    # 15 since #1241: a reviewer's wall answer an earlier reading could not carry is asked for
    # once; still the same for any number of rows.
    # 16 since #1245: the live countertops are asked for once, to group each row's
    # results under its countertop; still the same for any number of rows.
    assert len(large) == len(small) <= 16
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
