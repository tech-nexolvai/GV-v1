"""Through the extraction stage: the AI budget follows the pages read and stops cleanly (#1206).

Verification for the budget wiring in `workflow/stages.py` (`_read_slots`, `_stopped_at_budget`,
`_reader_outcome`), `workflow/unread_pages.budget_reached` and the sign-off block on the pages it
lists. A synthetic three-page vendor file (one page with a countertop row, three times) is read by
fake Claude readers at made-up prices: no network, no model.

The fake readers answer one question at a time, in the order the reader asks them: both readers'
row question for every page, then page by page every dimension, stone and wall question (14
questions a page). A reader that refuses the 19th question for the budget therefore stops the
reading on page 2's first dimension question, after page 1 was read in full.

Where the review of #1209 found that a fake hid a bug, the real path is used: the real spend guard
at made-up prices, the stage's own "Read the unread pages", and an architect file.
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
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun, made_a_call
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
from vocabulary.reviewer_reasons import REVIEWER_REASONS, reviewer_reason
from workflow.ai_budget import AiBudget
from workflow.slot_reader import SlotReaderRuntime
from workflow.slot_row_scope import budget_stopped_pages, slot_rows, unchosen_row_pages
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
    def __init__(self, price: object = PRICE) -> None:
        self.price = price

    def rate_for(self, _model_id: str) -> object:
        return self.price


#: Made-up prices for the real spend guard: output only, $0.05 per 1,000 tokens.
OUTPUT_PRICE = SimpleNamespace(input_per_1k_tokens=Decimal(0), output_per_1k_tokens=Decimal("0.05"))


class StoppingReaders(FakeReaders):
    """Answers like `FakeReaders` until question `refuse_at` (0-based), which is refused with
    `error`, as is every later one (or, with `refuse_when`, every question whose text has it);
    counts the questions sent. `output_tokens` is what each answer reports it used."""

    def __init__(
        self,
        refuse_at: int | None,
        error: BaseException | None = None,
        *,
        refuse_when: str | None = None,
        output_tokens: int | None = None,
    ) -> None:
        super().__init__(lambda _model, _png: '2"')
        self.refuse_at = refuse_at
        self.refuse_when = refuse_when
        self.output_tokens = output_tokens
        self.error = error or SpendCapExceeded("Claude reader batch spend cap would be exceeded")
        self.sent = 0

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        with self.lock:
            position = self.sent
            self.sent += 1
        if self.refuse_at is not None and position >= self.refuse_at:
            raise self.error
        if self.refuse_when is not None and _asks(kwargs, self.refuse_when):
            raise self.error
        reply = dict(super().converse(**kwargs))
        if self.output_tokens is not None:
            reply["usage"] = {"inputTokens": 0, "outputTokens": self.output_tokens}
        return reply


def _asks(request: Mapping[str, Any], words: str) -> bool:
    content = request["messages"][0]["content"]
    return any(words in part.get("text", "") for part in content)


#: The words of the stone-end question (`FakeReaders`): asked after a page's dimensions.
STONE_ENDS = "hold-only safety question"


def _file(pages: int = PAGES) -> bytes:
    """The vendor page with a countertop row (`test_unread_pages`), `pages` times."""
    one = pypdf.PdfReader(io.BytesIO(_headed_page_with_two_rows()))
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_page(one.pages[0])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _slot(
    readers: FakeReaders, *, rates: object | None = None, **changes: Any
) -> SlotReaderRuntime:
    slot = claude_runtime(readers, question_packets=True, spend_cap_usd=Decimal(25), **changes)
    # One question at a time, so the order the questions are asked in is the order they are sent.
    return replace(
        slot,
        form=replace(slot.form, rates=rates or PricedRates(), max_concurrent_calls=1),
    )


def _stages(
    store: LocalStore, readers: FakeReaders, *, rates: object | None = None, **options: Any
) -> DatabaseStages:
    slot = _slot(readers, rates=rates)
    return DatabaseStages(
        store,
        dpi=150,
        ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
        missing_space=MISSING_SPACE,
        form_reader=replace(slot.form, prompt=BUILT_IN_PROMPT),
        slot_reader=slot,
        **options,
    )


def _stopped_reading(
    session: Session, store: LocalStore, readers: StoppingReaders | None = None
) -> tuple[Any, StoppingReaders]:
    revision = _revision(session, store, _file(), pages=PAGES)
    session.commit()
    readers = readers or StoppingReaders(refuse_at=BEFORE_PAGE_TWO)

    _stages(store, readers).extract_pages(session, revision.id)
    session.commit()
    return revision, readers


def _earlier_call(session: Session, revision_id: Any, cost_micros: int | None) -> None:
    """A call the reading's stage already made (an earlier route), at `cost_micros` (`None`: no
    price was stated for its model)."""
    task = session.scalars(
        select(TaskRun)
        .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .where(WorkflowRun.package_revision_id == revision_id, TaskRun.task_type == "extract_pages")
    ).one()
    run = ExtractionRun(
        task_run_id=task.id, extractor="earlier-route", extractor_version="v1", config_hash="x"
    )
    session.add(run)
    session.flush()
    session.add(
        ModelInvocation(
            extraction_run_id=run.id,
            model_id="synthetic.model",
            prompt_id="p",
            template_id="t",
            input_tokens=10,
            output_tokens=10,
            cost_micros=cost_micros,
            latency_ms=1,
            outcome="ok",
        )
    )
    session.commit()


REASON = (
    "AI budget $3.00 reached after page 1 of 3. Use 'Read the unread pages', or ask an admin to "
    "raise the limit."
)


def test_the_budget_stop_lists_every_page_missing_its_own_reading_with_the_budget_named(
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
        assert "top up" not in page.reason
        assert page.budget_stopped and page.needs_answer and page.doubtful
        assert "no-row:budget-reached" in page.record.ambiguity_flags
    # The cause is kept on the refused question's record, not dropped as a bare "failed".
    outcomes = list(session.scalars(select(ModelInvocation.outcome).where(made_a_call())))
    assert outcomes.count("budget_refused") == 1
    assert "failed" not in outcomes


def test_the_amount_named_is_the_drawing_sets_budget_not_what_was_left_of_it(
    session: Session, store: LocalStore
) -> None:
    """#1209 review: with $1 already spent on the set, the Claude readers had $2 left, but the
    budget the person can act on is the set's $3."""
    revision = _revision(session, store, _file(), pages=PAGES)
    session.commit()
    _earlier_call(session, revision.id, 1_000_000)

    _stages(store, StoppingReaders(refuse_at=BEFORE_PAGE_TWO)).extract_pages(session, revision.id)
    session.commit()

    assert {page.reason for page in unchosen_row_pages(session, revision.id)} == {REASON}


def test_a_call_with_no_price_is_named_as_such_not_as_a_budget(
    session: Session, store: LocalStore
) -> None:
    """A call with no stated price means no cost can be counted, so no more calls are made: the
    reason says so and sends the admin to the model rates, never to an amount of $0.00."""
    revision = _revision(session, store, _file(), pages=PAGES)
    session.commit()
    _earlier_call(session, revision.id, None)
    readers = StoppingReaders(refuse_at=None)

    _stages(store, readers).extract_pages(session, revision.id)
    session.commit()

    assert readers.sent == 0
    reasons = {page.reason for page in unchosen_row_pages(session, revision.id)}
    assert reasons == {
        (
            "AI reading stopped before page 1 of 3 was read: a call had no price, so no more "
            "calls were made; check the model rates."
        )
    }


def test_the_pages_left_unread_block_sign_off_until_answered(
    session: Session, store: LocalStore
) -> None:
    revision, _readers = _stopped_reading(session, store)

    blocking = pages_blocking_sign_off(session, [revision.id])[revision.id]

    assert blocking.unconfirmed == (2, 3)


def test_a_page_whose_own_reading_finished_is_kept_and_held_for_what_was_not_asked(
    session: Session, store: LocalStore
) -> None:
    """#1209 review: page 1's row and dimensions are answered, then its stone-end question is
    refused. Page 1 is read, so it is kept, held for the person with the budget named; only pages
    2 and 3, whose own reading never happened, are listed."""
    revision, _readers = _stopped_reading(session, store, StoppingReaders(refuse_at=2 * PAGES + 8))

    rows = slot_rows(session, revision.id)
    assert [row.page_number for row in rows] == [1]
    flags = {flag for candidate in rows[0].candidates for flag in candidate.ambiguity_flags or ()}
    assert "row-hold:budget-not-asked" in flags
    assert reviewer_reason(flags, None) == REVIEWER_REASONS["budget-not-asked"]
    assert REVIEWER_REASONS["budget-not-asked"].startswith(
        "Not asked: the AI budget was reached before this page's later questions"
    )
    assert [page.page_number for page in unchosen_row_pages(session, revision.id)] == [2, 3]
    assert budget_stopped_pages(session, revision.id) == (1, 2, 3)


def test_with_an_architect_file_a_stop_in_the_dimensions_lists_no_page_that_was_read(
    session: Session, store: LocalStore
) -> None:
    """#1209 review: the architect match and pairing questions come in later batches. A stop in
    the dimensions batch (the vendor page's stone-end question) left every architect question
    unasked, and the page was listed as unread although its row and dimensions were read. Now it
    is kept, held with the budget named."""
    from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
    from tests.workflow.test_architect_file_reader import _add_architectural
    from tests.workflow.test_architect_reader import _upload
    from tests.workflow.test_separate_architect_production_chain import (
        TEXTS,
        VENDOR,
        Readers,
        _architect_file,
    )
    from tests.workflow.test_slot_reader import claude_crops_to_texts

    class Stopping(Readers):
        def converse(self, **kwargs: Any) -> Mapping[str, Any]:
            if _asks(kwargs, STONE_ENDS):
                raise SpendCapExceeded("Claude reader batch spend cap would be exceeded")
            return super().converse(**kwargs)

    revision = _upload(session, store, VENDOR)
    _add_architectural(session, store, revision, _architect_file())
    readers = Stopping()
    stages = _stages(store, readers, architect_reader=MEASURED_ARCHITECT_SETTINGS)
    original = stages._slot_page

    def slot_page(*args: Any, **kwargs: Any) -> Any:
        page = original(*args, **kwargs)
        if not isinstance(page, str):
            readers.lookup = claude_crops_to_texts(page, TEXTS)
        return page

    stages._slot_page = slot_page  # type: ignore[method-assign]

    stages.extract_pages(session, revision.id)
    session.commit()

    rows = slot_rows(session, revision.id)
    assert [row.page_number for row in rows] == [1], "the vendor page was read and is kept"
    flags = {flag for candidate in rows[0].candidates for flag in candidate.ambiguity_flags or ()}
    assert "row-hold:budget-not-asked" in flags
    assert unchosen_row_pages(session, revision.id) == ()


def test_read_the_unread_pages_reads_them_with_a_fresh_budget_and_pays_only_for_what_is_missing(
    session: Session, store: LocalStore
) -> None:
    """The review's probe, as a test, on the real path: the real spend guard at made-up prices
    ($0.10 an answer) stops the first reading at its $3 budget after page 2. "Read the unread
    pages" then gives page 3 its own budget on top of what was spent, reuses all 30 answers
    already paid for, and asks only page 3's 12 dimension questions. Before, a second reading sent
    nothing and said "$0.00 reached"."""
    rates = PricedRates(OUTPUT_PRICE)
    revision = _revision(session, store, _file(), pages=PAGES)
    session.commit()
    first = StoppingReaders(refuse_at=None, output_tokens=2000)

    _stages(store, first, rates=rates).extract_pages(session, revision.id)
    session.commit()

    # 30 answers at $0.10 reach $3.00; the 31st is refused by the guard before it is sent.
    assert first.sent == 30
    outcomes = list(session.scalars(select(ModelInvocation.outcome).where(made_a_call())))
    assert outcomes.count("budget_refused") == 1
    assert [row.page_number for row in slot_rows(session, revision.id)] == [1, 2]
    assert [
        (page.page_number, page.reason) for page in unchosen_row_pages(session, revision.id)
    ] == [
        (
            3,
            (
                "AI budget $3.00 reached after page 2 of 3. Use 'Read the unread pages', or ask "
                "an admin to raise the limit."
            ),
        )
    ]

    again = StoppingReaders(refuse_at=None, output_tokens=2000)
    result = _stages(store, again, rates=rates).read_unread_pages(session, revision.id)
    session.commit()

    assert again.sent == PER_PAGE - 2, "only page 3's dimension questions were asked"
    assert result["pages_waiting"] == [3] and result["still_waiting"] == []
    assert [row.page_number for row in slot_rows(session, revision.id)] == [1, 2, 3]
    assert unchosen_row_pages(session, revision.id) == ()
    assert budget_stopped_pages(session, revision.id) == ()


def test_running_the_whole_reading_again_reuses_every_answer_too(
    session: Session, store: LocalStore
) -> None:
    """Every question answered before the stop is in the call ledger: a reading of the same
    revision takes those answers at no cost (#1112) and asks only what the stop left."""
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
        # #1209 review: a fixed amount above the page budget is not clamped to it, so a proof run
        # can raise the limit.
        ({"ai_budget_usd": Decimal(8)}, Decimal(8)),
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


def test_a_fixed_amount_above_the_claude_ceiling_is_said_at_start_up(
    store: LocalStore, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="gv.workflow.stages"):
        _stages(store, StoppingReaders(refuse_at=None), ai_budget_usd=Decimal(40))

    assert any("below the fixed AI budget of $40" in record.message for record in caplog.records)


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


def test_read_the_unread_pages_through_the_package_screen_and_the_worker(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The product path: the package says which pages wait for the budget, a reviewer asks to read
    them (audited, queued in the outbox), and the local worker's own consumer reads them on the
    same revision."""
    import scripts.drain_outbox as worker
    from app.auth import Role
    from app.lifecycle.side_states import enter_needs_input
    from app.lifecycle.states import transition
    from app.models import OutboxEntry, Package, PackageRevision, PackageState
    from tests.api.test_reading_again import _ask, _client

    revision, _readers = _stopped_reading(session, store)
    transition(session, revision.id, PackageState.MATCHING, actor="the worker")
    transition(session, revision.id, PackageState.VALIDATING_EVIDENCE, actor="the worker")
    enter_needs_input(session, revision.id, actor="the worker", needed="values to confirm")
    session.commit()
    package = session.get_one(Package, revision.package_id)
    screen = _client(session, package.project_id, frozenset({Role.REVIEWER}))
    shown = screen.get(f"/api/v1/projects/{package.project_id}/packages/{package.id}").json()
    assert shown["pages_waiting_for_budget"] == [2, 3]

    asked = _ask(session, session.get_one(PackageRevision, revision.id))
    assert asked.status_code == 202, asked.text
    assert (asked.json()["action"], asked.json()["pages"]) == ("read_unread_pages", [2, 3])
    row = session.scalars(
        select(OutboxEntry).where(OutboxEntry.workflow == "read_unread_pages")
    ).one()

    again = StoppingReaders(refuse_at=None)
    monkeypatch.setattr(worker, "_stages", lambda **_: _stages(store, again))
    result = worker._consume(
        session, workflow=row.workflow, payload=row.payload, idempotency_key=str(row.id)
    )
    session.commit()

    assert result["still_waiting"] == []
    assert again.sent == PAGES * PER_PAGE - BEFORE_PAGE_TWO
    shown = screen.get(f"/api/v1/projects/{package.project_id}/packages/{package.id}").json()
    assert shown["pages_waiting_for_budget"] == []
    assert [row.page_number for row in slot_rows(session, revision.id)] == [1, 2, 3]


# --- #1209 re-review: what "Read the unread pages" must never undo ------------------------------


def test_after_reading_the_unread_pages_the_checks_run_again_before_sign_off(
    session: Session, store: LocalStore
) -> None:
    """N1, end to end with real findings: the checks ran while page 3 was unread; reading it is a
    new input, so sign-off is held ("inputs changed after the last check run") and a check run is
    queued; once the checks run, page 3 has its result and the hold is gone."""
    from app.models import CheckRun, Finding, ObservationCandidate, OutboxEntry, Page
    from app.review.approval import _revisions_with_unchecked_inputs
    from tests.workflow.test_stages import _publish_rulebook

    def pages_with_results() -> list[int]:
        return sorted(
            set(
                session.scalars(
                    select(Page.index + 1)
                    .join(ObservationCandidate, ObservationCandidate.page_id == Page.id)
                    .join(Finding, Finding.scope_row_candidate_id == ObservationCandidate.id)
                    .join(CheckRun, CheckRun.id == Finding.check_run_id)
                    .where(
                        Finding.package_revision_id == revision.id,
                        CheckRun.superseded_at.is_(None),
                    )
                )
            )
        )

    _publish_rulebook(session)
    rates = PricedRates(OUTPUT_PRICE)
    revision = _revision(session, store, _file(), pages=PAGES)
    session.commit()
    _stages(store, StoppingReaders(refuse_at=None, output_tokens=2000), rates=rates).extract_pages(
        session, revision.id
    )
    session.commit()
    DatabaseStages(store=store).run_checks(session, revision.id)
    session.commit()
    assert pages_with_results() == [1, 2]
    assert revision.id not in _revisions_with_unchecked_inputs(session, [revision.id])

    result = _stages(
        store, StoppingReaders(refuse_at=None, output_tokens=2000), rates=rates
    ).read_unread_pages(session, revision.id)
    session.commit()

    assert result["checks_queued"] is True
    queued = session.scalars(select(OutboxEntry).where(OutboxEntry.workflow == "run_checks")).one()
    assert queued.payload["package_revision_id"] == str(revision.id)
    assert revision.id in _revisions_with_unchecked_inputs(session, [revision.id])

    DatabaseStages(store=store).run_checks(session, revision.id)
    session.commit()

    assert pages_with_results() == [1, 2, 3]
    assert revision.id not in _revisions_with_unchecked_inputs(session, [revision.id])


def test_a_wall_answer_and_a_typed_width_on_a_page_already_read_survive(
    session: Session, store: LocalStore
) -> None:
    """N2: "Read the unread pages" writes a new reading of every page, so page 1's row is a new
    record. The reviewer's wall answer and typed width on it come along, by the row identity a
    later revision uses, each naming the answer it was copied from."""
    from app.models import SlotRowReviewDecision

    rates = PricedRates(OUTPUT_PRICE)
    revision = _revision(session, store, _file(), pages=PAGES)
    session.commit()
    _stages(store, StoppingReaders(refuse_at=None, output_tokens=2000), rates=rates).extract_pages(
        session, revision.id
    )
    session.commit()
    first = slot_rows(session, revision.id)[0]
    answer = SlotRowReviewDecision(
        row_candidate_id=first.anchor.id,
        wall_config="back_left_right",
        measurements={"piece_widths:0": "21 in"},
        confirmed_by="synthetic reviewer",
    )
    session.add(answer)
    session.commit()

    result = _stages(
        store, StoppingReaders(refuse_at=None, output_tokens=2000), rates=rates
    ).read_unread_pages(session, revision.id)
    session.commit()

    after = slot_rows(session, revision.id)
    assert [row.page_number for row in after] == [1, 2, 3]
    assert after[0].anchor.id != first.anchor.id, "a new reading: a new row record"
    decision = after[0].decision
    assert decision is not None
    assert (decision.wall_config, decision.measurements) == (
        "back_left_right",
        {"piece_widths:0": "21 in"},
    )
    assert (decision.confirmed_by, decision.carried_from_id) == ("synthetic reviewer", answer.id)
    assert result["answers_carried"] == 1


def test_with_stored_answers_switched_off_the_unread_pages_are_not_read(
    session: Session, store: LocalStore
) -> None:
    """N3: every page would be read again under a budget for the waiting pages only, and pages
    read before could come back unread. Refused with the reason, and nothing is asked."""
    from workflow.stages import READ_UNREAD_NEEDS_REUSE

    revision, _readers = _stopped_reading(session, store)
    again = StoppingReaders(refuse_at=None)
    stages = _stages(store, again)
    assert stages._slot_reader is not None
    stages._slot_reader = replace(stages._slot_reader, reuse_answers=False)

    result = stages.read_unread_pages(session, revision.id)

    assert result == {"ran": False, "reason": READ_UNREAD_NEEDS_REUSE}
    assert again.sent == 0
    assert [page.page_number for page in unchosen_row_pages(session, revision.id)] == [2, 3]


@pytest.mark.parametrize(
    ("setup", "limit"),
    [("unpriced", "unpriced"), ("fixed", "fixed"), ("page budget", None)],
)
def test_the_stop_says_when_reading_the_unread_pages_cannot_help_on_its_own(
    session: Session, store: LocalStore, setup: str, limit: str | None
) -> None:
    """N4: after a call with no price, the model rates must be fixed first; after a fixed amount
    for the set, an admin must raise it first. The stop records which, for the screen."""
    from workflow.slot_row_scope import budget_limit

    revision = _revision(session, store, _file(), pages=PAGES)
    session.commit()
    if setup == "unpriced":
        _earlier_call(session, revision.id, None)
    options = {"ai_budget_usd": Decimal(3)} if setup == "fixed" else {}

    _stages(store, StoppingReaders(refuse_at=BEFORE_PAGE_TWO), **options).extract_pages(
        session, revision.id
    )
    session.commit()

    assert budget_limit(session, revision.id) == limit


def _awaiting_person(session: Session, revision_id: Any) -> None:
    from app.lifecycle.side_states import enter_needs_input
    from app.lifecycle.states import transition
    from app.models import PackageState

    transition(session, revision_id, PackageState.MATCHING, actor="the worker")
    transition(session, revision_id, PackageState.VALIDATING_EVIDENCE, actor="the worker")
    enter_needs_input(session, revision_id, actor="the worker", needed="values to confirm")
    session.commit()


def _screen(session: Session, revision_id: Any, **settings: Any) -> tuple[Any, str]:
    from fastapi.testclient import TestClient

    from app.api.dependencies import get_session
    from app.auth import Principal, Role, authenticate
    from app.config import Settings
    from app.main import create_app
    from app.models import Package, PackageRevision

    revision = session.get_one(PackageRevision, revision_id)
    package = session.get_one(Package, revision.package_id)
    app = create_app(
        Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv", **settings)
    )
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="synthetic reviewer",
        roles=frozenset({Role.REVIEWER}),
        projects=frozenset({package.project_id}),
    )
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app, raise_server_exceptions=False)
    return client, f"/api/v1/projects/{package.project_id}/packages/{package.id}"


def test_the_screen_refuses_reading_the_unread_pages_with_stored_answers_off(
    session: Session, store: LocalStore
) -> None:
    """N3, through the endpoint: refused with the plain reason, nothing queued."""
    from app.api.reading_again import NEEDS_REUSE
    from app.models import OutboxEntry

    revision, _readers = _stopped_reading(session, store)
    _awaiting_person(session, revision.id)
    client, package = _screen(session, revision.id, reader_reuse_answers=False)

    answer = client.post(f"{package}/reading-again")

    assert answer.status_code == 409
    assert answer.json()["message"] == NEEDS_REUSE
    assert session.scalars(select(OutboxEntry)).all() == []


def test_after_a_call_with_no_price_the_screen_says_fix_the_rates_first_and_refuses(
    session: Session, store: LocalStore
) -> None:
    """N4, through the endpoint: the package names the limit, and the action is refused with the
    reason, because every later call would be refused too until the rates name the model."""
    from app.api.reading_again import UNPRICED_FIRST

    revision = _revision(session, store, _file(), pages=PAGES)
    session.commit()
    _earlier_call(session, revision.id, None)
    _stages(store, StoppingReaders(refuse_at=None)).extract_pages(session, revision.id)
    session.commit()
    _awaiting_person(session, revision.id)
    client, package = _screen(session, revision.id)

    shown = client.get(package).json()
    answer = client.post(f"{package}/reading-again")

    assert (shown["pages_waiting_for_budget"], shown["budget_limit"]) == ([1, 2, 3], "unpriced")
    assert answer.status_code == 409
    assert answer.json()["message"] == UNPRICED_FIRST
