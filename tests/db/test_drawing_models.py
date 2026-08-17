"""Database contract for views, items, printed identifiers and aliases (#196, C1.6).

The tests that matter are the ones asserting what the schema makes *impossible*. A drawing model
that lets any of these through is one that answers `same_assembly` confidently and wrongly:

* a view identified by its tag alone, merging two elevations from different sheets;
* an item created already corroborated, becoming a second route into the verdict;
* an alias edited in place, silently changing how every past match should have been read.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base, Immutable
from app.db.session import session_factory, unit_of_work
from app.models import (
    Alias,
    Document,
    DocumentKind,
    DocumentVersion,
    DrawingItem,
    DrawingView,
    ItemIdentifier,
    Package,
    PackageRevision,
    PackageState,
    Page,
    Project,
    SourceArtifact,
    duplicate_identifiers,
)

pytest_plugins = ("tests.app.postgres_fixture",)

DRAWING_TABLES = {"drawing_views", "drawing_items", "item_identifiers", "aliases"}
HASH = "c" * 64
BOX = {"space": "pdf_points", "polygon": [0, 0, 100, 100]}


def _upgrade(engine: Engine) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


def _page(session: Session, *, index: int = 0) -> Page:
    """The aggregate a view hangs from. Staged flushes order the inserts: these models use plain
    ForeignKey columns rather than ORM relationships, so SQLAlchemy has no dependency graph to sort
    a single `add_all` by."""
    project = Project(name=f"GV Drawing Test {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    artifact = SourceArtifact(
        storage_key=f"originals/{project.id}/d.pdf", sha256=HASH, size=1, backend_version_id=None
    )
    document = Document(package_revision_id=revision.id, kind=DocumentKind.SHOP)
    session.add_all((artifact, document))
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=HASH, page_count=2
    )
    session.add(version)
    session.flush()
    page = Page(
        document_version_id=version.id,
        index=index,
        content_hash=HASH,
        width_pt=612,
        height_pt=792,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        sheet_number=f"A-10{index}",
        page_type=None,
        revision_label=None,
    )
    session.add(page)
    session.flush()
    return page


def _view(session: Session, page: Page, tag: str = "D") -> DrawingView:
    view = DrawingView(page_id=page.id, tag=tag, region=BOX)
    session.add(view)
    session.flush()
    return view


def _item(session: Session, view: DrawingView, item_type: str = "CT001") -> DrawingItem:
    item = DrawingItem(drawing_view_id=view.id, item_type=item_type, extent=BOX)
    session.add(item)
    session.flush()
    return item


# ---------------------------------------------------------------------------
# Registration and marker mixins — no database needed
# ---------------------------------------------------------------------------


def test_all_four_tables_are_registered() -> None:
    assert DRAWING_TABLES <= set(Base.metadata.tables)


def test_an_alias_is_immutable_and_the_rest_are_not() -> None:
    """An alias changes what matches what, which makes it a small rule — editing one in place would
    silently change how every past match should have been read. Views and items are corrected as a
    package is re-read, so they are not."""
    assert issubclass(Alias, Immutable)
    for model in (DrawingView, DrawingItem, ItemIdentifier):
        assert not issubclass(model, Immutable)


def test_the_stored_default_for_corroborated_is_false() -> None:
    """The default is the control: an item read off a drawing is AI output, and one that could be
    created corroborated would be a second route into the verdict that bypasses the evidence gate.

    Asserted on the column rather than on a fresh instance. `mapped_column(default=False)` is a
    *column* default applied at INSERT, so an unflushed object reads `None` — falsy, so the gate
    still holds, but not `False`, and a test claiming otherwise would be asserting something the ORM
    does not promise. `test_an_item_is_stored_uncorroborated` covers the value that actually lands.
    """
    column = Base.metadata.tables["drawing_items"].columns["corroborated"]
    assert column.default is not None and column.default.arg is False
    assert column.nullable is False


def test_cross_view_identity_is_not_representable() -> None:
    """B7.3's job, deliberately absent here. A nullable "same as" column would invite somebody to
    guess that the item in elevation D and the item in plan E are the same cabinet."""
    columns = set(Base.metadata.tables["drawing_items"].columns.keys())
    assert not {"same_as_id", "assembly_id", "physical_item_id"} & columns


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


def test_one_tag_may_repeat_across_pages(postgres_engine: Engine) -> None:
    """Sheets reuse D, E, F page after page. Identity is the pair, so this is ordinary."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        first = _page(session, index=0)
        second = _page(session, index=1)
        _view(session, first, "D")
        _view(session, second, "D")
    with unit_of_work(factory) as session:
        assert session.scalars(select(DrawingView)).all().__len__() == 2


def test_the_same_tag_twice_on_one_page_is_refused(postgres_engine: Engine) -> None:
    """The failure this constraint exists for. Two views sharing (page, tag) would merge, and every
    item beneath them would belong to the wrong drawing."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        page = _page(session)
        _view(session, page, "D")
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        page = session.scalars(select(Page)).first()
        assert page is not None
        _view(session, page, "D")


def test_an_item_may_have_no_identifier(postgres_engine: Engine) -> None:
    """Plenty of fillers carry nothing printed. Requiring one would force somebody to invent a value,
    and an invented identifier is worse than an absent one because it matches."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _item(session, _view(session, _page(session)))
    with unit_of_work(factory) as session:
        item = session.scalars(select(DrawingItem)).one()
        assert (
            session.scalars(
                select(ItemIdentifier).where(ItemIdentifier.drawing_item_id == item.id)
            ).all()
            == []
        )


def test_an_item_may_carry_several_identifiers(postgres_engine: Engine) -> None:
    """A cabinet often has both a vendor code and a mark, and they disagree often enough that
    keeping only one would lose the disagreement."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        item = _item(session, _view(session, _page(session)))
        session.add_all(
            (
                ItemIdentifier(
                    drawing_item_id=item.id, kind="vendor_unique", value_as_printed="B24"
                ),
                ItemIdentifier(drawing_item_id=item.id, kind="mark", value_as_printed="C-3"),
            )
        )
    with unit_of_work(factory) as session:
        assert {row.kind for row in session.scalars(select(ItemIdentifier))} == {
            "vendor_unique",
            "mark",
        }


def test_a_repeated_vendor_identifier_is_stored_and_reported(postgres_engine: Engine) -> None:
    """Not refused. Real packages reuse marks, and a unique constraint would refuse the drawing
    rather than the ambiguity — the drawing is the fact, and the correct response is to show a
    reviewer so they decide which item the rule is about."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        view = _view(session, _page(session))
        for _ in range(2):
            item = _item(session, view)
            session.add(
                ItemIdentifier(
                    drawing_item_id=item.id, kind="vendor_unique", value_as_printed="B24"
                )
            )
        session.add(
            ItemIdentifier(
                drawing_item_id=_item(session, view).id,
                kind="vendor_unique",
                value_as_printed="B30",
            )
        )
    with unit_of_work(factory) as session:
        reported = session.execute(duplicate_identifiers()).all()
        assert [(value, count) for value, count in reported] == [("B24", 2)]


def test_an_alias_carries_who_added_it_and_why(postgres_engine: Engine) -> None:
    """An alias with no author is an anonymous rule change, and one with no rationale is a rule
    nobody can review."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        session.add(
            Alias(
                spelling="Cab.",
                canonical_term="cabinet",
                added_by="anant",
                rationale="seen on three Ridgewood packages",
                rulebook_version="1.0.0",
            )
        )
    with unit_of_work(factory) as session:
        alias = session.scalars(select(Alias)).one()
        assert alias.added_by == "anant" and "Ridgewood" in alias.rationale


def test_the_same_alias_is_versioned_rather_than_replaced(postgres_engine: Engine) -> None:
    """One spelling may map to one term once per rulebook version. A second row at a new version is
    how the table changes; editing the first in place is what `Immutable` prevents."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)

    def alias(version: str) -> Alias:
        return Alias(
            spelling="Cab.",
            canonical_term="cabinet",
            added_by="anant",
            rationale="seen on three Ridgewood packages",
            rulebook_version=version,
        )

    with unit_of_work(factory) as session:
        session.add(alias("1.0.0"))
    with unit_of_work(factory) as session:
        session.add(alias("1.1.0"))
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.add(alias("1.0.0"))

    with unit_of_work(factory) as session:
        assert {row.rulebook_version for row in session.scalars(select(Alias))} == {
            "1.0.0",
            "1.1.0",
        }


def test_a_view_cannot_be_deleted_while_an_item_references_it(postgres_engine: Engine) -> None:
    """RESTRICT, not cascade. Removing a view and silently taking its items would erase the record a
    finding cites."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _item(session, _view(session, _page(session)))
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.delete(session.scalars(select(DrawingView)).one())


def test_an_item_belongs_to_a_view_that_exists(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.add(DrawingItem(drawing_view_id=uuid4(), item_type="CT001", extent=BOX))


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (DrawingView, {"tag": ""}),
        (DrawingItem, {"item_type": ""}),
    ],
)
def test_an_empty_required_string_is_refused(
    postgres_engine: Engine, model: type, kwargs: dict[str, str]
) -> None:
    """An empty tag identifies nothing; an empty item type is outside the vocabulary by definition."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        page = _page(session)
        if model is DrawingView:
            session.add(DrawingView(page_id=page.id, region=BOX, **kwargs))
        else:
            session.add(DrawingItem(drawing_view_id=_view(session, page).id, extent=BOX, **kwargs))


def test_an_item_is_stored_uncorroborated(postgres_engine: Engine) -> None:
    """The value that actually lands. Nothing in this module sets it True; promotion is the evidence
    layer's job, under the same discipline that governs observations."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _item(session, _view(session, _page(session)))
    with unit_of_work(factory) as session:
        assert session.scalars(select(DrawingItem)).one().corroborated is False
