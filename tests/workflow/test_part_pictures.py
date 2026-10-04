"""Every suggested part gets its own picture, cut from the vendor's drawing alone (#897).

Verification for `workflow/part_pictures.py` and for `DatabaseStages`'s picture cutting in
`workflow/stages.py`: the page stage cuts one per suggestion as it makes it, and
`DatabaseStages.cut_part_pictures` cuts the ones still missing, which is what a person adding a part
asks for.

The sheet is `tests/workflow/test_part_proposals_route.py`'s: one vendor drawing with two cabinets
drawn end to end and a countertop over both, and a reviewer's note carrying an invented code just
above the second cabinet. **Every code here is invented.**

**What matters most:**
- each suggestion's picture is the vendor's drawing round its outline and the stated margin, with
  the reviewer's note left out of it;
- cutting writes a picture and nothing else: never a part, a decision or a run;
- each picture records whether it shows GV's own coloured marks, baked into the vendor's drawing
  where the render cannot leave them out, as the agreement gate's own test finds them on the
  picture's own pixels; and "not checked" where that test could not be asked (#921).
"""

from __future__ import annotations

import dataclasses
import hashlib
import tempfile
from collections.abc import Iterator, Sequence
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.evidence.parts import add_part
from app.models import (
    CountertopRun,
    CountertopRunDecision,
    DrawingItem,
    DrawingView,
    ItemIdentifier,
    PackageRevision,
    Page,
    PartConfirmation,
    PartPicture,
    PartProposal,
    ReadingPart,
    ViewRole,
)
from evidence.crop import BoxCropSpec, crop_pixel_box, decode_rgb_png, generate_crop
from extraction.rasterise import render_page
from extraction.reader import UnreadablePdf
from storage.local import LocalStore
from tests.extraction.test_annotations import _appearance, _free_text, _pdf, _stamp
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_association import SETTINGS
from tests.workflow.test_association import _revision as _stored_revision
from tests.workflow.test_markup_route import _SilentOcr
from tests.workflow.test_part_proposals_route import (
    DRAWING,
    HELVETICA,
    SHEET,
    _drawing_appearance,
    _sheet,
    _upgrade,
)
from vocabulary.part_kinds import PartKind
from workflow import stages as stages_module
from workflow.part_pictures import PNG, PartPictureSettings
from workflow.parts import outline_box
from workflow.review import PageResult
from workflow.stages import ColouredMarkup, DatabaseStages
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

#: Half an inch round each part, at the resolution the sheet's own tests read at. The reviewer's
#: note sits ten points above the second cabinet's box, so this margin takes it into that picture.
PICTURES = PartPictureSettings(margin_pt=Decimal(36), dpi=150)

#: GV's `38` in red, written into the vendor's drawing itself (#921), as a snapshot's markup is: at
#: appearance `(300, 525)`, page `(250, 75)`, just below the second cabinet's dimension. Inside that
#: cabinet's picture; outside the first cabinet's, which ends 36 points past page `x = 200`, and the
#: countertop's, which starts 36 points below page `y = 160`.
RED_TEXT = b"1 0 0 rg BT /F1 8 Tf 300 525 Td (38) Tj ET\n"
#: The same `38` in the vendor's black: the vendor's own number, not a mark.
BLACK_TEXT = b"0 0 0 rg BT /F1 8 Tf 300 525 Td (38) Tj ET\n"
#: GV's mark drawn as a glyph-sized red stroke at the same place, as a pen stroke is (#834): found
#: among the glyph paths the page's layers read, not in its text.
RED_STROKE = b"1 0 0 RG 1 w 300 525 m 306 530 l S\n"

#: The sheet with GV's red `38` in the vendor's drawing.
MARKED_SHEET = _sheet(DRAWING + RED_TEXT)

#: #929's marks at the same place, none of which the glyph-sized test saw: a red line 100 points
#: long, page `(240, 75)` to `(340, 75)`, through the second cabinet's picture and clear of the
#: first's; a yellow fill 13 x 7 points in a red outline, as GV's pasted outlet symbols are drawn;
#: and the same line and fill in the vendor's black and grey.
LONG_RED_LINE = b"1 0 0 RG 1 w 290 525 m 390 525 l S\n"
YELLOW_FILL = b"1 0 0 RG 1 1 0.5 rg 300 520 13 7 re B\n"
LONG_BLACK_LINE = b"0 0 0 RG 1 w 290 525 m 390 525 l S\n"
GREY_FILL = b"0.6 g 300 520 13 7 re f\n"


def _sheet_with_a_pasted_stamp(rect: bytes) -> bytes:
    """`SHEET` with a small stamp pasted onto the vendor's drawing at `rect`, holding one black
    stroke, as GV's reviewer pasted outlet symbols onto AI_Set_2's 17th sheet (#929). Objects 5 to
    8 are the annotations, 9 the drawing's appearance, 10 its font and 11 the pasted stamp's."""
    return _pdf(
        annotations=[
            _free_text("VENDOR'S SHOP DRAWING ELEVATION ", rect=b"[60 262 340 280]"),
            _free_text("XQ30L", rect=b"[220 140 260 150]"),
            _stamp(appearance_object=9),
            _stamp(rect=rect, appearance_object=11),
        ],
        extra_objects=[
            _drawing_appearance(10, DRAWING),
            HELVETICA,
            _appearance(
                b"0 0 0 RG 0.5 w 101 501 m 113 511 l S",
                bbox=b"[100 500 114 512]",
                matrix=b"[1 0 0 1 -100 -500]",
            ),
        ],
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


def _stages(store: LocalStore, pictures: PartPictureSettings | None = PICTURES) -> DatabaseStages:
    return DatabaseStages(
        store=store,
        dpi=150,
        ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
        association=SETTINGS,
        # The reader's own setting (#912): the page stage reads the sheet's text before it suggests
        # any part, and checking each picture for GV's coloured marks reads the coloured text in the
        # pasted drawing with it (#921).
        missing_space=MISSING_SPACE,
        part_pictures=pictures,
    )


def _extract(
    session: Session,
    store: LocalStore,
    pictures: PartPictureSettings | None = PICTURES,
    *,
    data: bytes = SHEET,
) -> tuple[PackageRevision, Sequence[PageResult]]:
    revision = _stored_revision(session, store, data=data)
    session.commit()
    result = _stages(store, pictures).extract_pages(session, revision.id)
    session.commit()
    return revision, result


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _left(proposal: PartProposal) -> tuple[Decimal, Decimal]:
    left, _, right, _ = outline_box(proposal.extent)
    return left, right


def _proposals(session: Session) -> list[PartProposal]:
    """Cabinets left to right, then the countertop."""
    return sorted(session.scalars(select(PartProposal)), key=lambda row: (row.kind, _left(row)))


def _picture_of(session: Session, proposal: PartProposal) -> PartPicture:
    return session.scalars(
        select(PartPicture).where(PartPicture.part_proposal_id == proposal.id)
    ).one()


def _bytes(store: LocalStore, key: str) -> bytes:
    with store.get(key) as stored:
        return stored.read()


def _expected_crop(
    session: Session, proposal: PartProposal, *, vendor_only: bool, store: LocalStore
) -> bytes:
    """The picture cut from the page rendered with or without the reviewer's markup, at the stated
    margin and resolution: the same box `generate_crop` cuts, so the bytes compare exactly."""
    view = session.get_one(DrawingView, proposal.drawing_view_id)
    page = session.get_one(Page, view.page_id)
    rendered = render_page(
        SHEET,
        page.index,
        document_version_id=page.document_version_id,
        page_content_hash=page.content_hash,
        dpi=PICTURES.dpi,
        maximum_pixels=stages_module.MAXIMUM_RENDER_PIXELS,
        vendor_only=vendor_only,
    )
    left, top, right, bottom = outline_box(proposal.extent)
    result = generate_crop(
        rendered,
        BoxCropSpec(
            document_version_id=page.document_version_id,
            page=page.index,
            left=left,
            top=top,
            right=right,
            bottom=bottom,
            context_margin_pt=PICTURES.margin_pt,
            dpi=PICTURES.dpi,
        ),
        store,
    )
    assert result.artifact is not None, result.reason
    return _bytes(store, result.artifact.key)


# -- the page stage cuts one per suggestion --------------------------------------------------------


def test_the_stage_cuts_and_records_a_picture_of_every_suggestion(
    session: Session, store: LocalStore
) -> None:
    """**Done when, 1.** Outcome: two cabinets and a countertop suggested, three pictures recorded,
    each pointing at stored PNG bytes whose digest it holds, with the margin and resolution they were
    cut at; and the page result says three were cut."""
    _, result = _extract(session, store)

    proposals = _proposals(session)
    pictures = [_picture_of(session, proposal) for proposal in proposals]

    assert [proposal.kind for proposal in proposals] == ["cabinet", "cabinet", "countertop"]
    assert [page.payload["part_pictures"] for page in result] == [
        {"cut": 3, "refused": 0, "refusals": []}
    ]
    for picture in pictures:
        stored = _bytes(store, picture.storage_key)
        assert stored.startswith(b"\x89PNG\r\n\x1a\n")
        assert hashlib.sha256(stored).hexdigest() == picture.sha256
        assert picture.media_type == PNG
        assert (picture.margin_pt, picture.dpi) == (Decimal(36), 150)
    assert len({picture.sha256 for picture in pictures}) == 3


def test_the_picture_is_cut_from_the_vendors_drawing_alone(
    session: Session, store: LocalStore
) -> None:
    """**Done when, 7.** The reviewer's note lies inside the second cabinet's picture. Outcome: the
    stored picture is byte for byte the crop of the page rendered without the reviewer's markup,
    and not the crop of the page as a reviewer sees it, which shows the note (#742)."""
    _extract(session, store)
    _, second, _ = _proposals(session)

    stored = _bytes(store, _picture_of(session, second).storage_key)
    vendors = _expected_crop(session, second, vendor_only=True, store=store)
    both_layers = _expected_crop(session, second, vendor_only=False, store=store)

    assert stored == vendors
    assert stored != both_layers


@pytest.mark.parametrize("data", [SHEET, MARKED_SHEET], ids=["clean", "gv-mark"])
def test_cutting_pictures_writes_no_part_decision_or_run(
    session: Session, store: LocalStore, data: bytes
) -> None:
    """**Done when, 3.** The worker writes a picture of each suggestion and nothing else: no item,
    no code, no decision, no run and no link, by extraction or by the job a person asks for. A
    picture that shows GV's coloured marks changes none of that (#921)."""
    revision, _ = _extract(session, store, data=data)
    view = session.scalars(select(DrawingView)).one()
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="reviewer@example.com")
    session.commit()
    _stages(store).cut_part_pictures(session, revision.id)
    session.commit()

    assert _count(session, PartPicture) == 3
    for model in (
        DrawingItem,
        ItemIdentifier,
        PartConfirmation,
        CountertopRun,
        CountertopRunDecision,
        ReadingPart,
    ):
        assert _count(session, model) == 0, model.__name__


def test_a_second_read_cuts_nothing_twice(session: Session, store: LocalStore) -> None:
    """A redelivery finds the suggestions and their pictures already there: no second row, and the
    page result says nothing was cut."""
    revision, _ = _extract(session, store)
    first = {row.part_proposal_id: row.id for row in session.scalars(select(PartPicture))}

    again = _stages(store).extract_pages(session, revision.id)
    session.commit()

    assert {row.part_proposal_id: row.id for row in session.scalars(select(PartPicture))} == first
    assert [page.payload["part_pictures"] for page in again] == [
        {"cut": 0, "refused": 0, "refusals": []}
    ]


def test_without_settings_no_picture_is_cut(session: Session, store: LocalStore) -> None:
    """`None` rather than zero: nobody said how to cut one, which is not "none cut"."""
    _, result = _extract(session, store, pictures=None)

    assert _count(session, PartProposal) == 3
    assert _count(session, PartPicture) == 0
    assert [page.payload["part_pictures"] for page in result] == [None]


def test_a_page_too_large_to_render_refuses_every_picture_with_its_reason(
    session: Session, store: LocalStore
) -> None:
    """A page that would be over the pixel budget at the stated resolution is not rendered for its
    pictures. Outcome: every suggestion keeps its place with no picture, and the result says why in
    words, once; the rest of the page is read as before."""
    _, result = _extract(session, store, PartPictureSettings(margin_pt=Decimal(36), dpi=2000))

    assert _count(session, PartProposal) == 3
    assert _count(session, PartPicture) == 0
    (payload,) = [page.payload["part_pictures"] for page in result]
    assert isinstance(payload, dict)
    assert (payload["cut"], payload["refused"]) == (0, 3)
    (reason,) = cast(list[str], payload["refusals"])
    assert reason.startswith("page 0: ") and "over the budget" in reason


# -- a part a person adds, and the job that cuts what is missing -----------------------------------


def _add_between_the_cabinets(session: Session, revision: PackageRevision) -> PartConfirmation:
    """A person adds a filler from the first cabinet's left end to the second's right end: a part
    whose outline is the line between two ends, with no height (#882)."""
    view = session.scalars(select(DrawingView)).one()
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="reviewer@example.com")
    first, second, _ = _proposals(session)
    assert first.defining_line is not None and second.defining_line is not None
    left = cast(list[list[str]], first.defining_line["points"])[0]
    right = cast(list[list[str]], second.defining_line["points"])[1]
    added = add_part(
        session,
        package_revision_id=revision.id,
        view_id=view.id,
        kind=PartKind.FILLER,
        code=None,
        ends=((left[0], left[1]), (right[0], right[1])),
        actor="reviewer@example.com",
    )
    assert isinstance(added, PartConfirmation), added
    session.commit()
    return added


def test_a_part_a_person_adds_gets_its_picture_from_the_job(
    session: Session, store: LocalStore
) -> None:
    """**Done when, 1, for an added part.** The page stage has cut the three suggestions' pictures;
    a person then adds a part. Outcome: the job cuts exactly that one, and its picture is the line
    and the margin round it: as tall as twice the margin, because nothing invents a height."""
    revision, _ = _extract(session, store)
    added = _add_between_the_cabinets(session, revision)

    result = _stages(store).cut_part_pictures(session, revision.id)
    session.commit()

    proposal = session.get_one(PartProposal, added.part_proposal_id)
    picture = _picture_of(session, proposal)
    view = session.get_one(DrawingView, proposal.drawing_view_id)
    page = session.get_one(Page, view.page_id)
    _, height, _ = decode_rgb_png(_bytes(store, picture.storage_key))
    left, top, right, bottom = outline_box(proposal.extent)
    rendered_height = round(page.height_pt * PICTURES.dpi / 72)

    assert result == {"ran": True, "cut": 1, "refused": 0, "refusals": []}
    assert _count(session, PartPicture) == 4
    assert top == bottom
    assert abs(Decimal(height) - 2 * PICTURES.margin_pt * PICTURES.dpi / 72) <= 2
    assert height < rendered_height
    assert left < right


def test_the_job_cuts_nothing_twice(session: Session, store: LocalStore) -> None:
    revision, _ = _extract(session, store)
    _add_between_the_cabinets(session, revision)
    stages = _stages(store)
    stages.cut_part_pictures(session, revision.id)
    session.commit()

    assert stages.cut_part_pictures(session, revision.id) == {
        "ran": True,
        "cut": 0,
        "refused": 0,
        "refusals": [],
    }
    assert _count(session, PartPicture) == 4


def test_a_re_read_also_cuts_an_added_parts_missing_picture(
    session: Session, store: LocalStore
) -> None:
    """The page stage cuts every suggestion on the page that has none, a person's own included, so
    an added part whose job never ran is cut on the next read."""
    revision, _ = _extract(session, store)
    _add_between_the_cabinets(session, revision)

    again = _stages(store).extract_pages(session, revision.id)
    session.commit()

    assert [page.payload["part_pictures"] for page in again] == [
        {"cut": 1, "refused": 0, "refusals": []}
    ]
    assert _count(session, PartPicture) == 4


def test_the_job_says_why_it_did_not_run(session: Session, store: LocalStore) -> None:
    """No settings, or no store: nothing is cut, and the result says which."""
    revision, _ = _extract(session, store, pictures=None)

    assert _stages(store, pictures=None).cut_part_pictures(session, revision.id) == {
        "ran": False,
        "reason": "no part picture settings are stated",
    }
    assert DatabaseStages(part_pictures=PICTURES).cut_part_pictures(session, revision.id) == {
        "ran": False,
        "reason": "no artifact store is configured",
    }
    assert _count(session, PartPicture) == 0


class _AlteredStore(LocalStore):
    """A store whose document comes back as different bytes, as a replaced file would."""

    def get(self, key: str) -> BinaryIO:
        with super().get(key) as stored:
            data = stored.read()
        return BytesIO(data + b"%altered" if data.startswith(b"%PDF") else data)


def test_the_job_does_not_render_a_document_that_no_longer_matches_its_digest(
    session: Session, store: LocalStore
) -> None:
    """A document whose bytes are not the ones uploaded is not drawn from: every missing picture is
    refused, with the reason."""
    revision, _ = _extract(session, store, pictures=None)
    altered = _AlteredStore(root=store.root, ticket_secret=b"a secret only this test knows")

    result = _stages(altered).cut_part_pictures(session, revision.id)

    assert result["ran"] is True
    assert (result["cut"], result["refused"]) == (0, 3)
    (reason,) = cast(list[str], result["refusals"])
    assert "does not match the digest recorded when it was uploaded" in reason
    assert _count(session, PartPicture) == 0


# -- the settings have no defaults -----------------------------------------------------------------


def test_the_settings_have_no_defaults() -> None:
    """**Done when, 6.** Both are stated or the settings cannot be built."""
    fields = dataclasses.fields(PartPictureSettings)

    assert {field.name for field in fields} == {"margin_pt", "dpi"}
    assert all(
        field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING
        for field in fields
    )
    with pytest.raises(TypeError):
        PartPictureSettings()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("margin", "dpi", "error"),
    [
        (36.0, 150, TypeError),
        ("36", 150, TypeError),
        (Decimal(0), 150, ValueError),
        (Decimal(-1), 150, ValueError),
        (Decimal("NaN"), 150, ValueError),
        (Decimal("Infinity"), 150, ValueError),
        (Decimal(36), 0, ValueError),
        (Decimal(36), -150, ValueError),
        (Decimal(36), 150.0, TypeError),
        (Decimal(36), True, TypeError),
    ],
)
def test_settings_that_cannot_cut_a_picture_are_refused(
    margin: object, dpi: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        PartPictureSettings(margin_pt=margin, dpi=dpi)  # type: ignore[arg-type]


def test_the_stages_refuse_anything_but_picture_settings() -> None:
    with pytest.raises(TypeError, match="PartPictureSettings"):
        DatabaseStages(part_pictures={"margin_pt": 36, "dpi": 150})  # type: ignore[arg-type]


def test_each_picture_is_the_outline_and_the_margin_round_it(
    session: Session, store: LocalStore
) -> None:
    """Each picture's pixels are the ones `crop_pixel_box` names for the part's box and the margin,
    at the stated resolution: a 100-point cabinet with a 36-point margin at 150 dpi is 172 points,
    about 358 pixels, across, rounded outward on each side; and no picture is the whole page."""
    _extract(session, store)

    for proposal in _proposals(session):
        view = session.get_one(DrawingView, proposal.drawing_view_id)
        page = session.get_one(Page, view.page_id)
        rendered = render_page(
            SHEET,
            page.index,
            document_version_id=page.document_version_id,
            page_content_hash=page.content_hash,
            dpi=PICTURES.dpi,
            maximum_pixels=stages_module.MAXIMUM_RENDER_PIXELS,
            vendor_only=True,
        )
        left, top, right, bottom = outline_box(proposal.extent)
        box = crop_pixel_box(
            rendered,
            BoxCropSpec(
                document_version_id=page.document_version_id,
                page=page.index,
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                context_margin_pt=PICTURES.margin_pt,
                dpi=PICTURES.dpi,
            ),
        )
        picture = _bytes(store, _picture_of(session, proposal).storage_key)
        width, height, _ = decode_rgb_png(picture)

        assert (width, height) == (box[2] - box[0], box[3] - box[1])
        assert (width, height) != (rendered.width_px, rendered.height_px)
        if proposal.kind == PartKind.CABINET.value:
            assert 358 <= width <= 360


# -- whether a picture shows GV's coloured marks (#921) ------------------------------------------


def _marks(session: Session) -> list[bool | None]:
    """Each suggestion's recorded answer: the cabinets left to right, then the countertop."""
    return [_picture_of(session, proposal).shows_gv_marks for proposal in _proposals(session)]


def _picture_box(session: Session, proposal: PartProposal) -> tuple[int, int, int, int]:
    """The pixels a suggestion's picture is cut by: `crop_pixel_box` on the vendor-only page at the
    stated resolution, as the stage cuts it."""
    view = session.get_one(DrawingView, proposal.drawing_view_id)
    page = session.get_one(Page, view.page_id)
    rendered = render_page(
        MARKED_SHEET,
        page.index,
        document_version_id=page.document_version_id,
        page_content_hash=page.content_hash,
        dpi=PICTURES.dpi,
        maximum_pixels=stages_module.MAXIMUM_RENDER_PIXELS,
        vendor_only=True,
    )
    left, top, right, bottom = outline_box(proposal.extent)
    return crop_pixel_box(
        rendered,
        BoxCropSpec(
            document_version_id=page.document_version_id,
            page=page.index,
            left=left,
            top=top,
            right=right,
            bottom=bottom,
            context_margin_pt=PICTURES.margin_pt,
            dpi=PICTURES.dpi,
        ),
    )


@pytest.mark.parametrize(
    ("mark", "expected"),
    [
        (RED_TEXT, [False, True, False]),
        (RED_STROKE, [False, True, False]),
        (BLACK_TEXT, [False, False, False]),
        (b"", [False, False, False]),
    ],
    ids=["red-text", "red-stroke", "black-text", "no-mark"],
)
def test_each_picture_records_whether_it_shows_gv_s_coloured_marks(
    session: Session, store: LocalStore, mark: bytes, expected: list[bool | None]
) -> None:
    """**#921, done when, first line, on a made-up sheet.** GV's red `38`, as text or as a pen
    stroke, lies in the second cabinet's picture only: that picture records it, and the first
    cabinet's and the countertop's record none. The same `38` in the vendor's black is no mark, and
    nor is the reviewer's note over the second cabinet, which the picture leaves out (#742)."""
    _, result = _extract(session, store, data=_sheet(DRAWING + mark))

    assert [proposal.kind for proposal in _proposals(session)] == [
        "cabinet",
        "cabinet",
        "countertop",
    ]
    assert _marks(session) == expected
    assert [page.payload["part_pictures"] for page in result] == [
        {"cut": 3, "refused": 0, "refusals": []}
    ]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_sheet(DRAWING + LONG_RED_LINE), [False, True, False]),
        (_sheet(DRAWING + YELLOW_FILL), [False, True, False]),
        (_sheet_with_a_pasted_stamp(b"[250 70 264 82]"), [False, True, False]),
        (_sheet(DRAWING + LONG_BLACK_LINE), [False, False, False]),
        (_sheet(DRAWING + GREY_FILL), [False, False, False]),
    ],
    ids=["long-red-line", "yellow-fill", "pasted-stamp", "long-black-line", "grey-fill"],
)
def test_a_picture_showing_a_long_line_a_fill_or_a_pasted_stamp_records_it(
    session: Session, store: LocalStore, data: bytes, expected: list[bool | None]
) -> None:
    """**#929 in the part pictures.** A long red line, a yellow fill in a red outline, or a stamp
    pasted onto the drawing — the three marks the glyph-sized test missed, the last as on AI_Set_2's
    17th sheet — lie in the second cabinet's picture only, and it records them; the same line and
    fill in the vendor's black and grey are no mark."""
    _extract(session, store, data=data)

    assert [proposal.kind for proposal in _proposals(session)] == [
        "cabinet",
        "cabinet",
        "countertop",
    ]
    assert _marks(session) == expected


def test_each_picture_s_markup_is_gathered_by_the_one_production_function(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**One function, not two (#929).** The markup a picture is checked against is
    `coloured_markup`'s, the function the agreement gate and the gate replay gather theirs by,
    asked once for the page at the picture's own resolution. Made to find nothing, it leaves the
    second cabinet's picture with the long red line unmarked."""
    real = stages_module.coloured_markup
    asked: list[int] = []

    def nothing(*arguments: object, **keywords: object) -> ColouredMarkup:
        found = real(*arguments, **keywords)  # type: ignore[arg-type]
        asked.append(int(keywords["dpi"]))  # type: ignore[call-overload]
        if int(keywords["dpi"]) == PICTURES.dpi:
            assert found.coloured_paths, "the real function finds the red line"
        return ColouredMarkup(
            text=(), paths=(), transform=None, coloured_paths=(), pasted_stamps=()
        )

    monkeypatch.setattr(stages_module, "coloured_markup", nothing)
    _extract(session, store, data=_sheet(DRAWING + LONG_RED_LINE))

    assert asked.count(PICTURES.dpi) == 1
    assert _marks(session) == [False, False, False]


@pytest.mark.parametrize("dpi", [100, 300])
def test_a_picture_cut_at_another_resolution_is_checked_on_its_own_pixels(
    session: Session, store: LocalStore, dpi: int
) -> None:
    """The stage reads the page at 150 dpi; a picture cut at another resolution is checked on its
    own pixels, with the coloured markup read at that resolution, so the answers are the same."""
    _extract(session, store, PartPictureSettings(margin_pt=Decimal(36), dpi=dpi), data=MARKED_SHEET)

    assert _marks(session) == [False, True, False]
    assert {_picture_of(session, proposal).dpi for proposal in _proposals(session)} == {dpi}


def test_the_answer_is_the_gate_s_own_test_asked_about_the_picture_s_own_pixels(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**#921, done when, second line.** Each picture's answer is what `crop_shows_a_gv_mark` — the
    test the agreement gate asks through `gv_mark_in_crop` (#901) — returned, asked once per picture
    about the rectangle that picture was cut by, with the page's coloured markup read at the
    picture's own resolution. The test is made to answer the opposite of the truth here, so a
    recorded answer can only have come from it."""
    real = stages_module.crop_shows_a_gv_mark
    calls: list[tuple[tuple[int, int, int, int], ColouredMarkup, bool]] = []

    def opposite(crop_box: tuple[int, int, int, int], markup: ColouredMarkup) -> bool:
        answer = not real(crop_box, markup)
        calls.append((crop_box, markup, answer))
        return answer

    monkeypatch.setattr(stages_module, "crop_shows_a_gv_mark", opposite)
    _extract(session, store, data=MARKED_SHEET)

    proposals = _proposals(session)
    answered = {box: answer for box, _, answer in calls}
    assert len(calls) == len(proposals) == 3
    assert set(answered) == {_picture_box(session, proposal) for proposal in proposals}
    for proposal in proposals:
        assert (
            _picture_of(session, proposal).shows_gv_marks
            is answered[_picture_box(session, proposal)]
        )
    assert _marks(session) == [True, False, True]
    for _, markup, _ in calls:
        assert markup.transform is not None and markup.transform.dpi == PICTURES.dpi
        assert markup.text, "the red 38 is read as markup in colour"


def test_a_page_whose_coloured_markup_cannot_be_read_has_its_pictures_not_checked(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the pasted drawings cannot be read for their coloured text, nothing rules a mark out
    and nothing says a picture is clean: each picture is still cut, and records `None`, "not
    checked". The markup is read once for the page, not once per picture."""
    reads: list[int] = []

    def unreadable(*arguments: object, **keywords: object) -> tuple[tuple[int, int, int, int], ...]:
        reads.append(1)
        raise UnreadablePdf("the pasted drawings could not be read")

    monkeypatch.setattr(stages_module, "coloured_text", unreadable)
    _, result = _extract(session, store, data=MARKED_SHEET)

    assert _marks(session) == [None, None, None]
    assert [page.payload["part_pictures"] for page in result] == [
        {"cut": 3, "refused": 0, "refusals": []}
    ]
    assert len(reads) == 1


@pytest.mark.parametrize(
    ("mark", "expected"),
    [(RED_TEXT, True), (RED_STROKE, True), (BLACK_TEXT, False)],
    ids=["red-text", "red-stroke", "black-text"],
)
def test_the_job_checks_an_added_part_s_picture_as_the_stage_does(
    session: Session, store: LocalStore, mark: bytes, expected: bool
) -> None:
    """A part a person adds is cut by the job, outside the page stage, and checked the same way:
    the glyph paths its page's layers read, and the coloured text. The added filler runs from the
    first cabinet's left end to the second's right end, so its picture holds the mark."""
    revision, _ = _extract(session, store, data=_sheet(DRAWING + mark))
    added = _add_between_the_cabinets(session, revision)

    result = _stages(store).cut_part_pictures(session, revision.id)
    session.commit()

    picture = _picture_of(session, session.get_one(PartProposal, added.part_proposal_id))
    assert result == {"ran": True, "cut": 1, "refused": 0, "refusals": []}
    assert picture.shows_gv_marks is expected


def test_the_job_cuts_nothing_without_the_reader_s_setting(
    session: Session, store: LocalStore
) -> None:
    """Without the reader's missing-space setting the coloured text cannot be read (#912), so no
    picture could be checked and the page would warn under none: the job cuts nothing, and says
    why, rather than cutting pictures nobody checked."""
    revision, _ = _extract(session, store, pictures=None, data=MARKED_SHEET)
    stages = DatabaseStages(store=store, dpi=150, association=SETTINGS, part_pictures=PICTURES)

    assert stages.cut_part_pictures(session, revision.id) == {
        "ran": False,
        "reason": (
            "the reader's missing-space setting is not stated, so no picture could be checked for "
            "GV's coloured marks"
        ),
    }
    assert _count(session, PartPicture) == 0
