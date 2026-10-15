"""The pages of both drawings, for the simple review's side-by-side viewer (#1342, W7).

One read-only listing per package: each document (the vendor's shop drawing and the architect's
drawing), its pages, whether each page's picture is ready and where to fetch it, and, on a combined
sheet, the architect's drawing's region on the same page so the viewer can zoom to it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Page, ViewRole
from app.models.package import Package
from storage.local import LocalStore
from tests.api.test_page_pictures import _client
from tests.evidence.test_sides import (  # noqa: F401
    MISSING_SPACE,
    _halves,
    _page,
    _read,
    session,
    store,
)
from workflow.stages import DatabaseStages
from workflow.view_roles import record_panel_view

pytest_plugins = ("tests.app.postgres_fixture",)


def _listing(client: Any, package: Package) -> dict[str, Any]:
    response = client.get(f"/api/v1/projects/{package.project_id}/packages/{package.id}/pages")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_a_combined_sheet_lists_its_page_and_the_architects_region_on_it(
    session: Session, store: LocalStore  # noqa: F811
) -> None:
    """One shop upload; its left half confirmed the architect's. The viewer shows the same page on
    both sides and zooms the architect side to the left half."""
    revision = _read(session, store, kind="shop")
    _halves(session, revision, left=ViewRole.ARCH, right=ViewRole.SHOP)
    package = session.get(Package, revision.package_id)
    assert package is not None
    client = _client(session, store, package.project_id)

    body = _listing(client, package)
    assert body["package_revision_id"] == str(revision.id)
    assert body["combined_set"] is True
    assert body["pages_without_pictures"] == 1
    [document] = body["documents"]
    assert document["kind"] == "shop"
    assert document["side"] == "vendor"
    assert document["page_count"] == 1
    [page] = document["pages"]
    assert page["page_number"] == 1
    assert page["picture_status"] == "not_ready"
    assert page["picture"] is None
    # Only the architect's drawing is a zoom target; the vendor's half is the page itself.
    [region] = page["architect_regions"]
    assert region["role_confirmed"] is True
    assert region["bbox"] == {"x0": "0", "y0": "0", "x1": "0.5", "y1": "1"}
    assert [[Decimal(x), Decimal(y)] for x, y in region["polygon"]] == [
        [Decimal(0), Decimal(0)],
        [Decimal("0.5"), Decimal(0)],
        [Decimal("0.5"), Decimal(1)],
        [Decimal(0), Decimal(1)],
    ]

    # Once the worker has rendered it, the page says how big it is and where to fetch it.
    DatabaseStages(store, missing_space=MISSING_SPACE).render_vendor_page_pictures(
        session, revision.id
    )
    body = _listing(client, package)
    assert body["pages_without_pictures"] == 0
    [page] = body["documents"][0]["pages"]
    assert page["picture_status"] == "ready"
    picture = page["picture"]
    assert picture["width_px"] > 0 and picture["height_px"] > 0 and picture["dpi"] > 0
    assert picture["media_type"] == "image/png"
    assert f"document_version_id={document['document_version_id']}" in picture["url"]
    shown = client.get(picture["url"])
    assert shown.status_code == 200, shown.text
    assert shown.content.startswith(b"\x89PNG")


def test_an_unconfirmed_architect_suggestion_is_listed_as_unconfirmed(
    session: Session, store: LocalStore  # noqa: F811
) -> None:
    """A suggestion is a place to look, never a role: it is listed and marked unconfirmed. A drawing
    nothing suggests anything about is not an architect region."""
    revision = _read(session, store, kind="shop")
    page = _page(session, revision)
    for index, proposed in enumerate(("arch", None)):
        record_panel_view(
            session,
            page_id=page.id,
            annotation_index=index,
            stored_points=(
                (Decimal(index) / 2, Decimal(0)),
                (Decimal(index + 1) / 2, Decimal(0)),
                (Decimal(index + 1) / 2, Decimal(1)),
                (Decimal(index) / 2, Decimal(1)),
            ),
            proposed_role=proposed,
            heading="ID SET ELEVATION" if proposed else None,
            reason="a half of the test sheet",
        )
    package = session.get(Package, revision.package_id)
    assert package is not None

    body = _listing(_client(session, store, package.project_id), package)
    [region] = body["documents"][0]["pages"][0]["architect_regions"]
    assert region["role_confirmed"] is False
    assert region["heading"] == "ID SET ELEVATION"
    assert region["bbox"]["x1"] == "0.5"


def test_a_two_file_package_lists_both_documents_with_their_own_picture_links(
    session: Session, store: LocalStore  # noqa: F811
) -> None:
    """The architect's file is its own document; its page 1 is fetched by its own version id, never
    the shop drawing's page 1."""
    revision = _read(session, store, kind="shop", also="architectural")
    package = session.get(Package, revision.package_id)
    assert package is not None
    DatabaseStages(store, missing_space=MISSING_SPACE).render_vendor_page_pictures(
        session, revision.id
    )
    client = _client(session, store, package.project_id)

    body = _listing(client, package)
    assert body["combined_set"] is False
    assert [d["side"] for d in body["documents"]] == ["vendor", "architect"]
    assert [d["kind"] for d in body["documents"]] == ["shop", "architectural"]
    shop, architect = body["documents"]
    assert shop["document_version_id"] != architect["document_version_id"]
    pages = {
        str(p.document_version_id): p for p in session.scalars(select(Page)).all()
    }  # one page each
    assert set(pages) == {shop["document_version_id"], architect["document_version_id"]}
    shop_png = client.get(shop["pages"][0]["picture"]["url"]).content
    architect_png = client.get(architect["pages"][0]["picture"]["url"]).content
    assert shop_png.startswith(b"\x89PNG") and architect_png.startswith(b"\x89PNG")
    assert shop_png != architect_png


def test_another_projects_reviewer_and_an_unknown_package_get_404(
    session: Session, store: LocalStore  # noqa: F811
) -> None:
    revision = _read(session, store, kind="shop")
    package = session.get(Package, revision.package_id)
    assert package is not None
    path = f"/api/v1/projects/{package.project_id}/packages/{package.id}/pages"
    assert _client(session, store, uuid4()).get(path).status_code == 404
    client = _client(session, store, package.project_id)
    unknown = f"/api/v1/projects/{package.project_id}/packages/{uuid4()}/pages"
    assert client.get(unknown).status_code == 404
