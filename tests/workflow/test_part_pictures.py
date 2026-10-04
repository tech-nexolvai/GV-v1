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
- cutting writes a picture and nothing else: never a part, a decision or a run.
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
from storage.local import LocalStore
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_association import SETTINGS
from tests.workflow.test_association import _revision as _stored_revision
from tests.workflow.test_markup_route import _SilentOcr
from tests.workflow.test_part_proposals_route import SHEET, _upgrade
from vocabulary.part_kinds import PartKind
from workflow import stages as stages_module
from workflow.part_pictures import PNG, PartPictureSettings
from workflow.parts import outline_box
from workflow.review import PageResult
from workflow.stages import DatabaseStages
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

#: Half an inch round each part, at the resolution the sheet's own tests read at. The reviewer's
#: note sits ten points above the second cabinet's box, so this margin takes it into that picture.
PICTURES = PartPictureSettings(margin_pt=Decimal(36), dpi=150)


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
        # any part. Cutting a picture reads no text and does not use it.
        missing_space=MISSING_SPACE,
        part_pictures=pictures,
    )


def _extract(
    session: Session, store: LocalStore, pictures: PartPictureSettings | None = PICTURES
) -> tuple[PackageRevision, Sequence[PageResult]]:
    revision = _stored_revision(session, store, data=SHEET)
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


def test_cutting_pictures_writes_no_part_decision_or_run(
    session: Session, store: LocalStore
) -> None:
    """**Done when, 3.** The worker writes a picture of each suggestion and nothing else: no item,
    no code, no decision, no run and no link, by extraction or by the job a person asks for."""
    revision, _ = _extract(session, store)
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
