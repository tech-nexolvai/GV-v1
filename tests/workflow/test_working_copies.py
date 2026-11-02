"""The database pipeline reads the working copy, made once at intake (#1750 item 1), $0, no DB.

A sheet prints one label at a known point of its content stream and is then shown in three ways at
once: turned by `/Rotate 90`, cut to a crop box whose corner is not at the origin, and scaled by a
`/UserUnit`. Where the label shows (as a share of the page as shown) is worked out by hand, and the
pipeline's reader (`read_page_contents`, fed through `reading_copy` as every stage is) must put it
there, exactly where the chat's own reader puts it on the chat's working copy.
"""

from __future__ import annotations

import hashlib
import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pikepdf
import pytest
from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-untyped]

from app.review.assistant import dim_labels
from extraction import working_copy as front_door
from extraction.reader import MissingSpace, read_page_contents
from storage.local import LocalStore
from storage.store import ArtifactStore
from workflow import working_copies
from workflow.working_copies import reading_copy, record_key, take_in, working_key

#: The media box, and the crop box inside it whose corner is not at the origin.
MEDIA = (0.0, 0.0, 1000.0, 800.0)
CROP = (60.0, 40.0, 960.0, 640.0)
LABEL_AT = (520.0, 330.0)
LABEL = '48"'
SIZE = 12.0
UNIT = 2.0
#: How far a box's centre may be from where the label shows, as a share of the page.
NEAR = 0.01

KEY = "documents/00000000-0000-0000-0000-000000000001/ab/cd/" + "a" * 64 + ".pdf"


def _sheet(*, rotate: int = 90, crop: tuple[float, ...] | None = CROP, unit: float = UNIT) -> bytes:
    lx, ly = LABEL_AT
    content = (
        f"BT /F1 {SIZE} Tf {lx} {ly} Td ({LABEL}) Tj ET\n"
        f"0.5 w {lx - 80} {ly - 6} m {lx + 100} {ly - 6} l S\n"
    ).encode()
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=list(MEDIA),
        Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
        Contents=pdf.make_stream(content),
    )
    if rotate:
        page.Rotate = rotate
    if crop is not None:
        page.CropBox = list(crop)
    if unit != 1:
        page.UserUnit = unit
    pdf.pages.append(pikepdf.Page(page))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def _shown_share() -> tuple[float, float]:
    """Where the label's middle shows, as shares of the page as shown: the crop box is the page,
    turned a quarter clockwise (the crop box's bottom edge is the shown page's left edge)."""
    x = LABEL_AT[0] + stringWidth(LABEL, "Helvetica", SIZE) / 2
    y = LABEL_AT[1] + SIZE * 0.35
    x0, y0, x1, y1 = CROP
    return (y - y0) / (y1 - y0), (x - x0) / (x1 - x0)


def _store(tmp_path: Path, data: bytes) -> LocalStore:
    store = LocalStore(tmp_path / "store")
    store.put(KEY, io.BytesIO(data), content_type="application/pdf")
    return store


def _counted(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """How many times the front door makes a copy."""
    made: list[int] = []
    real = front_door.working_copy

    def counting(data: bytes) -> Any:
        made.append(1)
        return real(data)

    monkeypatch.setattr(front_door, "working_copy", counting)
    return made


def test_a_file_already_in_shape_is_read_byte_for_byte_and_no_copy_is_stored(
    tmp_path: Path,
) -> None:
    """Every GV set read by the pipeline so far: what the stages read is not changed at all."""
    data = _sheet(rotate=0, crop=None, unit=1)
    store = _store(tmp_path, data)
    intake = take_in(store, KEY, data)
    assert not intake.changed and intake.refused is None
    assert reading_copy(store, KEY, data) is data
    assert store.exists(record_key(KEY))
    assert not store.exists(working_key(KEY))


def test_the_copy_is_made_once_at_intake_and_read_back_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _sheet()
    store = _store(tmp_path, data)
    made = _counted(monkeypatch)
    intake = take_in(store, KEY, data)
    assert intake.changed
    assert any("rotation baked into" in line for line in intake.changes)
    assert any("visible area moved" in line for line in intake.changes)
    assert any("user unit applied" in line for line in intake.changes)
    first = reading_copy(store, KEY, data)
    second = reading_copy(store, KEY, data)
    assert made == [1], "the copy was made again by a reader"
    assert first == second != data
    assert hashlib.sha256(first).hexdigest() == intake.working_sha256
    with pikepdf.open(io.BytesIO(first)) as pdf:
        page = pdf.pages[0]
        assert int(page.obj.get("/Rotate", 0)) == 0
        assert [float(v) for v in page.mediabox][:2] == [0.0, 0.0]
        assert float(page.obj.get("/UserUnit", 1)) == 1.0
        # Shown exactly as before: the crop box turned, at the unit's scale.
        width, height = CROP[2] - CROP[0], CROP[3] - CROP[1]
        assert [float(v) for v in page.mediabox][2:] == [height * UNIT, width * UNIT]
        front_door.require_standard_page(page)


def test_a_copy_that_is_not_the_one_its_record_names_is_refused(tmp_path: Path) -> None:
    from storage.hashing import ArtifactCorrupt

    data = _sheet()
    store = _store(tmp_path, data)
    take_in(store, KEY, data)
    with store.get(record_key(KEY)) as stored:
        record = json.loads(stored.read())
    record["working_sha256"] = "0" * 64
    with pytest.raises(ArtifactCorrupt):
        reading_copy(cast(ArtifactStore, _Record(store, record)), KEY, data)


class _Record:
    """The store, with the record of `KEY` replaced by `record` (one naming another copy)."""

    def __init__(self, store: LocalStore, record: dict[str, Any]) -> None:
        self._store, self._record = store, record

    def exists(self, key: str) -> bool:
        return self._store.exists(key)

    def get(self, key: str) -> Any:
        if key == record_key(KEY):
            return io.BytesIO(json.dumps(self._record).encode())
        return self._store.get(key)


def test_a_file_the_front_door_cannot_open_is_read_as_it_is(tmp_path: Path) -> None:
    data = b"%PDF-1.7 this is not a PDF"
    store = _store(tmp_path, data)
    intake = take_in(store, KEY, data)
    assert intake.refused and not intake.changed
    assert reading_copy(store, KEY, data) is data


def test_retention_removes_every_working_copy_key_record_first(tmp_path: Path) -> None:
    data = _sheet()
    store = _store(tmp_path, data)
    take_in(store, KEY, data)
    keys = working_copies.working_copy_keys(KEY)
    assert keys[:2] == (record_key(KEY, 1), working_key(KEY, 1))
    for key in keys:
        store.delete(key)
    assert not store.exists(record_key(KEY)) and not store.exists(working_key(KEY))


def _label_share(data: bytes) -> tuple[float, float]:
    """Where the pipeline's reader puts the label's middle, as shares of the page as shown."""
    contents = read_page_contents(
        data,
        0,
        document_version_id=uuid4(),
        dpi=72,
        missing_space=MissingSpace(gap_heights=Decimal("0.15")),
    )
    # Written across the turned page, its characters may come in either order (`char_dir`).
    found = [item for item in contents.texts if item.text in (LABEL, LABEL[::-1])]
    assert len(found) == 1, [item.text for item in contents.texts]
    xs = [float(point.x) for point in found[0].extent.points]
    ys = [float(point.y) for point in found[0].extent.points]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def test_the_label_lands_where_it_shows_through_the_pipeline_and_the_chat_alike(
    tmp_path: Path,
) -> None:
    """The proof the issue asks for: `/Rotate 90`, a crop box away from the origin and a user
    unit, read by the pipeline's reader through `reading_copy` and by the chat's reader on the
    chat's working copy: both put the label where the original shows it."""
    from app.review.assistant.chat_service import UploadSource, sync_uploads
    from app.review.assistant.sandbox import ChatWorkspace

    data = _sheet()
    expected = _shown_share()

    # The pipeline: stored, taken in, read as every stage reads it.
    store = _store(tmp_path, data)
    take_in(store, KEY, data)
    pipeline = _label_share(reading_copy(store, KEY, data))
    assert pipeline == pytest.approx(expected, abs=NEAR)
    # Where the stages read it before (the original, turned back by the reader): the same spot.
    assert _label_share(data) == pytest.approx(pipeline, abs=NEAR)

    # The chat: the same file through its own front door, read by `dim_labels`.
    workspace = ChatWorkspace(tmp_path / "chat")
    source = UploadSource(
        name="sheet.pdf",
        sha256=hashlib.sha256(data).hexdigest(),
        kind="vendor",
        pages=1,
        read=lambda: data,
    )
    sync_uploads(workspace, [source])
    copy = workspace.uploads / "sheet.pdf"
    assert copy.read_bytes() == reading_copy(
        store, KEY, data
    ), "the pipeline and the chat read different copies"
    labels = dim_labels.page_labels(copy, 1, equal_parts=False).labels
    box = next(label["box"] for label in labels if label["text"] == LABEL)
    with pikepdf.open(copy) as pdf:
        width, height = (float(v) for v in list(pdf.pages[0].mediabox)[2:])
    chat = ((box[0] + box[2]) / 2 / width, (box[1] + box[3]) / 2 / height)
    assert chat == pytest.approx(expected, abs=NEAR)
    assert chat == pytest.approx(pipeline, abs=NEAR)
