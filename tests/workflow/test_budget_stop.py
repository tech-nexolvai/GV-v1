"""Through the extraction stage: the AI budget follows the pages read and stops cleanly (#1206).

Verification for the budget wiring in `workflow/stages.py` (`_read_slots`, `_stopped_at_budget`,
`_reader_outcome`), `workflow/unread_pages.budget_reached` and the sign-off block on the pages it
lists. A synthetic three-page vendor file (one page with a countertop row, three times) is read by
fake Claude readers at made-up prices: no network, no model.

The fake readers answer one question at a time, in the order the reader asks them: both readers'
row question for every page, then page by page every dimension, stone and wall question (14
questions a page). A reader that refuses the 19th question for the budget therefore stops the
reading on page 2's first dimension question, after page 1 was read in full.
"""

from __future__ import annotations

import io
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pypdf
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models.runs import ModelInvocation, made_a_call
from app.review.no_countertop import pages_blocking_sign_off
from extraction.slot_reader.anthropic import ProviderAccountRefused, SpendCapExceeded
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_slot_reader import FakeReaders, claude_runtime
from tests.workflow.test_unread_pages import (
    BUILT_IN_PROMPT,
    MISSING_SPACE,
    _headed_page_with_two_rows,
    _revision,
    _SilentOcr,
)
from workflow.ai_budget import AiBudget
from workflow.slot_reader import SlotReaderRuntime
from workflow.slot_row_scope import slot_rows, unchosen_row_pages
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

PRICE = SimpleNamespace(input_per_1k_tokens=Decimal("0.01"), output_per_1k_tokens=Decimal("0.05"))
PAGES = 3
#: Questions per page: two row questions, eight span questions, two stone and two wall questions.
PER_PAGE = 14
#: The questions asked before page 2's first dimension question: every row question, then page 1's.
BEFORE_PAGE_TWO = 2 * PAGES + (PER_PAGE - 2)


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


class PricedRates:
    def rate_for(self, _model_id: str) -> object:
        return PRICE


class StoppingReaders(FakeReaders):
    """Answers like `FakeReaders` until question `refuse_at` (0-based), which is refused with
    `error`, as is every later one; counts the questions sent."""

    def __init__(self, refuse_at: int | None, error: BaseException | None = None) -> None:
        super().__init__(lambda _model, _png: '2"')
        self.refuse_at = refuse_at
        self.error = error or SpendCapExceeded("Claude reader batch spend cap would be exceeded")
        self.sent = 0

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        with self.lock:
            position = self.sent
            self.sent += 1
        if self.refuse_at is not None and position >= self.refuse_at:
            raise self.error
        return super().converse(**kwargs)


def _file(pages: int = PAGES) -> bytes:
    """The vendor page with a countertop row (`test_unread_pages`), `pages` times."""
    one = pypdf.PdfReader(io.BytesIO(_headed_page_with_two_rows()))
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_page(one.pages[0])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _slot(readers: FakeReaders, **changes: Any) -> SlotReaderRuntime:
    slot = claude_runtime(readers, question_packets=True, spend_cap_usd=Decimal(25), **changes)
    # One question at a time, so the order the questions are asked in is the order they are sent.
    return replace(slot, form=replace(slot.form, rates=PricedRates(), max_concurrent_calls=1))


def _stages(store: LocalStore, readers: FakeReaders, **options: Any) -> DatabaseStages:
    slot = _slot(readers)
    return DatabaseStages(
        store,
        dpi=150,
        ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
        missing_space=MISSING_SPACE,
        form_reader=replace(slot.form, prompt=BUILT_IN_PROMPT),
        slot_reader=slot,
        **options,
    )


def _stopped_reading(session: Session, store: LocalStore) -> tuple[Any, StoppingReaders]:
    revision = _revision(session, store, _file(), pages=PAGES)
    session.commit()
    readers = StoppingReaders(refuse_at=BEFORE_PAGE_TWO)

    _stages(store, readers).extract_pages(session, revision.id)
    session.commit()
    return revision, readers


REASON = (
    "AI budget $3.00 reached after page 1 of 3; raise the limit or top up, then run the reading "
    "again"
)


def test_the_budget_stop_lists_every_page_after_the_last_one_read_with_the_budget_named(
    session: Session, store: LocalStore
) -> None:
    """Before #1206 the refused questions became scattered "no reader answer" holds and the
    reading carried on asking. Now the first refusal stops it: page 1 is read in full and checked
    as usual, pages 2 and 3 are listed with the budget named, and nothing else is sent."""
    revision, readers = _stopped_reading(session, store)

    assert readers.sent == BEFORE_PAGE_TWO + 1, "no question was sent after the first refusal"
    assert [row.page_number for row in slot_rows(session, revision.id)] == [1]
    listed = unchosen_row_pages(session, revision.id)
    assert [(page.page_number, page.kind) for page in listed] == [(2, "none"), (3, "none")]
    for page in listed:
        assert page.reason == REASON
        assert page.budget_stopped and page.needs_answer and page.doubtful
        assert "no-row:budget-reached" in page.record.ambiguity_flags
    # The cause is kept on the refused question's record, not dropped as a bare "failed".
    outcomes = list(session.scalars(select(ModelInvocation.outcome).where(made_a_call())))
    assert outcomes.count("budget_refused") == 1
    assert "failed" not in outcomes


def test_the_pages_left_unread_block_sign_off_until_answered(
    session: Session, store: LocalStore
) -> None:
    revision, _readers = _stopped_reading(session, store)

    blocking = pages_blocking_sign_off(session, [revision.id])[revision.id]

    assert blocking.unconfirmed == (2, 3)


def test_running_the_reading_again_reuses_every_answer_and_pays_only_for_the_missing_pages(
    session: Session, store: LocalStore
) -> None:
    """Every question answered before the stop is in the call ledger: the next reading takes those
    answers at no cost (#1112) and asks only what the stop left, after which nothing is listed."""
    revision, _readers = _stopped_reading(session, store)
    again = StoppingReaders(refuse_at=None)

    _stages(store, again).extract_pages(session, revision.id)
    session.commit()

    assert again.sent == PAGES * PER_PAGE - BEFORE_PAGE_TWO
    assert [row.page_number for row in slot_rows(session, revision.id)] == [1, 2, 3]
    assert unchosen_row_pages(session, revision.id) == ()


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        # $1 a page plus $1 for three pages is $4: the Claude cap follows the set's budget.
        ({"ai_budget": AiBudget(Decimal(1), Decimal(1), Decimal(1), Decimal(100))}, Decimal(4)),
        # The defaults: three pages are under the $3 minimum.
        ({}, Decimal(3)),
        # A fixed amount stated for the set: the reading may spend no more than is left of it.
        (
            {
                "ai_budget": AiBudget(Decimal(1), Decimal(1), Decimal(1), Decimal(100)),
                "ai_budget_usd": Decimal("2.50"),
            },
            Decimal("2.50"),
        ),
    ],
)
def test_the_claude_cap_follows_the_drawing_sets_budget(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any],
    expected: Decimal,
) -> None:
    revision = _revision(session, store, _file(), pages=PAGES)
    session.commit()
    caps: list[Decimal | None] = []

    def capture(pages: list[object], *, runtime: SlotReaderRuntime, **_: object) -> tuple[()]:
        caps.append(runtime.spend_cap_usd)
        return ()

    monkeypatch.setattr("workflow.stages.read_slot_pages", capture)

    _stages(store, StoppingReaders(refuse_at=None), **options).extract_pages(session, revision.id)

    assert caps == [expected]


def test_out_of_credit_stops_the_reading_and_sends_nothing_after_it(
    session: Session, store: LocalStore
) -> None:
    """A 402 ends the reading at once: nothing queued behind it is sent, no rate-limit retry, and
    the error reaches the workflow, which names it (`workflow/reader_failures.py`)."""
    revision = _revision(session, store, _file(), pages=PAGES)
    session.commit()
    readers = StoppingReaders(refuse_at=4, error=ProviderAccountRefused(api="OpenRouter"))
    stages = _stages(store, readers)

    with pytest.raises(ProviderAccountRefused):
        stages.extract_pages(session, revision.id)

    assert readers.sent == 5
