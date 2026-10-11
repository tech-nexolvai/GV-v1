"""Every run label a reader builds fits `extraction_runs.config_hash` (#1173).

The column holds 200 characters. The slot reader's label spelled out both reader ids, and with the
Kimi/Qwen pair and a product named it came to just over 200: the save failed, the worker retried
every pass, and the package stayed at UPLOADED. These tests build labels with the configured
readers, with made-up reader ids far longer than any real one, and with an absurd spend cap, and
check every one fits — while the labels that already fit (the Claude pair's) stay exactly as they
were. Invented values only; no model is called.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, String, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models.runs import ExtractionRun
from extraction.slot_reader.walls import WALL_PROMPT_ID
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_slot_reader import KIMI, OPUS, QWEN, SONNET, FakeReaders, runtime
from vocabulary.semantic_types import ProductType
from workflow.slot_reader import SlotReaderRuntime

pytest_plugins = ("tests.app.postgres_fixture",)

#: The column the label is stored in.
_STORED = ExtractionRun.__table__.c.config_hash.type
assert isinstance(_STORED, String) and _STORED.length is not None
COLUMN: int = _STORED.length

#: Made-up reader ids far longer than any real one; they differ only in their last character.
LONG_FIRST = "us.example-vendor.an-invented-reader-whose-name-runs-on-" + "x" * 90 + "-a"
LONG_SECOND = "eu.another-example-vendor.a-second-invented-reader-" + "y" * 90 + "-b"


def _with_readers(base: SlotReaderRuntime, first: str, second: str) -> SlotReaderRuntime:
    return replace(base, form=replace(base.form, reader_ids=(first, second)))


def _heaviest(base: SlotReaderRuntime, product: ProductType | None) -> SlotReaderRuntime:
    """Every readable field at its longest: packets on, stacked on, a spend cap left after spending
    (`_remaining_claude_budget` writes what is left to the micro-dollar)."""
    return replace(
        base,
        product=product,
        allow_stacked=True,
        question_packets=True,
        spend_cap_usd=Decimal("2.487123"),
    )


@pytest.mark.parametrize("product", [None, *ProductType])
def test_the_kimi_qwen_label_fits_whatever_product_is_named(product: ProductType | None) -> None:
    """The bug as found: the configured Kimi/Qwen pair, a product named, the flags on."""
    configured = _heaviest(runtime(FakeReaders(lambda _m, _p: "")), product)
    assert configured.form.reader_ids == (KIMI, QWEN)

    label = configured.run_label(300)

    assert len(label) <= COLUMN
    assert label.startswith("dpi=300;readers=")


@pytest.mark.parametrize("dpi", [150, 300, 1200])
@pytest.mark.parametrize("product", [None, *ProductType])
def test_a_label_with_very_long_reader_ids_fits_and_keeps_them_apart(
    product: ProductType | None, dpi: int
) -> None:
    """Reader ids of any length: the label fits, and two pairs differing in one character differ."""
    base = _heaviest(runtime(FakeReaders(lambda _m, _p: "")), product)
    long = _with_readers(base, LONG_FIRST, LONG_SECOND).run_label(dpi)
    other = _with_readers(base, LONG_FIRST, LONG_SECOND[:-1] + "c").run_label(dpi)

    assert len(long) <= COLUMN
    assert long != other
    assert long.startswith(f"dpi={dpi};readers="), "a short readable head"
    assert LONG_FIRST not in long
    assert ";detail=" in long, "the settings' digest is still there"
    assert ";stacked_agreement=True;question_packets=True;" in long


def test_an_absurd_spend_cap_with_long_ids_still_fits_and_still_counts() -> None:
    """Past every readable form, the label is a digest of all of it: still short, still distinct."""
    base = _with_readers(runtime(FakeReaders(lambda _m, _p: "")), LONG_FIRST, LONG_SECOND)
    absurd = replace(base, spend_cap_usd=Decimal("9" * 150 + ".25")).run_label(300)
    cheaper = replace(base, spend_cap_usd=Decimal("9" * 150 + ".24")).run_label(300)

    assert len(absurd) <= COLUMN
    assert absurd != cheaper
    assert absurd.startswith("dpi=300;readers=")


def _claude(product: ProductType | None, route: str | None, cap: str) -> SlotReaderRuntime:
    claude = replace(
        _heaviest(runtime(FakeReaders(lambda _m, _p: ""), claude_row_reader=True), product),
        allow_stacked=False,
        claude_route=route,
        spend_cap_usd=Decimal(cap),
    )
    assert claude.form.reader_ids == (OPUS, SONNET)
    return claude


@pytest.mark.parametrize("route", [None, "openrouter:google-vertex/global"])
@pytest.mark.parametrize("product", [None, *ProductType])
def test_the_claude_pair_label_is_exactly_as_before(
    product: ProductType | None, route: str | None
) -> None:
    """A label that already fit is not changed: the Claude pair's, with its stated cap, is the same
    text as before #1173, and still `dpi=<n>;` and the runtime's `config_hash`."""
    claude = _claude(product, route, "2.50")
    detail = hashlib.sha256(claude.config_detail.encode()).hexdigest()[:16]
    before = (
        f"dpi=300;readers={OPUS}|{SONNET};prompt={claude.prompt_id}+{WALL_PROMPT_ID};"
        f"stacked_agreement=False;question_packets=True;spend_cap_usd=2.50;detail={detail}"
    )

    assert claude.run_label(300) == before == f"dpi=300;{claude.config_hash}"


@pytest.mark.parametrize("product", [None, *ProductType])
def test_the_claude_pair_label_fits_once_some_of_the_cap_is_spent(
    product: ProductType | None,
) -> None:
    """What is left of the cap is written to the micro-dollar, which took the Claude pair's label
    past 200 too; it now fits, and two amounts left are two runs."""
    spent = _claude(product, None, "2.487123").run_label(300)
    more = _claude(product, None, "2.487122").run_label(300)

    assert len(spent) <= COLUMN
    assert spent != more


def test_the_label_limit_is_the_column() -> None:
    from workflow.form_reader import RUN_LABEL_CHARACTERS
    from workflow.stages import RUN_IDENTITY_CHARACTERS

    assert RUN_LABEL_CHARACTERS == RUN_IDENTITY_CHARACTERS == COLUMN


# ---------------------------------------------------------------------------------------------
# The whole-page form reader's label: the same reader ids, spelled out the same way
# ---------------------------------------------------------------------------------------------


def test_the_form_reader_label_is_unchanged_where_it_fits() -> None:
    from workflow.form_reader import form_reader_run_label

    assert form_reader_run_label(dpi=300, reader_ids=(KIMI, QWEN), prompt_id="form-reader-v5") == (
        f"dpi=300;readers={KIMI}|{QWEN};prompt=form-reader-v5;gv_mark_guard=v1;ink=v1"
    )


def test_the_form_reader_label_fits_with_very_long_reader_ids() -> None:
    from workflow.form_reader import form_reader_run_label

    long = form_reader_run_label(
        dpi=300, reader_ids=(LONG_FIRST, LONG_SECOND), prompt_id="form-reader-v5+product=cabinet"
    )
    other = form_reader_run_label(
        dpi=300,
        reader_ids=(LONG_FIRST, LONG_SECOND[:-1] + "c"),
        prompt_id="form-reader-v5+product=cabinet",
    )
    absurd = form_reader_run_label(
        dpi=300, reader_ids=(LONG_FIRST, LONG_SECOND), prompt_id="form-reader-" + "z" * 300
    )

    assert len(long) <= COLUMN and len(absurd) <= COLUMN
    assert long.startswith("dpi=300;readers=")
    assert "prompt=form-reader-v5+product=cabinet" in long
    assert long != other


# ---------------------------------------------------------------------------------------------
# The fraction-parts route's label names its second reader's model id
# ---------------------------------------------------------------------------------------------


def test_the_fraction_parts_label_fits_with_a_very_long_second_reader(tmp_path: Path) -> None:
    from storage.local import LocalStore
    from tests.extraction.test_reader import MISSING_SPACE
    from tests.workflow.test_fraction_parts_route import (
        ASSOCIATION,
        DRAWING,
        SECOND_EXTRACTOR,
        _Engine,
        _Gate,
    )
    from workflow.stages import DatabaseStages

    store = LocalStore(root=tmp_path, ticket_secret=b"a secret only this test knows")
    gate = _Gate()
    stages = DatabaseStages(
        store,
        association=ASSOCIATION,
        fraction_parts=DRAWING,
        vision_readers=(gate,),
        vision_gate=SECOND_EXTRACTOR,
        missing_space=MISSING_SPACE,
    )
    short = stages._fraction_parts_config_hash(DRAWING, _Engine([]), gate)
    long_gate = _Gate()
    long_gate.config = replace(gate.config, model_id=LONG_FIRST)
    other_gate = _Gate()
    other_gate.config = replace(gate.config, model_id=LONG_FIRST[:-1] + "z")

    long = stages._fraction_parts_config_hash(DRAWING, _Engine([]), long_gate)
    other = stages._fraction_parts_config_hash(DRAWING, _Engine([]), other_gate)

    assert f";second={gate.config.model_id};readers=" in short, "unchanged where it fits"
    assert len(long) <= COLUMN
    assert long != other


# ---------------------------------------------------------------------------------------------
# Saved for real: the run row is written, and a label too long is refused by name
# ---------------------------------------------------------------------------------------------


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


@pytest.mark.parametrize("readers", [(KIMI, QWEN), (LONG_FIRST, LONG_SECOND)])
def test_the_slot_reader_run_row_is_written_with_the_long_readers(
    session: Session, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, readers: tuple[str, str]
) -> None:
    """The stage itself: a countertop set read by the Kimi/Qwen pair (or longer) saves its run."""
    from storage.local import LocalStore
    from tests.extraction.test_reader import MISSING_SPACE
    from tests.workflow.test_markup_route import _SilentOcr
    from tests.workflow.test_slot_reader import _scaffold
    from workflow.stages import DatabaseStages

    revision, _version, _page, run = _scaffold(session)
    slot = _heaviest(runtime(FakeReaders(lambda _m, _p: "")), None)
    slot = _with_readers(slot, *readers)
    stages = DatabaseStages(
        LocalStore(root=tmp_path, ticket_secret=b"a secret only this test knows"),
        dpi=300,
        ocr_engine=_SilentOcr(),
        missing_space=MISSING_SPACE,
        form_reader=slot.form,
        slot_reader=replace(slot, spend_cap_usd=None),
    )
    monkeypatch.setattr("workflow.stages.read_slot_pages", lambda _pages, **_options: ())

    stages._read_slots(
        session,
        package_revision_id=revision.id,
        pages=[],
        task_run_id=run.task_run_id,
        product=ProductType.COUNTERTOP,
    )
    session.flush()

    saved = session.scalars(
        select(ExtractionRun).where(
            ExtractionRun.task_run_id == run.task_run_id, ExtractionRun.id != run.id
        )
    ).one()
    assert saved.config_hash.startswith("dpi=300;readers=")
    assert len(saved.config_hash) <= COLUMN


def test_a_label_too_long_is_refused_by_name_before_the_database_sees_it() -> None:
    """`open_extraction_run` names the field, rather than the database's bare 'value too long'."""
    from uuid import uuid4

    from app.evidence.record import open_extraction_run

    with pytest.raises(ValueError, match=r"config_hash.*201 characters.*200"):
        open_extraction_run(
            object(),  # type: ignore[arg-type]  # never reached: the label is refused first
            task_run_id=uuid4(),
            extractor="extraction.form_reader",
            extractor_version="test",
            config_hash="x" * 201,
        )
