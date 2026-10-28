"""The separate architect file through the production chain, on made-up PDFs (#1167 review).

Two synthetic PDFs: the vendor's sheet (`tests/extraction/slot_reader/sheets.py`: a chain of three
pieces, `24"`, `48"`, `24"`, overall `96"`, drawn 1 : 2 : 1) and the architect's own file, two twin
views of three cabinets drawn 2' - 0", 4' - 0" and 2' - 0" at 1/4" = 1'-0", widths printed as real
text on ticks that sit on the cabinet outline. Everything runs as in production:

* the stage reads both files (`extract_pages`: the architect reader and #1166's view index);
* `DatabaseStages._read_slots` reads the vendor's row from the PDF (the real slot reader), builds the
  REAL `ArchitectMatcher`, matches, pairs, and stores the readings, the matches and the pairings;
* the reviewer picks a twin through #1166's POST handler; the slot stage reads again; the pick is
  carried (`carried_over`); the row is paired against that view by code and both AIs on the
  two-panel picture; the check stage decides an exact PASS.

The only fakes are the two readers: one client answering every question by `(model, picture)`. On
the two-panel question it reads the blue badges off the picture itself and answers by where they
are drawn (left to right), so a badge drawn on the wrong span would pair the wrong dimensions.
No model is called. Every value is invented; no client value appears here.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import PackageRevision, Page
from app.models.runs import TaskRun, WorkflowRun
from evidence.crop import decode_rgb_png
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.slot_reader import sheets
from tests.workflow.test_slot_reader import (
    BOTH_WALLS,
    FakeReaders,
    claude_crops_to_texts,
)
from workflow.architect_pairing import _ARCHITECT_COLOUR, _GLYPHS, _VENDOR_COLOUR
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

TEXTS: dict[int | None, str] = {0: '24"', 1: '48"', 2: '24"', None: '96"'}
VENDOR = sheets.sheet(sheets.text_labels(('24"', '48"', '24"'), '96"'))


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


# --- the architect's own file: two twin views of three cabinets ------------------------------------


# --- the two readers ---------------------------------------------------------------------------------


def _badges(png: bytes, colour: bytes) -> list[tuple[str, float]]:
    """Every numbered tag of one colour drawn on a picture, read off its pixels: `(label, centre
    x)`. The tags are the pairing picture's 5 x 7 dot font at 3 px a dot."""
    import cv2
    import numpy as np

    width, height, rgb = decode_rgb_png(png)
    image = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
    mask = np.all(image == np.frombuffer(colour, dtype=np.uint8), axis=2).astype(np.uint8)
    count, _labels, stats, _centres = cv2.connectedComponentsWithStats(mask, connectivity=8)
    found: list[tuple[str, float]] = []
    dot = 3
    for component in range(1, count):
        left, top, box_width, box_height, _area = (int(v) for v in stats[component])
        characters = (box_width - dot) // (6 * dot)
        if box_height != 9 * dot or box_width != characters * 6 * dot + dot or characters < 2:
            continue
        label = ""
        for position in range(characters):
            cells = tuple(
                "".join(
                    (
                        "1"
                        if bool(
                            np.all(
                                image[
                                    top + dot + glyph_y * dot + 1,
                                    left + dot + position * 6 * dot + glyph_x * dot + 1,
                                ]
                                == 255
                            )
                        )
                        else "0"
                    )
                    for glyph_x in range(5)
                )
                for glyph_y in range(7)
            )
            label += next((key for key, glyph in _GLYPHS.items() if glyph == cells), "?")
        found.append((label, left + box_width / 2))
    return found


class Readers(FakeReaders):
    """Both Claude readers. Labels: what each crop prints. The match question: unsure. The pairing
    question: read off the picture: the architect's tags left to right are the vendor's pieces
    left to right (what the drawings show: three cabinets in the same order)."""

    def __init__(self) -> None:
        super().__init__(lambda _model, png: self.lookup.get(png, ""), walls=lambda _m: BOTH_WALLS)
        self.lookup: dict[bytes, str] = {}
        self.pairing_pictures: list[bytes] = []

    def _answer(self, **kwargs: Any) -> Mapping[str, Any]:
        content = kwargs["messages"][0]["content"]
        text = " ".join(part.get("text", "") for part in content).strip()
        pictures = [part["image"]["source"]["bytes"] for part in content if "image" in part]
        if text.startswith("This picture has two panels"):
            self.pairing_pictures.append(pictures[0])
            architect = sorted(_badges(pictures[0], _ARCHITECT_COLOUR), key=lambda item: item[1])
            vendor = sorted(_badges(pictures[0], _VENDOR_COLOUR), key=lambda item: item[1])
            count = int(re.search(r"A1 to A(\d+)", text).group(1))  # type: ignore[union-attr]
            pieces = [label for label, _x in architect][: len(vendor)]
            return _reply(
                {
                    "architect": [
                        {"a": k, "measures": "single_cabinet"} for k in range(1, count + 1)
                    ],
                    "overall": "none",
                    "pieces": pieces + ["none"] * (len(vendor) - len(pieces)),
                    "why": "the same three cabinets, left to right",
                }
            )
        if "of the architect's elevation drawings, each numbered in a blue tag" in text:
            shown = int(re.search(r"On the right are (\d+) of", text).group(1))  # type: ignore[union-attr]
            return _reply(
                {
                    "candidates": [{"n": k, "same": "unsure"} for k in range(1, shown + 1)],
                    "pick": "unsure",
                    "why": "twin drawings",
                }
            )
        return super()._answer(**kwargs)


def _reply(payload: Mapping[str, object]) -> Mapping[str, Any]:
    return {
        "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }


# --- the chain ----------------------------------------------------------------------------------------


def _read_slots(
    session: Session, slot: DatabaseStages, revision: PackageRevision, readers: Readers
) -> None:
    """The slot stage on the vendor's page, as `extract_pages` calls it."""
    page = session.scalars(
        select(Page).where(Page.document_version_id == _vendor_version(session, revision))
    ).one()
    rendered = slot._vendor_render(VENDOR, page, page.document_version_id)
    assert rendered is not None
    ink = slot._page_ink(VENDOR, page, rendered.dpi)
    slot_page = slot._slot_page(VENDOR, page, page.document_version_id, rendered, ink)
    assert slot_page is not None, "the vendor's chain is a row candidate"
    readers.lookup = claude_crops_to_texts(slot_page, TEXTS)
    # Each read is its own extraction task, as a re-run is.
    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    task_run = task.id
    slot._read_slots(
        session,
        package_revision_id=revision.id,
        pages=[slot_page],
        task_run_id=task_run,
        data=VENDOR,
    )
    session.commit()


def _vendor_version(session: Session, revision: PackageRevision) -> Any:
    from app.models.document import Document, PackageRevisionDocument

    return session.scalars(
        select(PackageRevisionDocument.document_version_id)
        .join(Document, Document.id == PackageRevisionDocument.document_id)
        .where(PackageRevisionDocument.package_revision_id == revision.id, Document.kind == "shop")
    ).one()
