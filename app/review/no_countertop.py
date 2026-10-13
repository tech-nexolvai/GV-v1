"""Pages every AI said have no countertop need one reviewer click before sign-off (#1191).

**Why.** Since #1093 a page where every AI answered "no countertop line" is listed, not blocking.
The acceptance walk of 2026-10-12 found a real countertop in that list: code had failed to box its
row, both AIs said "none of these lines", and nobody was ever asked. Anant decided (option c):

1. an older reading that does not say what its "none" meant is blocking (`workflow/slot_row_scope.py`);
2. every page still listed needs one reviewer click before sign-off: **"Confirm: no countertop
   here"**, or **"There is a countertop"**, which turns the page into a blocking item handled the way
   a split page is (hold `row-choice-split`; the check stage writes its "needs you" result).

**Pages code read no row on (#1193).** A vendor-file page with no countertop row to read at all —
only the architect's or reference drawings, a title or divider page, a drawing whose heading is not
one we know — is listed too, with code's reason (`no-row:` records, `workflow/unread_pages.py`), and
needs the same answer: never silent.

**What counts as the same page.** An answer is a `NoCountertopAnswer` row. It counts on the reading
record it was given on, and on another reading of an identical page: the same page number, the same
*known* page content hash (`app/lifecycle/carry_forward.known_content`: a missing hash or the hash of
no bytes never matches) and the same reading answer (the record's `row-pick:` and `row-kind:` flags).
It is looked up across this revision and the earlier revisions of the same package, never a later
one, so a re-run or revision 2 of an unchanged page keeps it and a changed page or a changed answer
is asked again. The newest answer wins.

**Older readings (review finding 9).** A reading from before #1108 whose AIs all answered "no line"
does not say whether that meant "no countertop" or "a countertop none of the lines measures". Its page
is "older": it gets the same two buttons, with a reason saying why it is asked, blocks sign-off until
answered, and never gets a "needs you" result of its own, so a real no-countertop page is signed as
"confirmed no countertop", not as a "not checkable" countertop.

**Pages code proved blank (#1195).** A title or divider page code proved from the PDF has nothing
drawn on it (`no-row:blank-page` with its `blank-proof:` facts, `extraction/blank_page.py`) stays
listed but needs no answer: it never blocks sign-off and never asks the signer about a carried
answer. A reviewer may still say "There is a countertop" on it, which makes it blocking as above.

**Many pages at once (#1195).** "Confirm these N pages" records one answer per page, each exactly
like a single answer (who, when, the optional note), with the batch's id on each, so the audit trail
has one event per page and one for the batch.

**Sign-off.** Readiness (`app/review/approval.py`) refuses while a listed or older page has no
answer, and while a page that is blocking (a reviewer said there is a countertop, or the AIs
disagreed) has no result on the live check run: such a page shows only "Not checked", and signing
past it is the silent skip this issue closes. A confirmation carried from an earlier revision joins
#1180's list of carried answers the signer says still apply. None of this applies to a package for
another product, such as cabinets: it is asked no countertop question at all (#1205).

Verification: `tests/review/test_no_countertop_answers.py`
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, func, or_, select, tuple_
from sqlalchemy.orm import Session

from app.lifecycle.carry_forward import known_content
from app.lifecycle.closed import CLOSED_STATES
from app.models import (
    Document,
    DocumentVersion,
    NoCountertopAnswer,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    Page,
)
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from app.models.verdicts import CheckRun, Finding
from vocabulary.semantic_types import ProductType
from workflow.part_operands import reviewer_owned_pages
from workflow.slot_row_scope import NO_ROW_FLAG, UnchosenRowPage, unchosen_page

__all__ = [
    "ANSWER_WORDS",
    "Answer",
    "PageAnswer",
    "PagesBlockingSignOff",
    "answered_pages",
    "apply_answers",
    "blocking_pages",
    "countertops_checked",
    "pages_blocking_sign_off",
    "pages_words",
    "reading_answer",
    "reviewer_says_countertop",
    "unchecked_sentence",
    "unconfirmed_sentence",
]

Answer = Literal["no_countertop", "countertop"]

#: The two buttons, in the reviewer's words (for the audit trail and the screen's tests).
ANSWER_WORDS: Final[dict[str, str]] = {
    "no_countertop": "Confirm: no countertop here",
    "countertop": "There is a countertop",
}


@dataclass(frozen=True, slots=True)
class PageAnswer:
    """The answer in force on one listed page: what, who, when, and the revision it was given on."""

    answer: Answer
    answered_by: str
    answered_at: datetime
    revision_number: int
    from_revision: int | None
    """The revision it was given on, when that is an earlier revision than the one shown."""
    note: str | None = None
    """The reviewer's optional note (#1195)."""


def reviewer_says_countertop(
    answer: PageAnswer, *, found_by_code: bool = False, budget_stopped: bool = False
) -> str:
    """Why a listed page now needs the reviewer: who said there is a countertop, and when (#1191).
    The check stage records it as the page's result reason, so the signed files print it too.
    `found_by_code`: code, not the AIs, read no row on the page (#1193); `budget_stopped`: the AI
    budget was reached before the page was read (#1206)."""
    when = answer.answered_at.strftime("%Y-%m-%d %H:%M UTC")
    carried = "" if answer.from_revision is None else f" on revision {answer.from_revision}"
    found = (
        "the AI budget was reached before it was read"
        if budget_stopped
        else (
            "code found no countertop row to read on it"
            if found_by_code
            else "both AIs found no countertop line on it"
        )
    )
    return (
        f"{answer.answered_by} said on {when}{carried} that this page has a countertop, although "
        f"{found}, so nothing on it was read or checked. The reviewer sets up this page's countertop "
        "line on the Measurements screen, or decides this page."
    )


@dataclass(frozen=True, slots=True)
class PagesBlockingSignOff:
    """The pages that keep a revision from being signed off, by page number (#1191)."""

    unconfirmed: tuple[int, ...] = ()
    """Pages listed with no countertop that no reviewer has confirmed yet."""
    unchecked: tuple[int, ...] = ()
    """Blocking pages (a reviewer said there is a countertop, or the AIs disagreed) with no result
    on the live run yet."""
    carried: tuple[tuple[int, str], ...] = ()
    """Confirmations carried from an earlier revision's identical page, as `(the revision it was
    given on, words)`: like #1180's carried wall answers, the signer says they still apply."""


def reading_answer(page: UnchosenRowPage) -> str:
    """The reading's own answer on the page, as text: its `row-pick:` and `row-kind:` flags, and
    code's `no-row:` reason code (#1193), sorted.

    Two readings with the same text answered the same way (the same models, the same picks and the
    same kinds of answer, or code's same reason for reading no row)."""
    return "|".join(
        sorted(
            flag
            for flag in page.record.ambiguity_flags or ()
            if flag.startswith(("row-pick:", "row-kind:", NO_ROW_FLAG))
        )
    )


def _identity(page: UnchosenRowPage) -> tuple[int, str, str] | None:
    """The same page on another reading, or `None` when its content is unknown (never carried)."""
    if not known_content(page.page_content_hash):
        return None
    assert page.page_content_hash is not None
    return (page.page_number, page.page_content_hash, reading_answer(page))


def _answers(
    session: Session,
    wanted: dict[UUID, tuple[UUID, int, Sequence[UnchosenRowPage]]],
) -> dict[UUID, dict[UUID, PageAnswer]]:
    """`{revision: {record: answer in force}}` for the listed pages of each revision. One statement.

    `wanted` is `{revision: (package, revision number, its listed pages)}`.
    """
    pages = [page for _, _, listed in wanted.values() for page in listed]
    found: dict[UUID, dict[UUID, PageAnswer]] = {revision: {} for revision in wanted}
    if not pages:
        return found
    record_ids = {page.record.id for page in pages}
    hashes = {
        page.page_content_hash
        for page in pages
        if page.page_content_hash is not None and known_content(page.page_content_hash)
    }
    condition: ColumnElement[bool] = NoCountertopAnswer.row_choice_candidate_id.in_(record_ids)
    if hashes:
        condition = or_(condition, NoCountertopAnswer.page_content_hash.in_(hashes))
    stored = session.execute(
        select(NoCountertopAnswer, PackageRevision.package_id, PackageRevision.revision_number)
        .join(PackageRevision, PackageRevision.id == NoCountertopAnswer.package_revision_id)
        .where(
            PackageRevision.package_id.in_({package for package, _, _ in wanted.values()}),
            condition,
        )
        .order_by(NoCountertopAnswer.created_at.desc(), NoCountertopAnswer.id.desc())
    ).all()
    for revision, (package_id, number, listed) in wanted.items():
        for page in listed:
            identity = _identity(page)
            for answer, answer_package, answer_number in stored:
                if answer_package != package_id or answer_number > number:
                    continue
                same_record = answer.row_choice_candidate_id == page.record.id
                same_page = identity is not None and identity == (
                    answer.page_number,
                    answer.page_content_hash,
                    answer.reading_answer,
                )
                if same_record or same_page:
                    found[revision][page.record.id] = PageAnswer(
                        answer=cast(Answer, answer.answer),
                        answered_by=answer.answered_by,
                        answered_at=answer.created_at,
                        revision_number=answer_number,
                        from_revision=answer_number if answer_number < number else None,
                        note=answer.note,
                    )
                    break
    return found


def apply_answers(
    pages: Iterable[UnchosenRowPage], answers: dict[UUID, PageAnswer]
) -> tuple[UnchosenRowPage, ...]:
    """The pages with each "There is a countertop" applied: that page is blocking, like a split."""
    return tuple(
        (
            replace(
                page,
                kind="split",
                reason=reviewer_says_countertop(
                    answer, found_by_code=page.found_by_code, budget_stopped=page.budget_stopped
                ),
            )
            if page.listed
            and (answer := answers.get(page.record.id)) is not None
            and answer.answer == "countertop"
            else page
        )
        for page in pages
    )


def answered_pages(
    session: Session, revision: PackageRevision, pages: Sequence[UnchosenRowPage]
) -> tuple[tuple[UnchosenRowPage, ...], dict[UUID, PageAnswer]]:
    """The revision's pages with the reviewers' answers applied, and the answers in force by record.

    `pages` are the revision's unchosen pages with any reviewer-owned page already left out. One
    statement when a page is listed; none otherwise.
    """
    listed = [page for page in pages if page.listed]
    if not listed:
        return tuple(pages), {}
    answers = _answers(
        session, {revision.id: (revision.package_id, revision.revision_number, listed)}
    )[revision.id]
    return apply_answers(pages, answers), answers


def blocking_pages(
    pages: Iterable[UnchosenRowPage],
    answers: dict[UUID, PageAnswer],
    checked_records: Collection[UUID],
    *,
    countertops_checked: bool = True,
) -> PagesBlockingSignOff:
    """What these pages (answers already applied) still need before sign-off.

    `checked_records` are the records a live result is scoped to. `countertops_checked` is False on
    a package whose checks never include the countertop width (another product): its blocking pages
    can get no result, so a missing one is not asked for. **Nor is any countertop question (#1205):**
    a cabinet package read before #1205 still has pages listed "with no countertop", and confirming
    them was the wrong question and a blocking one. None of its pages blocks sign-off, and no
    carried confirmation needs the signer's word.
    """
    if not countertops_checked:
        return PagesBlockingSignOff()
    pages = tuple(pages)
    # A page code proved blank (#1195) is listed but needs no answer.
    unconfirmed = sorted(
        {
            page.page_number
            for page in pages
            if page.needs_answer and answers.get(page.record.id) is None
        }
    )
    unchecked = sorted(
        {
            page.page_number
            for page in pages
            if page.kind == "split" and page.record.id not in checked_records
        }
    )
    carried = sorted(
        (
            answer.from_revision,
            page.page_number,
            (
                f"Page {page.page_number}: no countertop confirmed on revision "
                f"{answer.from_revision} ({answer.answered_by})"
            ),
        )
        for page in pages
        if page.needs_answer
        and (answer := answers.get(page.record.id)) is not None
        and answer.answer == "no_countertop"
        and answer.from_revision is not None
    )
    return PagesBlockingSignOff(
        unconfirmed=tuple(unconfirmed),
        unchecked=tuple(unchecked),
        carried=tuple((revision, words) for revision, _, words in carried),
    )


def countertops_checked(product_type: str | None) -> bool:
    """Whether a package's check runs include the countertop width (every product, or countertops)."""
    return product_type is None or product_type == ProductType.COUNTERTOP.value


def pages_blocking_sign_off(
    session: Session, revision_ids: Collection[UUID]
) -> dict[UUID, PagesBlockingSignOff]:
    """`pages_blocking_sign_off` for many revisions in a bounded number of statements.

    One statement when no revision has a slot-reader reading; otherwise the row-choice records, then
    (only where pages exist) the revisions, the reviewer-owned pages, the answers and the live results.
    """
    ids = tuple(dict.fromkeys(revision_ids))
    result = {revision: PagesBlockingSignOff() for revision in ids}
    if not ids:
        return result
    # The newest slot-reader run of each revision, as `latest_slot_reader_run` picks it.
    ranked = (
        select(
            WorkflowRun.package_revision_id.label("revision_id"),
            ExtractionRun.id.label("run_id"),
            func.row_number()
            .over(
                partition_by=WorkflowRun.package_revision_id,
                order_by=(ExtractionRun.created_at.desc(), ExtractionRun.id.desc()),
            )
            .label("rank"),
        )
        .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
        .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .where(
            WorkflowRun.package_revision_id.in_(ids),
            ExtractionRun.extractor_version.like("slot-reader%"),
        )
        .subquery()
    )
    runs = session.execute(
        select(ranked.c.revision_id, ranked.c.run_id).where(ranked.c.rank == 1)
    ).all()
    if not runs:
        return result
    records = session.execute(
        select(PackageRevisionDocument.package_revision_id, Page.index, Page.content_hash)
        .add_columns(ObservationCandidate)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .join(DocumentVersion, DocumentVersion.id == ObservationCandidate.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .where(
            tuple_(
                ObservationCandidate.extraction_run_id,
                PackageRevisionDocument.package_revision_id,
            ).in_([(run_id, revision_id) for revision_id, run_id in runs]),
            Document.kind == "shop",
            ObservationCandidate.ambiguity_flags.contains(["slot-reader-row-choice"]),
        )
        .order_by(Page.index, ObservationCandidate.created_at, ObservationCandidate.id)
    ).all()
    by_revision: dict[UUID, list[UnchosenRowPage]] = {}
    for revision_id, page_index, content_hash, record in records:
        by_revision.setdefault(revision_id, []).append(
            unchosen_page(page_index, record, content_hash)
        )
    if not by_revision:
        return result
    revisions: dict[UUID, tuple[UUID, int, str | None]] = {}
    closed: set[UUID] = set()
    for revision_id, package_id, number, product, state in session.execute(
        select(
            PackageRevision.id,
            PackageRevision.package_id,
            PackageRevision.revision_number,
            Package.product_type,
            PackageRevision.state,
        )
        .join(Package, Package.id == PackageRevision.package_id)
        .where(PackageRevision.id.in_(by_revision))
    ):
        revisions[revision_id] = (package_id, number, product)
        if state in CLOSED_STATES:
            # A closed revision keeps a blank-page proof under the rule it was made with.
            closed.add(revision_id)
    pages_of: dict[UUID, tuple[UnchosenRowPage, ...]] = {}
    for revision_id, pages in by_revision.items():
        owned = reviewer_owned_pages(session, revision_id)
        pages_of[revision_id] = tuple(
            replace(page, closed=revision_id in closed)
            for page in pages
            if page.page_number - 1 not in owned
        )
    answers = _answers(
        session,
        {
            revision_id: (
                revisions[revision_id][0],
                revisions[revision_id][1],
                [page for page in pages if page.listed],
            )
            for revision_id, pages in pages_of.items()
            if revision_id in revisions
        },
    )
    applied = {
        revision_id: apply_answers(pages, answers.get(revision_id, {}))
        for revision_id, pages in pages_of.items()
    }
    split_records = {
        page.record.id for pages in applied.values() for page in pages if page.kind == "split"
    }
    checked = (
        set(
            session.scalars(
                select(Finding.scope_row_candidate_id)
                .join(CheckRun, CheckRun.id == Finding.check_run_id)
                .where(
                    Finding.package_revision_id.in_(applied),
                    Finding.scope_row_candidate_id.in_(split_records),
                    CheckRun.superseded_at.is_(None),
                )
            ).all()
        )
        if split_records
        else set()
    )
    for revision_id, answered in applied.items():
        product = revisions.get(revision_id, (None, None, None))[2]
        result[revision_id] = blocking_pages(
            answered,
            answers.get(revision_id, {}),
            {record for record in checked if record is not None},
            countertops_checked=countertops_checked(product),
        )
    return result


def pages_words(numbers: Sequence[int]) -> str:
    """ "page 3", "pages 3 and 7", "pages 3, 5 and 7"."""
    listed = [str(number) for number in sorted(numbers)]
    if len(listed) == 1:
        return f"page {listed[0]}"
    return f"pages {', '.join(listed[:-1])} and {listed[-1]}"


def unconfirmed_sentence(numbers: Sequence[int]) -> str:
    """The plain sentence that refuses sign-off while listed pages are unconfirmed."""
    verb = "is" if len(numbers) == 1 else "are"
    return (
        "Confirm the pages listed with no countertop before signing off: "
        f'{pages_words(numbers)} {verb} not confirmed yet. On each, choose "Confirm: no '
        'countertop here" or "There is a countertop".'
    )


def unchecked_sentence(numbers: Sequence[int]) -> str:
    """The plain sentence that refuses sign-off while a page that needs the reviewer (a reviewer
    said there is a countertop, or the AIs disagreed) has no result on the live run yet."""
    verb = "has" if len(numbers) == 1 else "have"
    words = pages_words(numbers)
    return (
        f"{words[0].upper()}{words[1:]} {verb} no check result yet. Run the checks before signing "
        "off."
    )
