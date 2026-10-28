"""The Measure page's parts of each drawing: listed, then confirmed one at a time by a person (#882).

Verification for `app/api/drawing_parts.py` and `app/evidence/parts.py`, and for the match stage
reading only parts a person confirmed and has not taken back.

The sheet is `tests/workflow/test_part_proposals_route.py`'s: one vendor drawing, labelled as the
vendor's but not yet confirmed, on which the page stage suggests two cabinets (the first under the
invented code `XQ24`) and a countertop. **Every code here is invented.**

**The outcome that matters most is a count of zero**: listing the suggestions, however often, makes
no part. A suggestion becomes a part only through a person's confirmation (#852).

**Each part's own picture (#897)** is served checked against its digest, through the same project
boundary, and adding a part asks the worker to cut one in the same transaction. **Each part says
what its picture was found to show of GV's own coloured marks (#921)**: `shown`, `not_shown`, or
`not_checked` for a picture cut before the check existed.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import (
    DrawingView,
    Package,
    PackageRevision,
    Page,
    PartConfirmation,
    PartProposal,
    VendorPagePicture,
    ViewRole,
)
from app.models.runs import ModelInvocation
from evidence.crop import decode_rgb_png
from extraction.rasterise import render_page
from storage.local import LocalStore
from tests.workflow.test_association import SETTINGS as ASSOCIATION_SETTINGS
from tests.workflow.test_part_pictures import MARKED_SHEET
from tests.workflow.test_part_proposals_route import _extract, _upgrade
from workflow.stages import MAXIMUM_RENDER_PIXELS, DatabaseStages
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)


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


def _sheet(
    session: Session, store: LocalStore, data: bytes | None = None
) -> tuple[PackageRevision, UUID, UUID]:
    """The extracted sheet, or another drawing: its revision, its project and its package."""
    revision, _ = _extract(session, store) if data is None else _extract(session, store, data=data)
    package = session.get_one(Package, revision.package_id)
    return revision, package.project_id, package.id


def _vendors(session: Session, role: ViewRole = ViewRole.SHOP) -> DrawingView:
    view = session.scalars(select(DrawingView)).one()
    confirm_view_role(session, view=view, role=role, actor="reviewer@example.com")
    session.commit()
    return view


# -- listing -------------------------------------------------------------------------------------


# -- which drawings may have parts -----------------------------------------------------------------


# -- deciding ------------------------------------------------------------------------------------


# -- adding --------------------------------------------------------------------------------------


# -- who may ---------------------------------------------------------------------------------------


# -- whether a picture shows GV's coloured marks (#921) -------------------------------------------


def test_vendor_page_picture_backfill_is_idempotent_and_model_free(
    session: Session, store: LocalStore
) -> None:
    revision, _, _ = _sheet(session, store, MARKED_SHEET)
    view = _vendors(session)
    stages = DatabaseStages(store=store, dpi=150)
    before_invocations = session.scalars(select(ModelInvocation.id)).all()
    before_proposals = session.scalars(select(PartProposal.id)).all()
    before_confirmations = session.scalars(select(PartConfirmation.id)).all()

    first = stages.render_vendor_page_pictures(session, revision.id)
    session.commit()
    second = stages.render_vendor_page_pictures(session, revision.id)
    session.commit()

    assert first["rendered"] == 1
    assert second["rendered"] == 0
    row = session.scalars(select(VendorPagePicture)).one()
    assert row.page_id == view.page_id
    assert row.width_px > 0 and row.height_px > 0
    assert row.snap_points == []
    assert row.snap_tolerance is None
    assert session.scalars(select(ModelInvocation.id)).all() == before_invocations
    assert session.scalars(select(PartProposal.id)).all() == before_proposals
    assert session.scalars(select(PartConfirmation.id)).all() == before_confirmations


def test_backfill_stores_pixels_from_the_vendor_only_page(
    session: Session, store: LocalStore
) -> None:
    revision, _, _ = _sheet(session, store, MARKED_SHEET)
    _vendors(session)
    stages = DatabaseStages(store=store, dpi=150)

    result = stages.render_vendor_page_pictures(session, revision.id)
    session.commit()
    assert result["rendered"] == 1

    picture = session.scalars(select(VendorPagePicture)).one()
    page = session.get_one(Page, picture.page_id)
    vendor = render_page(
        MARKED_SHEET,
        page.index,
        document_version_id=page.document_version_id,
        page_content_hash=page.content_hash,
        dpi=picture.dpi,
        maximum_pixels=MAXIMUM_RENDER_PIXELS,
        vendor_only=True,
    )
    full = render_page(
        MARKED_SHEET,
        page.index,
        document_version_id=page.document_version_id,
        page_content_hash=page.content_hash,
        dpi=picture.dpi,
        maximum_pixels=MAXIMUM_RENDER_PIXELS,
        vendor_only=False,
    )
    with store.get(picture.storage_key) as stored:
        width, height, pixels = decode_rgb_png(stored.read())

    assert (width, height, pixels) == (vendor.width_px, vendor.height_px, vendor.rgb_bytes)
    assert pixels != full.rgb_bytes


def test_backfill_publishes_only_detected_dimension_and_extension_endpoints(
    session: Session, store: LocalStore
) -> None:
    revision, _, _ = _sheet(session, store, MARKED_SHEET)
    _vendors(session)
    stages = DatabaseStages(store=store, dpi=150, association=ASSOCIATION_SETTINGS)

    result = stages.render_vendor_page_pictures(session, revision.id)
    session.commit()

    assert result["rendered"] == 1, result
    picture = session.scalars(select(VendorPagePicture)).one()
    assert picture.snap_tolerance == str(ASSOCIATION_SETTINGS.witness_tolerance)
    assert picture.snap_points
    assert {point["source"] for point in picture.snap_points} <= {"dimension", "extension"}


# -- only a person decides here (#1244, ADR-0021 amendment) ----------------------------------------


# -- why it is not a part (#1246, with phase 5 decision 6) -----------------------------------------
