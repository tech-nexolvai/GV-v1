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
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.architect_matches import pick_architect_view
from app.auth.roles import Principal, Role
from app.db.session import session_factory
from app.models import ObservationCandidate, Package, PackageRevision, Page
from app.models.evidence import ArchitectPairingRecord, ArchitectViewMatchRecord
from app.models.runs import TaskRun, WorkflowRun
from app.schemas.architect_matches import ArchitectViewPickIn
from evidence.crop import decode_rgb_png
from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
from extraction.slot_reader.bedrock import ARCH_PAIR_2PANEL_PROMPT_ID
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.architect import architect_sheet as sheet_parts
from tests.extraction.architect.combined_sheet import _dashed, _row, _text
from tests.extraction.slot_reader import sheets
from tests.extraction.slot_reader.test_arch_pair_question import Rates as PricedRates
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_architect_file_reader import _add_architectural
from tests.workflow.test_architect_reader import _stages, _upload
from tests.workflow.test_markup_route import _SilentOcr
from tests.workflow.test_slot_reader import (
    BOTH_WALLS,
    FakeReaders,
    claude_crops_to_texts,
    runtime,
)
from tests.workflow.test_stages import _publish_rulebook
from workflow.architect_pairing import _ARCHITECT_COLOUR, _GLYPHS, _VENDOR_COLOUR
from workflow.architect_row_plan import ARCHITECT_CHECK_RULE_ID
from workflow.stages import DatabaseStages, _SpendMeter

pytest_plugins = ("tests.app.postgres_fixture",)

TEXTS: dict[int | None, str] = {0: '24"', 1: '48"', 2: '24"', None: '96"'}
VENDOR = sheets.sheet(sheets.text_labels(('24"', '48"', '24"'), '96"'))
#: The architect's cabinet sides, in the drawing's own space: 36, 72 and 36 pt at 1.5 pt per inch.
SIDES = (60.0, 96.0, 168.0, 204.0)


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


def _three_cabinets() -> bytes:
    """Three cabinets on a floor line, their widths dimensioned on one row whose ticks sit on the
    cabinets' solid sides, dashed extension lines as CAD draws them."""
    left, *_, right = SIDES
    parts = [
        b"0 0 0 RG 0.5 w",
        f"{left - 10} 60 m {right + 40} 60 l S".encode(),
        *(f"{x} 60 m {x} 120 l S".encode() for x in SIDES),
        f"{left} 120 m {right} 120 l S".encode(),
    ]
    stream = b"\n".join(parts) + b"\n"
    for x in SIDES:
        stream += _dashed(x, 3, 200)
    stream += _row(40, list(SIDES))
    for (x0, x1), label in zip(pairwise(SIDES), ("2' - 0\"", "4' - 0\"", "2' - 0\""), strict=True):
        stream += _text((x0 + x1) / 2 - 8, 43, label)
    return stream


def _architect_file() -> bytes:
    """One sheet, two twin views clearly apart, each with its bubble, title and scale note."""
    width = 1200
    body = sheet_parts._placed(_three_cabinets(), sheet_parts.DRAWING_ORIGIN)
    body += sheet_parts._label_block(60, "3", "SYNTHETIC ELEVATION", '1/4" = 1\'-0"')
    body += sheet_parts._placed(_three_cabinets(), sheet_parts.SECOND_ORIGIN)
    body += sheet_parts._label_block(
        sheet_parts.SECOND_ORIGIN[0] + 10, "4", "SECOND SYNTHETIC ELEVATION", '1/4" = 1\'-0"'
    )
    content = sheet_parts._frame(width) + body
    return sheet_parts._pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {sheet_parts.PAGE_HEIGHT}] "
                "/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
            ).encode(),
            sheet_parts._stream(content),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
    )


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


def _anchor(session: Session) -> ObservationCandidate:
    """The newest slot-reader row's first piece."""
    return session.scalars(
        select(ObservationCandidate)
        .where(ObservationCandidate.ambiguity_flags.contains(["slot-reader", "slot:0"]))
        .order_by(ObservationCandidate.created_at.desc())
        .limit(1)
    ).one()


def _match(session: Session, anchor: ObservationCandidate) -> ArchitectViewMatchRecord:
    return session.scalars(
        select(ArchitectViewMatchRecord)
        .where(ArchitectViewMatchRecord.row_anchor_candidate_id == anchor.id)
        .order_by(ArchitectViewMatchRecord.created_at.desc())
        .limit(1)
    ).one()


def test_pick_then_re_read_carries_the_match_and_pairs_by_code_and_both_ais_to_an_exact_pass(
    session: Session, store: LocalStore
) -> None:
    revision = _upload(session, store, VENDOR)
    _add_architectural(session, store, revision, _architect_file())
    reading = _stages(store)
    reading.extract_pages(session, revision.id)
    session.commit()
    _publish_rulebook(session)
    readers = Readers()
    plain = runtime(readers, claude_row_reader=True)
    # Priced, so every call is metered as in production.
    slot_runtime = replace(plain, form=replace(plain.form, rates=PricedRates()))  # type: ignore[arg-type]
    slot = DatabaseStages(
        store,
        dpi=150,
        ocr_engine=_SilentOcr(),
        missing_space=MISSING_SPACE,
        form_reader=slot_runtime.form,
        slot_reader=slot_runtime,
        architect_reader=MEASURED_ARCHITECT_SETTINGS,
    )
    # What `extract_pages` sets up before it reads: the drawing set's spend meter (#757), and the
    # architect reader's results for this extraction.
    slot._meter = _SpendMeter(cap_micros=1_000_000_000)
    slot._architect_pages = dict(reading._architect_pages)
    slot._architect_view_crops = dict(reading._architect_view_crops)
    assert len(slot._architect_view_crops) == 2, "both twin views were indexed"

    # 1. The first read: the real matcher sees twins (code ties), so the reviewer chooses.
    _read_slots(session, slot, revision, readers)
    first = _anchor(session)
    assert [first.value_numerator] == [24], "the vendor's row was read from the PDF"
    automatic = _match(session, first)
    assert (automatic.source, automatic.status) == ("automatic", "needs_reviewer")
    assert session.scalars(select(ArchitectPairingRecord)).all() == []
    candidates = [candidate["view_id"] for candidate in automatic.candidates]
    assert len(candidates) == 2

    # 2. The reviewer picks the second twin, through #1166's handler.
    package = session.get_one(Package, revision.package_id)
    principal = Principal(
        id="reviewer (synthetic test)",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({package.project_id}),
    )
    picked_view = UUID(str(candidates[1]))
    pick_architect_view(
        principal,
        principal,
        session,
        package.project_id,
        package.id,
        first.id,
        ArchitectViewPickIn(view_id=picked_view, expected_record_id=automatic.id),
    )

    # 3. The slot stage reads again: the pick is carried; code and both AIs pair against it.
    _read_slots(session, slot, revision, readers)
    second = _anchor(session)
    assert second.id != first.id
    carried = _match(session, second)
    assert (carried.source, carried.status, carried.matched_view_id) == (
        "carried",
        "carried_over",
        picked_view,
    )
    pairing = session.scalars(
        select(ArchitectPairingRecord).where(
            ArchitectPairingRecord.row_anchor_candidate_id == second.id
        )
    ).one()
    assert (pairing.source, pairing.status) == ("code+ais", "paired"), pairing.details["reasons"]
    assert pairing.details["architect_view_id"] == str(picked_view)
    assert pairing.details["match_record_id"] == str(carried.id)
    ai: Any = pairing.details["ai"]
    assert ai["prompt_id"] == ARCH_PAIR_2PANEL_PROMPT_ID

    # The badges drawn on the picture map to the ids the packet names: left to right, A-numbers
    # name the architect's spans left to right (`arch-slot:0`, `1`, `2`) of the picked view.
    (picture,) = set(readers.pairing_pictures[-2:])
    drawn = [label for label, _x in sorted(_badges(picture, _ARCHITECT_COLOUR), key=lambda b: b[1])]
    numbering = ai["numbering"]["architect"]
    assert sorted(drawn) == sorted(numbering)
    assert [numbering[label]["slot"] for label in drawn] == [0, 1, 2]
    for label in drawn:
        candidate = session.get_one(ObservationCandidate, UUID(numbering[label]["candidate_id"]))
        assert candidate.page_id is not None
        assert any(flag.startswith("arch-view:") for flag in candidate.ambiguity_flags or [])
    vendor_drawn = [
        label for label, _x in sorted(_badges(picture, _VENDOR_COLOUR), key=lambda b: b[1])
    ]
    assert vendor_drawn == ["V1", "V2", "V3"]

    # 4. The check stage: exact PASS, compared with the picked view.
    from app.models.rules import RuleDefinition, RuleSnapshot
    from app.models.verdicts import CheckRun, Finding

    DatabaseStages(store).run_checks(session, revision.id)
    session.commit()
    (finding,) = session.scalars(
        select(Finding)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == revision.id,
            CheckRun.superseded_at.is_(None),
            RuleDefinition.rule_id == ARCHITECT_CHECK_RULE_ID,
            Finding.scope_row_candidate_id.is_not(None),
        )
    ).all()
    assert finding.scope_row_candidate_id == second.id
    assert finding.outcome == "PASS", (finding.reason, finding.notes)
    assert (finding.notes or [""])[0].startswith("compared with ")
