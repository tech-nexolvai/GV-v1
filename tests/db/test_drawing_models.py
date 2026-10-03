"""Database contract for views, items, printed identifiers, aliases and parts (#196, C1.6, #852).

The tests that matter are the ones asserting what the schema makes *impossible*. A drawing model
that lets any of these through is one that answers `same_assembly` confidently and wrongly:

* a view identified by its tag alone, merging two elevations from different sheets;
* an item created already corroborated, becoming a second route into the verdict;
* an alias edited in place, silently changing how every past match should have been read;
* a suggested part becoming an item without a person confirming it (#852);
* a reading measuring two parts at once, or a run whose members are not confirmed parts;
* a reading linked to a part by anything but a person's decision, or read by a rule (#913).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.audit.events import AuditEvent
from app.db.base import Base, Immutable, immutable_table_names
from app.db.session import session_factory, unit_of_work
from app.models import (
    Alias,
    CanonicalObservation,
    CountertopRun,
    CountertopRunDecision,
    Document,
    DocumentKind,
    DocumentVersion,
    DrawingItem,
    DrawingView,
    ExtractionRun,
    ItemIdentifier,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageState,
    Page,
    PartConfirmation,
    PartDecision,
    PartProposal,
    Project,
    ReadingPart,
    SourceArtifact,
    TaskRun,
    ViewRole,
    WorkflowRun,
    duplicate_identifiers,
)
from evidence.canonical import Authority
from extraction.model.assembly import ASSEMBLY_MEMBER_TYPES
from rules.semantic_types import DocumentRole, SemanticType
from tests.app.postgres_fixture import alembic_config
from units.measurement import Unit
from verdict.operands import EvidenceStatus
from vocabulary.part_kinds import PartKind
from workflow.parts import (
    CODE_IDENTIFIER_KIND,
    confirm_part,
    current_decision,
    withdraw_part,
)

pytest_plugins = ("tests.app.postgres_fixture",)

DRAWING_TABLES = {"drawing_views", "drawing_items", "item_identifiers", "aliases"}
HASH = "c" * 64
BOX = {"space": "pdf_points", "polygon": [0, 0, 100, 100]}


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
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
    document = Document(package_id=revision.package_id, kind=DocumentKind.SHOP)
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


def test_a_view_role_is_nullable_until_established() -> None:
    """A null role means unknown; the matcher must not treat it as an inferred side."""
    column = Base.metadata.tables["drawing_views"].columns["role"]

    assert column.nullable is True


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


def test_a_view_can_store_a_confirmed_role(postgres_engine: Engine) -> None:
    """The role belongs to the view, so one uploaded sheet can later carry both sides."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        view = _view(session, _page(session), "ID")
        view.role = ViewRole.ARCH.value
    with unit_of_work(factory) as session:
        assert session.scalars(select(DrawingView)).one().role == "arch"


def test_an_unknown_view_role_is_refused(postgres_engine: Engine) -> None:
    """Closed vocabulary: a typo must not become a third side of a comparison."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        view = _view(session, _page(session), "ID")
        view.role = "probably_arch"


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


# ---------------------------------------------------------------------------
# The drawing's parts (#852): a suggestion becomes an item only when a person confirms it
# ---------------------------------------------------------------------------

PART_TABLES = (
    "part_proposals",
    "part_confirmations",
    "countertop_runs",
    "countertop_run_decisions",
    "reading_parts",
)
REPO_ROOT = Path(__file__).resolve().parents[2]
ACTOR = "anant"
STORED_BOX: dict[str, object] = {
    "space": "stored",
    "points": [["0.10", "0.20"], ["0.30", "0.20"], ["0.30", "0.40"], ["0.10", "0.40"]],
}
STORED_LINE: dict[str, object] = {"space": "stored", "points": [["0.10", "0.45"], ["0.30", "0.45"]]}


def _code_reading(session: Session, page: Page, raw_text: str = "B15L") -> ObservationCandidate:
    """A reading of a code, from a real extraction run on the page's own revision."""
    revision = session.scalars(
        select(PackageRevision)
        .join(Document, Document.package_id == PackageRevision.package_id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(DocumentVersion.id == page.document_version_id)
    ).one()
    workflow = WorkflowRun(package_revision_id=revision.id, engine_run_id=f"run-{uuid4()}")
    session.add(workflow)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key=f"extract-{uuid4()}",
        task_type="extract_page",
        attempt=1,
        outcome="ok",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id, extractor="pdfplumber", extractor_version="1.0", config_hash="c"
    )
    session.add(run)
    session.flush()
    reading = ObservationCandidate(
        document_version_id=page.document_version_id,
        page_id=page.id,
        extraction_run_id=run.id,
        raw_text=raw_text,
        polygon=[[10, 10], [20, 10], [20, 20]],
        coordinate_space="image",
        confidence=None,
        ambiguity_flags=[],
    )
    session.add(reading)
    session.flush()
    return reading


def _observation(session: Session, page: Page) -> CanonicalObservation:
    """A width a person read. `HUMAN_CONFIRMED` needs no extractor candidate behind it."""
    value = Fraction(36)
    observation = CanonicalObservation(
        document_version_id=page.document_version_id,
        page_id=page.id,
        document_role=DocumentRole.SHOP,
        polygon=[["0.1", "0.1"], ["0.2", "0.1"], ["0.2", "0.2"]],
        coordinate_space="stored",
        semantic_type=SemanticType.CABINET_WIDTH,
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit=Unit.INCH,
        status=EvidenceStatus.HUMAN_CONFIRMED,
        authority=Authority.AUTHORITATIVE,
        evidence_crop_uri=None,
    )
    session.add(observation)
    session.flush()
    return observation


def _proposal(
    session: Session,
    view: DrawingView,
    *,
    kind: PartKind = PartKind.CABINET,
    code: ObservationCandidate | None = None,
) -> PartProposal:
    proposal = PartProposal(
        drawing_view_id=view.id,
        kind=kind.value,
        extent=STORED_BOX,
        code_as_printed=None if code is None else code.raw_text,
        code_candidate_id=None if code is None else code.id,
        defining_line=STORED_LINE,
        source="dimension-segments",
        source_version="v1",
        reason="the segment between two extension lines on the cabinet dimension chain",
    )
    session.add(proposal)
    session.flush()
    return proposal


def _confirmed(
    session: Session, view: DrawingView, kind: PartKind = PartKind.CABINET
) -> DrawingItem:
    """A part a person confirmed, through the one function that may make its item."""
    confirmation = confirm_part(
        session, proposal=_proposal(session, view, kind=kind), kind=kind, code=None, actor=ACTOR
    )
    item = session.get(DrawingItem, confirmation.drawing_item_id)
    assert item is not None
    return item


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


# -- registration, and what the tables cannot hold — no database needed ------


def test_the_part_tables_are_registered_and_append_only() -> None:
    """Every decision about a part is a record somebody may need to replay, so none is editable."""
    assert set(PART_TABLES) <= set(Base.metadata.tables)
    for model in (
        PartProposal,
        PartConfirmation,
        CountertopRun,
        CountertopRunDecision,
        ReadingPart,
    ):
        assert issubclass(model, Immutable)
    assert set(PART_TABLES) <= set(immutable_table_names())


def test_no_part_table_holds_a_width() -> None:
    """Widths come from readings, never from a code's digits or a box's size. A table with nowhere to
    put a width — no width column, no exact number, no unit — cannot be where one is invented."""
    numeric = ("width", "numerator", "denominator", "unit")
    for table in (*PART_TABLES, "drawing_items", "item_identifiers"):
        columns = set(Base.metadata.tables[table].columns.keys())
        assert not {name for name in columns if any(word in name for word in numeric)}, table


def test_a_part_kind_is_stored_as_the_type_the_run_resolver_reads() -> None:
    """A confirmed cabinet or filler is a member the assembly resolver counts; a countertop is not."""
    assert PartKind.CABINET.item_type in ASSEMBLY_MEMBER_TYPES
    assert PartKind.FILLER.item_type in ASSEMBLY_MEMBER_TYPES
    assert PartKind.COUNTERTOP.item_type not in ASSEMBLY_MEMBER_TYPES


# -- the one writer of items and identifiers ----------------------------------

#: The two tables only a person's confirmation may write, and the models that map them.
ITEM_TABLES = frozenset({"drawing_items", "item_identifiers"})
ITEM_MODELS = frozenset({"DrawingItem", "ItemIdentifier"})
ORM_MODULES = frozenset({"app.models", "app.models.drawing"})


@dataclass(frozen=True)
class _Guarded:
    """Tables only named functions may write, and the models that map them."""

    tables: frozenset[str]
    models: frozenset[str]

    @property
    def raw_insert(self) -> re.Pattern[str]:
        names = "|".join(sorted(self.tables))
        return re.compile(rf"insert\s+into\s+(?:\S+\.)?\"?(?:{names})\b", re.IGNORECASE)


ITEMS = _Guarded(tables=ITEM_TABLES, models=ITEM_MODELS)

#: Where the one writer lives: `(file, function)`.
THE_WRITER = ("workflow/parts.py", "confirm_part")

#: Top-level directories that are not shipped code. Everything else is scanned, so a package added
#: tomorrow is covered the day it lands.
NOT_SOURCE = frozenset({"tests", "frontend", "docs", "node_modules"})

_INSERTING_CALLS = frozenset({"insert", "bulk_insert_mappings"})


def _source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for top in sorted(root.iterdir()):
        if top.name.startswith(".") or top.name in NOT_SOURCE:
            continue
        if top.is_dir():
            files.extend(top.rglob("*.py"))
        elif top.suffix == ".py":
            files.append(top)
    return sorted(files)


def _orm_bindings(tree: ast.Module, guarded: _Guarded) -> tuple[set[str], set[str]]:
    """The names a module binds to the guarded ORM models, and to the modules that define them.

    Resolved from the imports, and from the class statements in the module that declares the models,
    so `extraction.model.items.DrawingItem` — a frozen value type with the same name that writes
    nothing — is never mistaken for the table. The declaring module counts as well: the first version
    of this guard read imports alone, and a hook written beside the models went unseen.
    """
    models: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name in guarded.models
            and any(isinstance(base, ast.Name) and base.id == "Base" for base in node.bases)
        ):
            models.add(node.name)
        elif isinstance(node, ast.ImportFrom) and node.module in ORM_MODULES:
            for alias in node.names:
                if alias.name == "*":
                    models.update(guarded.models)
                elif alias.name in guarded.models:
                    models.add(alias.asname or alias.name)
                elif alias.name == "drawing":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "app":
            modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "models"
            )
        elif isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name for alias in node.names if alias.name in ORM_MODULES
            )
    return models, modules


def _is_model(node: ast.expr, models: set[str], modules: set[str], guarded: _Guarded) -> bool:
    if isinstance(node, ast.Name):
        return node.id in models
    if isinstance(node, ast.Attribute):
        if node.attr == "__table__":
            return _is_model(node.value, models, modules, guarded)
        return node.attr in guarded.models and ast.unparse(node.value) in modules
    return False


def _writes(node: ast.AST, models: set[str], modules: set[str], guarded: _Guarded) -> bool:
    """Whether one node writes a guarded table, in any of the ways this codebase could write one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(guarded.raw_insert.search(node.value))
    if isinstance(node, ast.JoinedStr):
        return bool(guarded.raw_insert.search(ast.unparse(node)))
    if not isinstance(node, ast.Call):
        return False
    callee = node.func
    called = getattr(callee, "id", None) or getattr(callee, "attr", None)
    if _is_model(callee, models, modules, guarded) and called != "__table__":
        return True  # constructing a row
    if called in _INSERTING_CALLS:
        if isinstance(callee, ast.Attribute) and _is_model(callee.value, models, modules, guarded):
            return True  # `DrawingItem.__table__.insert()`
        if any(_is_model(argument, models, modules, guarded) for argument in node.args):
            return True  # `insert(DrawingItem)`, `bulk_insert_mappings(DrawingItem, ...)`
    if called in {"table", "Table"} and node.args:
        first = node.args[0]
        # A table named by string is only ever a way round the models, which every reader uses.
        return isinstance(first, ast.Constant) and first.value in guarded.tables
    return False


def _nodes_by_function(tree: ast.AST, function: str = "<module>") -> Iterator[tuple[str, ast.AST]]:
    for child in ast.iter_child_nodes(tree):
        inner = (
            child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
        )
        yield inner, child
        yield from _nodes_by_function(child, inner)


def _writers(root: Path, guarded: _Guarded = ITEMS) -> list[tuple[str, str, int]]:
    """Every place outside `tests/` that writes a guarded table: by default `drawing_items` or
    `item_identifiers`.

    As `(file, enclosing function, line)`, so the assertion can say exactly where.
    """
    found: list[tuple[str, str, int]] = []
    for path in _source_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        models, modules = _orm_bindings(tree, guarded)
        relative = path.relative_to(root).as_posix()
        for function, node in _nodes_by_function(tree):
            if _writes(node, models, modules, guarded):
                found.append((relative, function, getattr(node, "lineno", 0)))
    return found


def test_only_a_confirmation_writes_items_or_identifiers() -> None:
    """The rule the whole design rests on: a suggestion is not a part.

    Matching reads `drawing_items` today, and the plan on #748 has the countertop checks read it too.
    Anything else able to write a row there could turn a suggestion into a part with no person
    involved, so exactly one function may, and it is the one a person's decision calls.
    """
    writers = _writers(REPO_ROOT)
    elsewhere = [writer for writer in writers if writer[:2] != THE_WRITER]
    assert not elsewhere, (
        "these write drawing_items or item_identifiers outside workflow/parts.py:confirm_part:\n  "
        + "\n  ".join(f"{path}:{line} in {function}" for path, function, line in elsewhere)
    )
    # And the walk does see the one writer that exists. A guard that found nothing anywhere would
    # pass the assertion above while checking nothing.
    assert {writer[:2] for writer in writers} == {THE_WRITER}


#: Every form of writing either table, keyed by the test id. Each is a separate module the guard has
#: to find on its own.
WRITING_FORMS: dict[str, str] = {
    "constructor": (
        "from app.models import DrawingItem\n"
        "def go(session, view):\n"
        "    session.add(DrawingItem(drawing_view_id=view, item_type='x', extent={}))\n"
    ),
    "renamed-constructor": (
        "from app.models.drawing import ItemIdentifier as Code\n"
        "def go(session, item):\n"
        "    session.add(Code(drawing_item_id=item, kind='mark', value_as_printed='B1'))\n"
    ),
    "insert-statement": (
        "from app.models import DrawingItem\n"
        "from sqlalchemy import insert\n"
        "def go(session):\n"
        "    session.execute(insert(DrawingItem).values(item_type='x'))\n"
    ),
    "table-insert-in-a-proposal-hook": (
        "from sqlalchemy import event\n"
        "from app.models import DrawingItem, PartProposal\n"
        "@event.listens_for(PartProposal, 'after_insert')\n"
        "def go(mapper, connection, target):\n"
        "    connection.execute(DrawingItem.__table__.insert().values(item_type='x'))\n"
    ),
    "module-attribute": (
        "import app.models\ndef go(session):\n    session.add(app.models.DrawingItem())\n"
    ),
    "from-app-import-models": (
        "from app import models\ndef go(session):\n    session.add(models.ItemIdentifier())\n"
    ),
    "bulk-insert": (
        "from app.models import DrawingItem\n"
        "def go(session, rows):\n"
        "    session.bulk_insert_mappings(DrawingItem, rows)\n"
    ),
    "raw-sql": (
        "from sqlalchemy import text\n"
        "def go(session):\n"
        "    session.execute(text('INSERT INTO drawing_items (id) VALUES (:id)'))\n"
    ),
    "raw-sql-f-string": (
        "def go(session, schema):\n"
        "    session.execute(text(f'insert into {schema}.item_identifiers (id) values (1)'))\n"
    ),
    "a-hook-beside-the-models": (
        "from sqlalchemy import event\n"
        "class Base: ...\n"
        "class DrawingItem(Base): ...\n"
        "class PartProposal(Base): ...\n"
        "@event.listens_for(PartProposal, 'after_insert')\n"
        "def go(mapper, connection, target):\n"
        "    connection.execute(DrawingItem.__table__.insert().values(item_type='x'))\n"
    ),
    "lightweight-table": (
        "from sqlalchemy import column, table\n"
        "def go(session):\n"
        "    session.execute(table('drawing_items', column('id')).insert())\n"
    ),
}

#: Reads, and the extraction value type that shares a name with the model. None of them writes.
READING_FORMS: dict[str, str] = {
    "the-extraction-value-type": (
        "from extraction.model.items import DrawingItem\n"
        "def go(view, kind, extent):\n"
        "    return DrawingItem(view=view, item_type=kind, extent=extent)\n"
    ),
    "an-orm-read": (
        "from app.models import DrawingItem\n"
        "from sqlalchemy import select\n"
        "def go(session):\n"
        "    return session.scalars(select(DrawingItem)).all()\n"
    ),
    "a-raw-read": (
        "from sqlalchemy import text\n"
        "def go(session):\n"
        "    return session.execute(text('SELECT id FROM drawing_items'))\n"
    ),
}


@pytest.mark.parametrize("source", list(WRITING_FORMS.values()), ids=list(WRITING_FORMS))
def test_the_guard_catches_every_way_of_writing_one(source: str, tmp_path: Path) -> None:
    """Each form, because a guard blind to one of them reports all clear for exactly that one.

    Run against a throwaway tree rather than by planting a file in the repository, which would leave
    it failing its own guard if this test died first.
    """
    (tmp_path / "workflow").mkdir()
    (tmp_path / "workflow" / "elsewhere.py").write_text(source, encoding="utf-8")

    assert [writer[:2] for writer in _writers(tmp_path)] == [("workflow/elsewhere.py", "go")]


@pytest.mark.parametrize("source", list(READING_FORMS.values()), ids=list(READING_FORMS))
def test_the_guard_leaves_readers_alone(source: str, tmp_path: Path) -> None:
    """A guard that flagged every mention would be switched off by the first person it annoyed."""
    (tmp_path / "workflow").mkdir()
    (tmp_path / "workflow" / "reader.py").write_text(source, encoding="utf-8")

    assert _writers(tmp_path) == []


# -- the one writer of a run (#893) -------------------------------------------

#: A confirmed run's members, and the decisions that confirm or withdraw a run.
RUN_ROWS = _Guarded(tables=frozenset({"countertop_runs"}), models=frozenset({"CountertopRun"}))
RUN_DECISIONS = _Guarded(
    tables=frozenset({"countertop_run_decisions"}), models=frozenset({"CountertopRunDecision"})
)

#: Who may write each: a person's confirmation writes a run and its decision; a person's withdrawal
#: writes a decision and no run. Nothing else, and the suggestion least of all.
RUN_WRITERS: dict[_Guarded, set[tuple[str, str]]] = {
    RUN_ROWS: {("workflow/countertop_runs.py", "confirm_countertop_run")},
    RUN_DECISIONS: {
        ("workflow/countertop_runs.py", "confirm_countertop_run"),
        ("workflow/countertop_runs.py", "withdraw_countertop_run"),
    },
}

#: Every form in `WRITING_FORMS`, aimed at the run tables instead of the item tables.
RUN_WRITING_FORMS: dict[str, str] = {
    name: source.replace("DrawingItem", "CountertopRun")
    .replace("ItemIdentifier", "CountertopRunDecision")
    .replace("drawing_items", "countertop_runs")
    .replace("item_identifiers", "countertop_run_decisions")
    for name, source in WRITING_FORMS.items()
}

#: Reading a run, and suggesting one, write nothing.
RUN_READING_FORMS: dict[str, str] = {
    "an-orm-read": (
        "from app.models import CountertopRun\n"
        "from sqlalchemy import select\n"
        "def go(session):\n"
        "    return session.scalars(select(CountertopRun)).all()\n"
    ),
    "the-live-reader": (
        "from workflow.countertop_runs import live_run_rows\n"
        "def go(session):\n"
        "    return session.scalars(live_run_rows()).all()\n"
    ),
    "a-raw-read": (
        "from sqlalchemy import text\n"
        "def go(session):\n"
        "    return session.execute(text('SELECT run_id FROM countertop_run_decisions'))\n"
    ),
}


def test_only_a_persons_decision_writes_a_run() -> None:
    """**Done when, 1.** A suggested run writes no row: only a person's confirmation writes
    `countertop_runs`, and only a confirmation or a withdrawal writes `countertop_run_decisions`.

    The suggestion (`propose_run`) and the page that lists it are absent from both sets, so if
    either ever wrote a row this fails, naming where."""
    for guarded, expected in RUN_WRITERS.items():
        writers = {writer[:2] for writer in _writers(REPO_ROOT, guarded)}
        assert writers == expected, (guarded.tables, sorted(writers))


@pytest.mark.parametrize("source", list(RUN_WRITING_FORMS.values()), ids=list(RUN_WRITING_FORMS))
def test_the_run_guard_catches_every_way_of_writing_one(source: str, tmp_path: Path) -> None:
    (tmp_path / "workflow").mkdir()
    (tmp_path / "workflow" / "elsewhere.py").write_text(source, encoding="utf-8")

    found = [writer[:2] for guarded in RUN_WRITERS for writer in _writers(tmp_path, guarded)]

    assert found == [("workflow/elsewhere.py", "go")]


@pytest.mark.parametrize("source", list(RUN_READING_FORMS.values()), ids=list(RUN_READING_FORMS))
def test_the_run_guard_leaves_readers_alone(source: str, tmp_path: Path) -> None:
    (tmp_path / "workflow").mkdir()
    (tmp_path / "workflow" / "reader.py").write_text(source, encoding="utf-8")

    assert [writer for guarded in RUN_WRITERS for writer in _writers(tmp_path, guarded)] == []


#: The only shipped code that may mention a run at all until a rule reads one (#748 step 7): the
#: models and their migrations, the writer, and the Measure page's listing and endpoints.
RUN_AWARE: frozenset[str] = frozenset(
    {
        "alembic/versions/0058_drawing_parts.py",
        "alembic/versions/0060_countertop_run_decisions.py",
        "app/models/__init__.py",
        "app/models/drawing.py",
        "workflow/countertop_runs.py",
        "app/evidence/countertop_runs.py",
        "app/api/countertop_runs.py",
        "app/main.py",
    }
)

_RUN_WORDS = re.compile(r"countertop_run|CountertopRun|live_run_rows")


def _mentions(root: Path, words: re.Pattern[str]) -> set[str]:
    """Every shipped file whose code, as opposed to its prose, says one of `words`: an import, a
    model, a reader or a table. Docstrings and comments are not code, so they are skipped."""
    found: set[str] = set()
    for path in _source_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        prose = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = "" if id(node) in prose else node.value
            elif isinstance(node, ast.Name):
                text = node.id
            elif isinstance(node, ast.Attribute):
                text = node.attr
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                text = " ".join(
                    [getattr(node, "module", None) or ""]
                    + [f"{alias.name} {alias.asname or ''}" for alias in node.names]
                )
            else:
                continue
            if words.search(text):
                found.add(path.relative_to(root).as_posix())
                break
    return found


def _mentions_a_run(root: Path) -> set[str]:
    return _mentions(root, _RUN_WORDS)


def test_no_rule_input_reads_a_run_yet() -> None:
    """**Done when, 4.** No rule reads runs until step 7, so nothing that builds a rule's inputs —
    `rules/`, `verdict/`, `evidence/`, the evidence stage, matching — may name one. A file that
    starts to is either step 7, which updates this list on purpose, or a leak."""
    assert _mentions_a_run(REPO_ROOT) == RUN_AWARE


def test_the_run_reader_guard_sees_a_reader(tmp_path: Path) -> None:
    """And it is not blind: an import of the reader, in a module about rule inputs, is caught; the
    same words in a docstring are not."""
    (tmp_path / "workflow").mkdir()
    (tmp_path / "workflow" / "operands.py").write_text(
        '"""Mentions countertop_runs in prose only."""\n'
        "from workflow.countertop_runs import live_run_rows as rows\n",
        encoding="utf-8",
    )
    (tmp_path / "workflow" / "prose.py").write_text(
        '"""Mentions countertop_runs in prose only."""\n', encoding="utf-8"
    )

    assert _mentions_a_run(tmp_path) == {"workflow/operands.py"}


# -- the one writer of a reading's link to its part (#913) ----------------------

#: Which reading is each confirmed part's width.
LINK_ROWS = _Guarded(tables=frozenset({"reading_parts"}), models=frozenset({"ReadingPart"}))

#: The one function that constructs a link row, and the only two that may call it: a person
#: confirming a link and a person taking one back. Nothing else, and the suggestion least of all.
LINK_WRITER = ("workflow/reading_parts.py", "_write_link")
LINK_DECISIONS = frozenset(
    {
        ("workflow/reading_parts.py", "confirm_reading_part"),
        ("workflow/reading_parts.py", "withdraw_reading_part"),
    }
)

#: Every form in `WRITING_FORMS`, aimed at the link table instead of the item tables.
LINK_WRITING_FORMS: dict[str, str] = {
    name: source.replace("DrawingItem", "ReadingPart")
    .replace("ItemIdentifier", "ReadingPart")
    .replace("drawing_items", "reading_parts")
    .replace("item_identifiers", "reading_parts")
    for name, source in WRITING_FORMS.items()
}

#: Reading a link, and suggesting one, write nothing.
LINK_READING_FORMS: dict[str, str] = {
    "an-orm-read": (
        "from app.models import ReadingPart\n"
        "from sqlalchemy import select\n"
        "def go(session):\n"
        "    return session.scalars(select(ReadingPart)).all()\n"
    ),
    "the-live-reader": (
        "from workflow.reading_parts import live_reading_parts\n"
        "def go(session):\n"
        "    return session.scalars(live_reading_parts()).all()\n"
    ),
    "the-suggestion": (
        "from workflow.reading_parts import suggest_links\n"
        "def go(parts, readings, tolerance):\n"
        "    return suggest_links(parts, readings, edge_tolerance=tolerance)\n"
    ),
    "a-raw-read": (
        "from sqlalchemy import text\n"
        "def go(session):\n"
        "    return session.execute(text('SELECT drawing_item_id FROM reading_parts'))\n"
    ),
}


def _callers(root: Path, function: str) -> set[tuple[str, str]]:
    """Every `(file, enclosing function)` outside `tests/` that calls `function`, by its name or as
    an attribute, so `reading_parts._write_link(...)` is caught as well as `_write_link(...)`."""
    found: set[tuple[str, str]] = set()
    for path in _source_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for enclosing, node in _nodes_by_function(tree):
            if isinstance(node, ast.Call):
                called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if called == function:
                    found.add((path.relative_to(root).as_posix(), enclosing))
    return found


def test_only_a_persons_decision_writes_a_link() -> None:
    """**Done when, 1.** A suggested link writes nothing: one function constructs a `reading_parts`
    row, and only a person's confirmation or withdrawal calls it.

    The suggestion (`suggest_links`), the listing and the endpoints are absent from both sets, so if
    any of them ever wrote a row, or called the writer, this fails naming where."""
    writers = {writer[:2] for writer in _writers(REPO_ROOT, LINK_ROWS)}
    assert writers == {LINK_WRITER}, sorted(writers)
    assert _callers(REPO_ROOT, LINK_WRITER[1]) == LINK_DECISIONS


@pytest.mark.parametrize("source", list(LINK_WRITING_FORMS.values()), ids=list(LINK_WRITING_FORMS))
def test_the_link_guard_catches_every_way_of_writing_one(source: str, tmp_path: Path) -> None:
    (tmp_path / "workflow").mkdir()
    (tmp_path / "workflow" / "elsewhere.py").write_text(source, encoding="utf-8")

    assert [writer[:2] for writer in _writers(tmp_path, LINK_ROWS)] == [
        ("workflow/elsewhere.py", "go")
    ]


@pytest.mark.parametrize("source", list(LINK_READING_FORMS.values()), ids=list(LINK_READING_FORMS))
def test_the_link_guard_leaves_readers_alone(source: str, tmp_path: Path) -> None:
    (tmp_path / "workflow").mkdir()
    (tmp_path / "workflow" / "reader.py").write_text(source, encoding="utf-8")

    assert _writers(tmp_path, LINK_ROWS) == []
    assert _callers(tmp_path, LINK_WRITER[1]) == set()


@pytest.mark.parametrize(
    "call",
    ["_write_link(session)", "reading_parts._write_link(session)"],
    ids=["by-name", "as-an-attribute"],
)
def test_the_link_guard_sees_a_new_caller_of_the_writer(call: str, tmp_path: Path) -> None:
    """A suggestion that reached the writer would write a link no person decided, without
    constructing a row itself; the caller check is what sees it."""
    (tmp_path / "workflow").mkdir()
    (tmp_path / "workflow" / "suggest.py").write_text(
        "from workflow import reading_parts\n"
        "from workflow.reading_parts import _write_link\n"
        f"def suggest(session):\n    {call}\n",
        encoding="utf-8",
    )

    assert _callers(tmp_path, LINK_WRITER[1]) == {("workflow/suggest.py", "suggest")}


#: The only shipped code that may mention a link at all until a rule reads one (#748 step 7): the
#: models and their migration, the writer, and the Measure page's listing and endpoints.
LINK_AWARE: frozenset[str] = frozenset(
    {
        "alembic/versions/0058_drawing_parts.py",
        "app/models/__init__.py",
        "app/models/drawing.py",
        "workflow/reading_parts.py",
        "app/evidence/reading_parts.py",
        "app/api/reading_parts.py",
        "app/main.py",
    }
)

_LINK_WORDS = re.compile(r"reading_part|ReadingPart")


def test_no_rule_input_reads_a_link_yet() -> None:
    """**Done when, 3.** No rule reads a reading's part until step 7, so nothing that builds a
    rule's inputs — `rules/`, `verdict/`, `evidence/`, the evidence stage, matching — may name a
    link. A file that starts to is either step 7, which updates this list on purpose, or a leak."""
    assert _mentions(REPO_ROOT, _LINK_WORDS) == LINK_AWARE


def test_the_link_reader_guard_sees_a_reader(tmp_path: Path) -> None:
    """And it is not blind: an import of the reader, in a module about rule inputs, is caught; the
    same words in a docstring are not."""
    (tmp_path / "workflow").mkdir()
    (tmp_path / "workflow" / "operands.py").write_text(
        '"""Mentions reading_parts in prose only."""\n'
        "from workflow.reading_parts import live_reading_parts as links\n",
        encoding="utf-8",
    )
    (tmp_path / "workflow" / "prose.py").write_text(
        '"""Mentions reading_parts in prose only."""\n', encoding="utf-8"
    )

    assert _mentions(tmp_path, _LINK_WORDS) == {"workflow/operands.py"}


# -- against a real database --------------------------------------------------


def test_suggesting_a_part_writes_no_item(postgres_engine: Engine) -> None:
    """The proposal path, end to end: a suggestion with a code and the reading it came from is
    stored, and neither table a check reads has gained a row."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        page = _page(session)
        _proposal(session, _view(session, page), code=_code_reading(session, page))
    with unit_of_work(factory) as session:
        assert _count(session, PartProposal) == 1
        assert _count(session, DrawingItem) == 0
        assert _count(session, ItemIdentifier) == 0


def test_confirming_a_part_makes_its_item_and_its_code(postgres_engine: Engine) -> None:
    """The item takes the suggestion's view and extent and the generic type for the confirmed kind;
    the code becomes a `catalogue` identifier exactly as printed; nothing is corroborated."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        page = _page(session)
        view = _view(session, page)
        proposal = _proposal(session, view, code=_code_reading(session, page, "B15L"))
        confirmation = confirm_part(
            session, proposal=proposal, kind=PartKind.CABINET, code="B15L", actor=ACTOR
        )
        confirmation_id, view_id = confirmation.id, view.id

    with unit_of_work(factory) as session:
        confirmation = session.get_one(PartConfirmation, confirmation_id)
        item = session.scalars(select(DrawingItem)).one()
        assert confirmation.drawing_item_id == item.id
        assert confirmation.decision == PartDecision.CONFIRMED.value
        assert (confirmation.kind, confirmation.code_as_printed) == ("cabinet", "B15L")
        assert confirmation.confirmed_by == ACTOR and confirmation.supersedes_id is None
        assert item.drawing_view_id == view_id
        assert item.item_type == SemanticType.CABINET_WIDTH.value
        assert item.extent == STORED_BOX
        assert item.corroborated is False
        identifier = session.scalars(select(ItemIdentifier)).one()
        assert identifier.drawing_item_id == item.id
        assert (identifier.kind, identifier.value_as_printed) == (CODE_IDENTIFIER_KIND, "B15L")
        assert CODE_IDENTIFIER_KIND == "catalogue"
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.target_id == confirmation_id)
        ).one()
        assert (event.actor, event.target_type) == (ACTOR, "part_confirmation")


def test_the_person_s_kind_and_code_are_what_the_item_carries(postgres_engine: Engine) -> None:
    """The suggestion said cabinet with a code; the person said filler with none. The item follows
    the person — a code read by a machine does not reach an identifier unless somebody kept it."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        page = _page(session)
        proposal = _proposal(session, _view(session, page), code=_code_reading(session, page))
        confirm_part(session, proposal=proposal, kind=PartKind.FILLER, code=None, actor=ACTOR)
    with unit_of_work(factory) as session:
        assert session.scalars(select(DrawingItem)).one().item_type == (
            SemanticType.FILLER_WIDTH.value
        )
        assert _count(session, ItemIdentifier) == 0


def test_two_parts_may_share_a_code_and_neither_gets_a_width(postgres_engine: Engine) -> None:
    """A code names a model, not one cabinet, so a repeat is stored and reported, never refused —
    and confirming writes no reading: a width is never decoded out of a code."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        view = _view(session, _page(session))
        for _ in range(2):
            confirm_part(
                session,
                proposal=_proposal(session, view),
                kind=PartKind.CABINET,
                code="B36",
                actor=ACTOR,
            )
    with unit_of_work(factory) as session:
        assert _count(session, DrawingItem) == 2
        reported = session.execute(duplicate_identifiers(CODE_IDENTIFIER_KIND)).all()
        assert [(value, count) for value, count in reported] == [("B36", 2)]
        assert _count(session, CanonicalObservation) == 0
        assert _count(session, ReadingPart) == 0


@pytest.mark.parametrize(
    "code", ["b-15 L", " B15L", "B15L\t"], ids=["mixed", "leading", "trailing"]
)
def test_a_code_is_kept_exactly_as_given(postgres_engine: Engine, code: str) -> None:
    """Never trimmed, re-cased or re-spelled. Matching handles reading variants later, where the
    original is still there to show a reviewer; normalising here would destroy it."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        proposal = _proposal(session, _view(session, _page(session)))
        confirm_part(session, proposal=proposal, kind=PartKind.CABINET, code=code, actor=ACTOR)
    with unit_of_work(factory) as session:
        assert session.scalars(select(ItemIdentifier)).one().value_as_printed == code
        assert session.scalars(select(PartConfirmation)).one().code_as_printed == code


def test_withdrawing_a_suggestion_writes_no_item(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        proposal = _proposal(session, _view(session, _page(session)))
        withdrawal = withdraw_part(session, proposal=proposal, actor=ACTOR)
        assert withdrawal.decision == PartDecision.WITHDRAWN.value
    with unit_of_work(factory) as session:
        assert _count(session, PartConfirmation) == 1
        assert _count(session, DrawingItem) == 0


def test_a_correction_replaces_the_decision_and_makes_a_new_item(postgres_engine: Engine) -> None:
    """Confirm, correct, withdraw: three rows in a chain, the last one current, and each
    confirmation's item its own."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        proposal = _proposal(session, _view(session, _page(session)))
        first = confirm_part(
            session, proposal=proposal, kind=PartKind.CABINET, code=None, actor=ACTOR
        )
        second = confirm_part(
            session, proposal=proposal, kind=PartKind.FILLER, code=None, actor="raj"
        )
        third = withdraw_part(session, proposal=proposal, actor=ACTOR)

        assert second.supersedes_id == first.id
        assert third.supersedes_id == second.id
        assert current_decision(session, proposal) == third
        assert first.drawing_item_id != second.drawing_item_id
        assert _count(session, DrawingItem) == 2


@pytest.mark.parametrize(
    ("shape", "constraint"),
    [
        ({"decision": "confirmed", "kind": "cabinet"}, "part_confirmation_decision_shape"),
        ({"decision": "confirmed", "item": True}, "part_confirmation_decision_shape"),
        ({"decision": "withdrawn", "item": True}, "part_confirmation_decision_shape"),
        ({"decision": "withdrawn", "kind": "cabinet"}, "part_confirmation_decision_shape"),
        ({"decision": "withdrawn", "code": "B36"}, "part_confirmation_decision_shape"),
        (
            {"decision": "confirmed", "kind": "drawer", "item": True},
            "part_confirmation_kind",
        ),
        ({"decision": "maybe", "kind": "cabinet", "item": True}, 'part_confirmation_decision"'),
    ],
    ids=[
        "confirmed-without-its-item",
        "confirmed-without-a-kind",
        "withdrawn-with-an-item",
        "withdrawn-with-a-kind",
        "withdrawn-with-a-code",
        "an-unknown-kind",
        "an-unknown-decision",
    ],
)
def test_a_decision_is_one_thing_or_the_other(
    postgres_engine: Engine, shape: dict[str, object], constraint: str
) -> None:
    """A confirmation names its kind and the item it made; a withdrawal names neither. Inserted
    directly, so it is the database refusing and not `confirm_part` declining to try."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError, match=constraint), unit_of_work(factory) as session:
        view = _view(session, _page(session))
        proposal = _proposal(session, view)
        # Made directly, so the item is named by no other confirmation and only the shape is wrong.
        item = _item(session, view) if shape.get("item") else None
        session.add(
            PartConfirmation(
                part_proposal_id=proposal.id,
                decision=shape["decision"],
                kind=shape.get("kind"),
                code_as_printed=shape.get("code"),
                drawing_item_id=None if item is None else item.id,
                confirmed_by=ACTOR,
            )
        )
        session.flush()


def _link(observation: UUID, item: UUID | None, *, supersedes: UUID | None = None) -> ReadingPart:
    return ReadingPart(
        canonical_observation_id=observation,
        drawing_item_id=item,
        supersedes_id=supersedes,
        signal="the reading sits on the part's dimension line",
        confirmed_by=ACTOR,
    )


def _member(
    countertop: UUID, member: UUID, *, run: UUID, position: int, tolerance: str = "0.002"
) -> CountertopRun:
    return CountertopRun(
        run_id=run,
        countertop_item_id=countertop,
        position=position,
        member_item_id=member,
        signal="beneath the countertop, edge to edge with its neighbour",
        proposal_source="assembly-resolver/v1",
        edge_tolerance=Decimal(tolerance),
        confirmed_by=ACTOR,
    )


def _run_decision(
    countertop: UUID,
    *,
    run: UUID | None,
    supersedes: UUID | None = None,
    decision: PartDecision | None = None,
) -> CountertopRunDecision:
    """A person's decision on a countertop's run, made directly: confirming `run`, or withdrawing
    when `run` is `None`."""
    chosen = decision or (PartDecision.WITHDRAWN if run is None else PartDecision.CONFIRMED)
    return CountertopRunDecision(
        countertop_item_id=countertop,
        supersedes_id=supersedes,
        decision=chosen.value,
        run_id=run,
        confirmed_by=ACTOR,
    )


def _decided(session: Session, countertop: UUID, run: UUID) -> None:
    """The decision a run's rows must belong to, so a test of the rows fails only for its own
    reason."""
    session.add(_run_decision(countertop, run=run))
    session.flush()


def _decision(proposal: PartProposal, *, supersedes: UUID | None = None) -> PartConfirmation:
    return PartConfirmation(
        part_proposal_id=proposal.id,
        supersedes_id=supersedes,
        decision=PartDecision.WITHDRAWN.value,
        confirmed_by=ACTOR,
    )


@pytest.mark.parametrize(
    ("case", "constraint"),
    [
        ("two-first-decisions", "ix_part_confirmations_first_decision"),
        ("one-decision-replaced-twice", "uq_part_confirmations_supersedes_id"),
        ("replacing-another-part-s-decision", "fk_part_confirmations_supersedes_id"),
    ],
)
def test_a_suggestion_has_at_most_one_current_decision(
    postgres_engine: Engine, case: str, constraint: str
) -> None:
    """Two people deciding at once cannot both become current, and a correction cannot end the
    history of a different part."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        view = _view(session, _page(session))
        proposal, other = _proposal(session, view), _proposal(session, view)
        first, others = _decision(proposal), _decision(other)
        session.add_all((first, others))
        session.flush()
        ids = (proposal.id, other.id, first.id, others.id)

    with pytest.raises(IntegrityError, match=constraint), unit_of_work(factory) as session:
        proposal_id, _, first_id, others_id = ids
        proposal = session.get_one(PartProposal, proposal_id)
        if case == "two-first-decisions":
            session.add(_decision(proposal))
        elif case == "one-decision-replaced-twice":
            session.add(_decision(proposal, supersedes=first_id))
            session.flush()
            session.add(_decision(proposal, supersedes=first_id))
        else:
            session.add(_decision(proposal, supersedes=others_id))
        session.flush()


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"code_as_printed": "B36"}, "part_proposal_code_has_reading"),
        ({"code_candidate_id": "reading"}, "part_proposal_code_has_reading"),
        (
            {"code_as_printed": " \t", "code_candidate_id": "reading"},
            "part_proposal_code_not_blank",
        ),
        ({"extent": {"space": "pdf_points", "points": []}}, "part_proposal_extent_stored"),
        ({"extent": {"points": []}}, "part_proposal_extent_stored"),
        ({"defining_line": {"space": "image", "points": []}}, "part_proposal_line_stored"),
        ({"kind": "drawer"}, "part_proposal_kind"),
        ({"source": " "}, "part_proposal_source_not_blank"),
        ({"source_version": ""}, "part_proposal_version_not_blank"),
        ({"reason": "\n"}, "part_proposal_reason_not_blank"),
    ],
    ids=[
        "a-code-with-no-reading",
        "a-reading-with-no-code",
        "a-blank-code",
        "an-extent-in-another-space",
        "an-extent-naming-no-space",
        "a-line-in-another-space",
        "an-unknown-kind",
        "a-blank-source",
        "a-blank-version",
        "a-blank-reason",
    ],
)
def test_a_malformed_suggestion_is_refused(
    postgres_engine: Engine, changes: dict[str, object], constraint: str
) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError, match=constraint), unit_of_work(factory) as session:
        page = _page(session)
        view = _view(session, page)
        values: dict[str, object] = {
            "drawing_view_id": view.id,
            "kind": "cabinet",
            "extent": STORED_BOX,
            "source": "dimension-segments",
            "source_version": "v1",
            "reason": "a segment of the cabinet chain",
            **changes,
        }
        if values.get("code_candidate_id") == "reading":
            values["code_candidate_id"] = _code_reading(session, page).id
        session.add(PartProposal(**values))
        session.flush()


def _one_of_each(session: Session) -> None:
    """One row in each of the four tables, written the way each will be."""
    page = _page(session)
    view = _view(session, page)
    countertop = _confirmed(session, view, PartKind.COUNTERTOP)
    cabinet = _confirmed(session, view)
    run = uuid4()
    _decided(session, countertop.id, run)
    session.add(
        CountertopRun(
            run_id=run,
            countertop_item_id=countertop.id,
            position=0,
            member_item_id=cabinet.id,
            signal="beneath the countertop, reaching both of its ends",
            proposal_source="assembly-resolver/v1",
            edge_tolerance=Decimal("0.002"),
            confirmed_by=ACTOR,
        )
    )
    session.add(
        ReadingPart(
            canonical_observation_id=_observation(session, page).id,
            drawing_item_id=cabinet.id,
            signal="the reading sits on the cabinet's own dimension line",
            confirmed_by=ACTOR,
        )
    )
    session.flush()


@pytest.mark.parametrize("table", PART_TABLES)
@pytest.mark.parametrize(
    "statement",
    ["UPDATE {table} SET created_at = created_at", "DELETE FROM {table}"],
    ids=["update", "delete"],
)
def test_each_part_table_is_append_only(
    postgres_engine: Engine, table: str, statement: str
) -> None:
    """A correction is a new row. Refused by the database for whoever is connected, not by
    convention."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _one_of_each(session)
    with pytest.raises(DBAPIError, match="append-only"), unit_of_work(factory) as session:
        session.execute(text(statement.format(table=table)))


@pytest.mark.parametrize("actor", ["", " ", "\t\n"], ids=["empty", "space", "whitespace"])
@pytest.mark.parametrize(
    "table",
    ["part_confirmations", "countertop_runs", "countertop_run_decisions", "reading_parts"],
)
def test_a_blank_actor_is_refused(postgres_engine: Engine, table: str, actor: str) -> None:
    """Every decision about a part names who made it. A blank name is a decision nobody owns.

    Inserted directly, so it is the database refusing and not `confirm_part` declining to try.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError, match="actor_not_blank"), unit_of_work(factory) as session:
        page = _page(session)
        view = _view(session, page)
        row: PartConfirmation | CountertopRun | CountertopRunDecision | ReadingPart
        if table == "part_confirmations":
            row = _decision(_proposal(session, view))
        elif table == "countertop_runs":
            countertop, run = _confirmed(session, view, PartKind.COUNTERTOP).id, uuid4()
            _decided(session, countertop, run)
            row = _member(countertop, _confirmed(session, view).id, run=run, position=0)
        elif table == "countertop_run_decisions":
            row = _run_decision(_confirmed(session, view, PartKind.COUNTERTOP).id, run=uuid4())
        else:
            row = _link(_observation(session, page).id, _confirmed(session, view).id)
        row.confirmed_by = actor
        session.add(row)
        session.flush()


@pytest.mark.parametrize("actor", ["", "  "])
def test_a_decision_with_no_person_is_refused_before_it_is_written(
    postgres_engine: Engine, actor: str
) -> None:
    """The function says so first, in words, rather than leaving it to a constraint name."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        proposal = _proposal(session, _view(session, _page(session)))
        with pytest.raises(ValueError, match="person"):
            confirm_part(session, proposal=proposal, kind=PartKind.CABINET, code=None, actor=actor)
        with pytest.raises(ValueError, match="person"):
            withdraw_part(session, proposal=proposal, actor=actor)
        assert _count(session, DrawingItem) == 0
        assert _count(session, PartConfirmation) == 0


@pytest.mark.parametrize(
    ("case", "constraint"),
    [
        ("a-second-first-link", "ix_reading_parts_first_link"),
        ("one-link-replaced-twice", "uq_reading_parts_supersedes_id"),
        ("replacing-another-reading-s-link", "fk_reading_parts_supersedes_id"),
        ("withdrawing-a-link-that-never-was", "reading_part_withdraws_a_link"),
    ],
)
def test_a_reading_has_at_most_one_live_link(
    postgres_engine: Engine, case: str, constraint: str
) -> None:
    """Every way a reading could end up measuring two parts at once, refused by the database."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        page = _page(session)
        view = _view(session, page)
        cabinet, filler = _confirmed(session, view), _confirmed(session, view, PartKind.FILLER)
        reading, other_reading = _observation(session, page), _observation(session, page)
        first, others = _link(reading.id, cabinet.id), _link(other_reading.id, cabinet.id)
        session.add_all((first, others))
        session.flush()
        ids = (reading.id, filler.id, first.id, others.id)

    with pytest.raises(IntegrityError, match=constraint), unit_of_work(factory) as session:
        reading_id, filler_id, first_id, others_id = ids
        if case == "a-second-first-link":
            session.add(_link(reading_id, filler_id))
        elif case == "one-link-replaced-twice":
            session.add(_link(reading_id, filler_id, supersedes=first_id))
            session.flush()
            session.add(_link(reading_id, None, supersedes=first_id))
        elif case == "replacing-another-reading-s-link":
            session.add(_link(reading_id, filler_id, supersedes=others_id))
        else:
            session.add(_link(reading_id, None))
        session.flush()


def test_a_corrected_link_leaves_exactly_one_live_link(postgres_engine: Engine) -> None:
    """Link, move, withdraw, link again: one row is replaced by nothing at every step, and it is
    the latest decision."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)

    def unreplaced(session: Session, reading: UUID) -> list[ReadingPart]:
        replaced = select(ReadingPart.supersedes_id).where(ReadingPart.supersedes_id.is_not(None))
        return list(
            session.scalars(
                select(ReadingPart).where(
                    ReadingPart.canonical_observation_id == reading,
                    ReadingPart.id.not_in(replaced),
                )
            )
        )

    with unit_of_work(factory) as session:
        page = _page(session)
        view = _view(session, page)
        cabinet, filler = _confirmed(session, view), _confirmed(session, view, PartKind.FILLER)
        reading = _observation(session, page).id
        previous: UUID | None = None
        for item in (cabinet.id, filler.id, None, cabinet.id):
            link = _link(reading, item, supersedes=previous)
            session.add(link)
            session.flush()
            assert [row.id for row in unreplaced(session, reading)] == [link.id]
            previous = link.id
        assert unreplaced(session, reading)[0].drawing_item_id == cabinet.id


def test_a_reading_links_only_to_a_confirmed_part(postgres_engine: Engine) -> None:
    """A suggestion's id is not an item's. The foreign key is what keeps a reading from measuring a
    part nobody confirmed."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with (
        pytest.raises(IntegrityError, match="fk_reading_parts_drawing_item_id"),
        unit_of_work(factory) as session,
    ):
        page = _page(session)
        proposal = _proposal(session, _view(session, page))
        session.add(_link(_observation(session, page).id, proposal.id))
        session.flush()


def test_a_countertop_run_keeps_its_members_in_order(postgres_engine: Engine) -> None:
    """Position is stored, and the tolerance comes back exactly as it went in."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    run = uuid4()
    with unit_of_work(factory) as session:
        view = _view(session, _page(session))
        countertop = _confirmed(session, view, PartKind.COUNTERTOP)
        _decided(session, countertop.id, run)
        members = [
            _confirmed(session, view, PartKind.FILLER),
            _confirmed(session, view),
            _confirmed(session, view),
        ]
        # Written in reverse, so reading them back in order cannot be insertion order.
        for position, member in reversed(list(enumerate(members))):
            session.add(
                _member(countertop.id, member.id, run=run, position=position, tolerance="0.0025")
            )
        expected = [member.id for member in members]
    with unit_of_work(factory) as session:
        rows = session.scalars(
            select(CountertopRun)
            .where(CountertopRun.run_id == run)
            .order_by(CountertopRun.position)
        ).all()
        assert [row.member_item_id for row in rows] == expected
        assert {row.edge_tolerance for row in rows} == {Decimal("0.0025")}


@pytest.mark.parametrize(
    ("case", "constraint"),
    [
        ("a-suggestion-as-a-member", "fk_countertop_runs_member_item_id"),
        ("two-members-in-one-place", "uq_countertop_runs_slot"),
        ("one-member-twice", "uq_countertop_runs_member"),
        ("the-countertop-as-its-own-member", "countertop_run_member_not_countertop"),
        ("a-negative-position", "countertop_run_position_not_negative"),
    ],
)
def test_a_malformed_run_is_refused(postgres_engine: Engine, case: str, constraint: str) -> None:
    """Members are confirmed parts, each in one place, none of them the countertop itself."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    run = uuid4()
    with pytest.raises(IntegrityError, match=constraint), unit_of_work(factory) as session:
        view = _view(session, _page(session))
        countertop = _confirmed(session, view, PartKind.COUNTERTOP).id
        _decided(session, countertop, run)
        first, second = _confirmed(session, view).id, _confirmed(session, view).id
        rows = {
            "a-suggestion-as-a-member": [_proposal(session, view).id],
            "two-members-in-one-place": [first, second],
            "one-member-twice": [first, first],
            "the-countertop-as-its-own-member": [countertop],
            "a-negative-position": [first],
        }[case]
        for index, member in enumerate(rows):
            position = 0 if case == "two-members-in-one-place" else index
            if case == "a-negative-position":
                position = -1
            session.add(_member(countertop, member, run=run, position=position))
            session.flush()


@pytest.mark.parametrize("tolerance", ["NaN", "Infinity", "-Infinity", "-0.001"])
def test_an_edge_tolerance_that_removes_the_checks_is_refused(
    postgres_engine: Engine, tolerance: str
) -> None:
    """NaN and Infinity do not loosen the resolver's gap, overlap and reach checks — they remove
    them, and `resolve_assembly` refuses them for that reason. PostgreSQL orders NaN above every
    number, so the database has to refuse it with an upper bound, not only `>= 0`."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with (
        pytest.raises(IntegrityError, match="countertop_run_tolerance_finite"),
        unit_of_work(factory) as session,
    ):
        view = _view(session, _page(session))
        countertop, run = _confirmed(session, view, PartKind.COUNTERTOP).id, uuid4()
        _decided(session, countertop, run)
        member = _confirmed(session, view).id
        session.add(_member(countertop, member, run=run, position=0, tolerance=tolerance))
        session.flush()


# -- a person's decision on a run (#893) ----------------------------------------


@pytest.mark.parametrize(
    ("case", "constraint"),
    [
        ("two-first-decisions", "ix_countertop_run_decisions_first_decision"),
        ("one-decision-replaced-twice", "uq_countertop_run_decisions_supersedes_id"),
        ("replacing-another-countertop-s-decision", "fk_countertop_run_decisions_supersedes_id"),
    ],
)
def test_a_countertop_has_at_most_one_current_run_decision(
    postgres_engine: Engine, case: str, constraint: str
) -> None:
    """Two people deciding at once cannot both become current, and a correction cannot end the
    history of another countertop's run. Inserted directly, so it is the database refusing."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        view = _view(session, _page(session))
        countertop = _confirmed(session, view, PartKind.COUNTERTOP).id
        other = _confirmed(session, view, PartKind.COUNTERTOP).id
        first, others = _run_decision(countertop, run=uuid4()), _run_decision(other, run=None)
        session.add_all((first, others))
        session.flush()
        ids = (countertop, first.id, others.id)

    with pytest.raises(IntegrityError, match=constraint), unit_of_work(factory) as session:
        countertop, first_id, others_id = ids
        if case == "two-first-decisions":
            session.add(_run_decision(countertop, run=None))
        elif case == "one-decision-replaced-twice":
            session.add(_run_decision(countertop, run=None, supersedes=first_id))
            session.flush()
            session.add(_run_decision(countertop, run=uuid4(), supersedes=first_id))
        else:
            session.add(_run_decision(countertop, run=None, supersedes=others_id))
        session.flush()


@pytest.mark.parametrize(
    ("case", "constraint"),
    [
        ("a-confirmation-naming-no-run", "run_decision_shape"),
        ("a-withdrawal-naming-a-run", "run_decision_shape"),
        ("an-unknown-decision", "run_decision_value"),
        ("one-run-confirmed-twice", "uq_countertop_run_decisions_run_id"),
    ],
)
def test_a_run_decision_is_one_thing_or_the_other(
    postgres_engine: Engine, case: str, constraint: str
) -> None:
    """A confirmation names the run it confirmed; a withdrawal names none; and a run belongs to one
    decision only, so one confirmation can never be read as two."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError, match=constraint), unit_of_work(factory) as session:
        view = _view(session, _page(session))
        countertop = _confirmed(session, view, PartKind.COUNTERTOP).id
        if case == "a-confirmation-naming-no-run":
            row = _run_decision(countertop, run=None, decision=PartDecision.CONFIRMED)
        elif case == "a-withdrawal-naming-a-run":
            row = _run_decision(countertop, run=uuid4(), decision=PartDecision.WITHDRAWN)
        elif case == "an-unknown-decision":
            # Naming no run, so only the decision's value is wrong.
            row = _run_decision(countertop, run=None)
            row.decision = "probably"
        else:
            run = uuid4()
            other = _confirmed(session, view, PartKind.COUNTERTOP).id
            session.add(_run_decision(countertop, run=run))
            session.flush()
            row = _run_decision(other, run=run)
        session.add(row)
        session.flush()


@pytest.mark.parametrize("case", ["no-decision", "another-countertop-s-decision"])
def test_a_run_row_belongs_to_a_decision_about_its_own_countertop(
    postgres_engine: Engine, case: str
) -> None:
    """A member row with no person's decision behind it, or filed under a decision about another
    countertop, is refused: the database, not only the writer, says a run is a person's."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with (
        pytest.raises(IntegrityError, match="fk_countertop_runs_run_id"),
        unit_of_work(factory) as session,
    ):
        view = _view(session, _page(session))
        countertop = _confirmed(session, view, PartKind.COUNTERTOP).id
        run = uuid4()
        if case == "another-countertop-s-decision":
            _decided(session, _confirmed(session, view, PartKind.COUNTERTOP).id, run)
        session.add(_member(countertop, _confirmed(session, view).id, run=run, position=0))
        session.flush()
