"""A cabinet package is not read by the AI, and says so once (#1205).

Verification for `app/review/cabinet_reading.py` and its gate in the extraction stage
(`workflow/stages.py`): a cabinet package's pages never reach a reader, the skip is recorded as an
extraction run of its own, and the results carry one plain line; a countertop package, and a package
made before products existed, are read exactly as before. Synthetic drawings only; no model is
called (the readers are fakes that record every request).
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import ObservationCandidate, Package, PackageRevision
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from app.review.cabinet_reading import (
    AI_READING_SKIPPED,
    CABINET_LISTS_LINE,
    CABINET_NOT_READ_NOTE,
    ai_reading_skipped,
    ai_reading_used,
    results_note,
)
from rules.schema import Cardinality, Rule
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_slot_reader import FakeReaders
from tests.workflow.test_stages import RULEBOOK
from tests.workflow.test_unread_pages import (
    _headed_page_with_two_rows,
    _revision,
    _stages,
)
from vocabulary.semantic_types import ProductType
from workflow.evidence_operands import position_sensitive_inputs

pytest_plugins = ("tests.app.postgres_fixture",)


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


def _read(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
    product: ProductType | None,
) -> tuple[PackageRevision, FakeReaders, list[object], list[object]]:
    """One headed page with a vendor row on it, for a package of `product`, through the extraction
    stage: what reached the slot reader, the whole-page reader and the fake model clients."""
    revision = _revision(session, store, _headed_page_with_two_rows(), pages=1)
    package = session.get(Package, revision.package_id)
    assert package is not None
    package.product_type = None if product is None else product.value
    session.commit()
    slot_pages: list[object] = []
    whole_pages: list[object] = []

    def slot(pages: list[object], **_options: object) -> tuple[()]:
        slot_pages.extend(pages)
        return ()

    def whole(pages: list[object], **_options: object) -> tuple[()]:
        whole_pages.extend(pages)
        return ()

    monkeypatch.setattr("workflow.stages.read_slot_pages", slot)
    monkeypatch.setattr("workflow.stages.read_form_pages", whole)
    readers = FakeReaders(lambda _model, _png: '2"')
    stages = _stages(readers)
    stages._store = store

    stages.extract_pages(session, revision.id)
    session.commit()
    return revision, readers, slot_pages, whole_pages


def test_a_cabinet_package_makes_no_reader_calls_and_records_the_note(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The point.** The same page a countertop package reads (below) is given to no reader for a
    cabinet package; nothing is listed as "not checked" for countertop reasons; and the skip is on
    the package's record, so its results say it once."""
    revision, readers, slot_pages, whole_pages = _read(
        session, store, monkeypatch, ProductType.CABINET
    )

    assert slot_pages == [] and whole_pages == []
    assert not readers.requests and not readers.row_requests and not readers.wall_requests
    # No row-choice record, so no "Pages not checked" countertop question is ever asked.
    assert (
        session.scalars(
            select(ObservationCandidate).where(
                ObservationCandidate.ambiguity_flags.contains(["slot-reader-row-choice"])
            )
        ).first()
        is None
    )
    (skipped,) = session.scalars(
        select(ExtractionRun)
        .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
        .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .where(
            WorkflowRun.package_revision_id == revision.id,
            ExtractionRun.extractor == AI_READING_SKIPPED,
        )
    ).all()
    assert skipped.config_hash == "product=cabinet"
    assert ai_reading_skipped(session, revision.id)
    assert results_note(session, revision.id, "cabinet") == CABINET_NOT_READ_NOTE


def test_a_redelivered_cabinet_extraction_records_the_skip_once(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision, _readers, _slot, _whole = _read(session, store, monkeypatch, ProductType.CABINET)
    stages = _stages(FakeReaders(lambda _model, _png: '2"'))
    stages._store = store

    stages.extract_pages(session, revision.id)
    session.commit()

    assert (
        len(
            session.scalars(
                select(ExtractionRun).where(ExtractionRun.extractor == AI_READING_SKIPPED)
            ).all()
        )
        == 1
    )


@pytest.mark.parametrize(
    "product", [ProductType.COUNTERTOP, None], ids=["countertop", "no-product"]
)
def test_a_countertop_package_is_read_exactly_as_before(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
    product: ProductType | None,
) -> None:
    """Unchanged: the vendor's row reaches the slot reader, and no skip is recorded."""
    revision, _readers, slot_pages, _whole = _read(session, store, monkeypatch, product)

    assert len(slot_pages) == 1
    assert not ai_reading_skipped(session, revision.id)
    assert results_note(session, revision.id, None if product is None else product.value) is None


def test_a_cabinet_package_read_before_the_change_gets_the_plain_line(
    session: Session, store: LocalStore, tmp_path: Path
) -> None:
    """No skip on record (its drawings were read before #1205): the line does not claim they were
    not read, and only says where the cabinet checks take their values from."""
    from tests.workflow.test_unread_pages import _combined_file

    revision = _revision(session, store, _combined_file([None]), pages=1)
    session.commit()

    assert results_note(session, revision.id, "cabinet") == CABINET_LISTS_LINE
    assert results_note(session, revision.id, "countertop") is None
    assert results_note(session, revision.id, None) is None


def test_only_a_package_whose_checks_include_countertops_is_read() -> None:
    """The reading asks countertop questions, so it runs exactly where countertops are checked
    (`countertops_checked`, the same test sign-off uses): never for cabinets, and never for a
    product added later, which would otherwise be read with countertop questions while its
    countertop pages blocked nothing (#1205 review)."""
    from enum import StrEnum

    from app.review.no_countertop import countertops_checked

    class Later(StrEnum):
        """A stand-in for a product the rulebook does not publish yet."""

        VANITY = "vanity"

    assert not ai_reading_used(ProductType.CABINET)
    assert ai_reading_used(ProductType.COUNTERTOP)
    assert ai_reading_used(None)
    for product in ProductType:
        assert ai_reading_used(product) is countertops_checked(product.value)
    assert not ai_reading_used(Later.VANITY)  # type: ignore[arg-type]
    assert results_note_for_later(Later.VANITY.value) is not None


def results_note_for_later(product: str) -> str | None:
    """A later product's results still say plainly where its checks take their values from."""
    from uuid import uuid4

    class _NoSkipOnRecord:
        def scalar(self, *_args: object) -> bool:
            return False

    return results_note(_NoSkipOnRecord(), uuid4(), product)  # type: ignore[arg-type]


def _rules() -> Iterator[Rule]:
    for path in sorted(RULEBOOK.glob("*.yaml")):
        yield Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def test_no_cabinet_check_can_take_a_vendor_reading() -> None:
    """**Why skipping is safe**, held to the published rulebook: every input a cabinet check reads
    off the vendor's drawing is a run compared in order, which evidence never fills (#794, #833).
    If a cabinet rule ever gains a vendor input a reading could fill, this fails, and the skip must
    be revisited before that rule is published."""
    cabinet = [rule for rule in _rules() if rule.product_type == ProductType.CABINET]
    assert {rule.id for rule in cabinet} == {"CAB-FILLER-001", "CAB-ARCH-VS-SHOP-001"}
    for rule in cabinet:
        withheld = position_sensitive_inputs(rule)
        for name, selector in (rule.inputs or {}).items():
            source = getattr(selector.source, "value", str(selector.source))
            if source == "SHOP":
                assert selector.cardinality is Cardinality.MANY, (rule.id, name)
                assert name in withheld, (rule.id, name)
