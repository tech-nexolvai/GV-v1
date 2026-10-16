"""The worker's draft cloud PDF job (#1340): the drawing clouded before sign-off, marked DRAFT.

What a reviewer asked for with `POST .../draft-cloud` (`workflow/draft_cloud.py` queues it). It
reads the live results, draws them as the signed cloud PDF would (`render_draft_redline`, INTERNAL
mode), puts a front page listing the open items before the drawing, and a "DRAFT - not signed off"
banner on every page. It decides nothing: every result and every reason is the stored one.

**The open items, in plain words** (front page):

* *Will be clouded as wrong*: every live FAIL.
* *Needs a person*: every result sign-off still waits on (`approval_readiness`) that is not a check
  waiting for its values, plus the pages listed with no countertop that nobody confirmed, and the
  pages that need the checks run again.
* *NOT CHECKED*: a check waiting for values nothing has read yet (the depth and sink checks while
  the depth reader is off: `several_countertops.WAITING`), and a result no rule covers. Printed as
  NOT CHECKED, never as judged (Anant, 2026-10-15), even after a person settled it: settling a
  check that never ran does not make it checked.
* *Settled by a person* (#1394): a check that never ran which a person already settled is not an
  open item, so it is listed apart, never under the open items or their count. The drawing still
  prints it NOT CHECKED (`not_checked_ids`).

**Never silent.** Each failure is raised as `DraftCloudRefused` with a sentence, which the outbox
parks at once and the status read shows.
"""

from __future__ import annotations

import hashlib
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from typing import Final
from uuid import UUID

from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import RectangleObject
from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.package import Package, PackageRevision
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.verdicts import CheckRun, Finding, OutputArtifact, OutputArtifactKind
from app.review.approval import approval_readiness
from app.review.left_for_person import cover_groups, left_for_person
from app.review.reviewer_marks import is_not_checked, live_decisions, reviewer_marks_for
from reports.redline import (
    AI_REVIEW_NOTE,
    ARCHITECT_DIFFERENT_GLYPH,
    AiReviewCounts,
    ArchitectCompare,
    ReviewerMark,
)
from storage.store import ArtifactStore
from verdict.outcomes import Outcome
from workflow.draft_cloud import (
    DraftCloudRefused,
    draft_for_request,
    draft_storage_prefix,
)
from workflow.redline_outputs import (
    NOT_CHECKED,
    ai_review_input,
    architect_compare_input,
    compare_for_render,
    left_for_person_marks,
    redline_marks,
    render_draft_redline,
)

__all__ = [
    "BANNER",
    "OpenItems",
    "ai_review_for_draft",
    "architect_compare_for_draft",
    "grouped_by_reason",
    "old_compare_left_out_words",
    "old_compare_switched_on",
    "open_items",
    "render_draft_cloud",
    "without_old_compare",
]

#: Printed on every page of a draft.
BANNER: Final = "DRAFT – not signed off"

#: The reason every check waiting for unread values starts with (`several_countertops.WAITING`).
_WAITING: Final = "Waiting for "

#: Said after a waiting check a person settled without it running.
_SETTLED: Final = "(A person settled it as not checked.)"

PDF: Final = "application/pdf"


@dataclass(frozen=True, slots=True)
class OpenItem:
    label: str
    reason: str


@dataclass(frozen=True, slots=True)
class OpenItems:
    """The front page's three lists, and which results are printed NOT CHECKED."""

    failures: tuple[OpenItem, ...]
    needs_a_person: tuple[OpenItem, ...]
    not_checked: tuple[OpenItem, ...]
    not_checked_ids: frozenset[UUID]
    settled: tuple[OpenItem, ...] = ()
    """Checks that never ran which a person already settled (#1394): not open, still printed NOT
    CHECKED on the drawing."""


def _label(finding: Finding, rule_id: str) -> str:
    return f"{finding.scope_label} — {rule_id}" if finding.scope_label else rule_id


def _reason(finding: Finding) -> str:
    trace_reason = finding.trace.get("reason") if isinstance(finding.trace, Mapping) else None
    return (
        finding.reason
        or (trace_reason if isinstance(trace_reason, str) else None)
        or "No reason was recorded."
    )


def open_items(
    session: Session,
    package_revision_id: UUID,
    rows: Sequence[tuple[Finding, CheckRun, RuleSnapshot, RuleDefinition]],
) -> OpenItems:
    """Sort the live results into the front page's lists, from stored rows and sign-off readiness."""
    readiness = approval_readiness(session, package_revision_id)
    blocking = set(readiness.blocking_finding_ids)
    # Not checked and nobody settled it (decision 2026-10-16): no longer holds sign-off, still an
    # open NOT CHECKED item here, never "settled by a person".
    unsettled = set(readiness.not_checked_finding_ids)
    # The standing decision on each result: a person's "Not this" on a depth or sink reading makes
    # its check NOT CHECKED (#1360).
    standing = {
        decided.finding_id: decided.actions[-1]
        for decided in live_decisions(
            session, [(finding, definition.rule_id) for finding, _r, _s, definition in rows]
        )
        if decided.actions
    }
    failures: list[OpenItem] = []
    person: list[OpenItem] = []
    not_checked: list[OpenItem] = []
    settled: list[OpenItem] = []
    not_checked_ids: set[UUID] = set()
    # The very switch the review screen and sign-off read (`arch_compare_records_on`), so the
    # front page and the screen count the same rows.
    from app.review.architect_compare import arch_compare_records_on

    old_compare = arch_compare_records_on()
    for finding, _run, _snapshot, definition in rows:
        if definition.rule_id == OLD_COMPARE_RULE and not old_compare:
            # The old row-by-row compare is switched off (`GV_ARCH_COMPARE_RECORDS`): its
            # leftover "nothing was compared" row is not an open item, as on the review screen.
            continue
        item = OpenItem(_label(finding, definition.rule_id), _reason(finding))
        decision = standing.get(finding.id)
        if finding.outcome == Outcome.FAIL.value:
            failures.append(item)
        elif decision is not None and decision.checked_wrong_by_hand:
            # A person checked it by hand and found it wrong (#1358): clouded as wrong, counted
            # here, by the signed record's own mark. The recorded result is unchanged.
            failures.append(
                OpenItem(
                    item.label, f"Checked by hand: WRONG by {decision.reviewer}. {item.reason}"
                )
            )
        elif is_not_checked(finding.outcome, item.reason, standing.get(finding.id)):
            # Settling a check that never ran does not make it checked (decision 1): still
            # printed NOT CHECKED, with who settled it said.
            not_checked_ids.add(finding.id)
            if (
                finding.outcome != Outcome.NO_APPLICABLE_RULE.value
                and finding.id not in blocking
                and finding.id not in unsettled
            ):
                ended = item.reason if item.reason.endswith(".") else f"{item.reason}."
                # Settled: no longer an open item (#1394), listed apart.
                settled.append(OpenItem(item.label, f"{ended} {_SETTLED}"))
                continue
            not_checked.append(item)
        elif finding.id in blocking:
            person.append(item)
    for page in readiness.unconfirmed_no_countertop_pages:
        person.append(
            OpenItem(
                f"Page {page}",
                "Listed with no countertop; nobody has confirmed that yet.",
            )
        )
    for page in readiness.unchecked_countertop_pages:
        person.append(
            OpenItem(f"Page {page}", "Needs the checks run again before it has a result.")
        )
    return OpenItems(
        tuple(failures),
        tuple(person),
        tuple(not_checked),
        frozenset(not_checked_ids),
        tuple(settled),
    )


#: Where the direct compare's results are read (the same function the review items use).
ARCHITECT_COMPARE_MODULE: Final = "app.review.architect_compare"


#: Where the AI page review's rows are read (the same function the review items use).
AI_REVIEW_MODULE: Final = "app.review.ai_review"

#: The old architect check's rule; its rows are open items only while the old compare is on.
OLD_COMPARE_RULE: Final = "CT-ARCH-WIDTH-001"

#: The setting that switches the old direct-compare pipeline on (`GV_ARCH_COMPARE_RECORDS`).
OLD_COMPARE_SETTING: Final = "arch_compare_records"


def old_compare_switched_on() -> bool:
    """Whether the old direct compare is on (`GV_ARCH_COMPARE_RECORDS`). Before that setting
    exists the old compare is the only one, so it stays on, exactly as before."""
    from pydantic import ValidationError

    from app.config import Settings

    field = Settings.model_fields.get(OLD_COMPARE_SETTING)
    if field is None:
        return True
    try:
        return bool(getattr(Settings(), OLD_COMPARE_SETTING))  # type: ignore[call-arg]
    except ValidationError:
        return bool(field.default)


def old_compare_left_out_words(count: int) -> str:
    """Said once wherever the old compare's leftover rows are not printed (never silent)."""
    return (
        f"{count} result(s) of the old architect check ({OLD_COMPARE_RULE}) left out: the old "
        f"architect check ({OLD_COMPARE_RULE}) is switched off, so nothing was compared by it. "
        "The architect is compared by the AI page review instead."
    )


def without_old_compare[Row: tuple[Finding, CheckRun, RuleSnapshot, RuleDefinition]](
    rows: Sequence[Row],
) -> tuple[list[Row], int]:
    """The results in force without the old compare's leftover rows while it is switched off
    (`GV_ARCH_COMPARE_RECORDS`), and how many were left out. Unchanged while it is on."""
    from app.review.architect_compare import arch_compare_records_on

    if arch_compare_records_on():
        return list(rows), 0
    kept = [row for row in rows if row[3].rule_id != OLD_COMPARE_RULE]
    return kept, len(rows) - len(kept)


def ai_review_for_draft(
    session: Session, package_revision_id: UUID
) -> tuple[ArchitectCompare | None, tuple[ReviewerMark, ...]]:
    """The live AI page review for the draft, and the marks of a person who agreed with a row.
    `(None, ())` before the AI page review exists or while it has no row on this revision."""
    import importlib

    try:
        module = importlib.import_module(AI_REVIEW_MODULE)
    except ModuleNotFoundError as error:
        if error.name != AI_REVIEW_MODULE:
            raise
        return None, ()
    return ai_review_input(module.ai_review_for_revision(session, package_revision_id))


def architect_compare_for_draft(
    session: Session, package_revision_id: UUID
) -> tuple[ArchitectCompare | None, tuple[ReviewerMark, ...]]:
    """What the draft compares with the architect: the live AI page review when it has rows, else
    the live direct compare while it is switched on; never both, so nothing is clouded twice."""
    return compare_for_render(
        ai_review_for_draft(session, package_revision_id),
        lambda: (
            _direct_compare_for_draft(session, package_revision_id)
            if old_compare_switched_on()
            else (None, ())
        ),
    )


def _direct_compare_for_draft(
    session: Session, package_revision_id: UUID
) -> tuple[ArchitectCompare | None, tuple[ReviewerMark, ...]]:
    """The live direct compare for the draft, and the marks of a person who agreed with one.

    `(None, ())` before the direct compare exists or has run on this revision: the draft is then
    drawn exactly as before."""
    import importlib

    try:
        module = importlib.import_module(ARCHITECT_COMPARE_MODULE)
    except ModuleNotFoundError as error:
        if error.name != ARCHITECT_COMPARE_MODULE:
            raise
        return None, ()
    items, counts = module.architect_compare_for_revision(session, package_revision_id)
    return architect_compare_input(items, counts)


def _live_rows(
    session: Session, package_revision_id: UUID
) -> list[tuple[Finding, CheckRun, RuleSnapshot, RuleDefinition]]:
    """The results in force, in the order the other outputs list them."""
    return [
        (finding, run, snapshot, definition)
        for finding, run, snapshot, definition in session.execute(
            select(Finding, CheckRun, RuleSnapshot, RuleDefinition)
            .join(CheckRun, Finding.check_run_id == CheckRun.id)
            .join(RuleSnapshot, CheckRun.rule_snapshot_id == RuleSnapshot.id)
            .join(RuleDefinition, RuleSnapshot.rule_definition_id == RuleDefinition.id)
            .where(
                Finding.package_revision_id == package_revision_id,
                CheckRun.superseded_at.is_(None),
            )
            .order_by(Finding.created_at, Finding.id)
        ).all()
    ]


def render_draft_cloud(
    session: Session,
    store: ArtifactStore,
    package_revision_id: UUID,
    request_id: UUID,
    *,
    requested_by: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, object]:
    """Make and record the draft for one request; a repeat of the request returns its draft."""
    existing = draft_for_request(session, package_revision_id, request_id)
    if existing is not None:
        return {"draft_id": str(existing.id), "already_recorded": True}
    revision = session.get(PackageRevision, package_revision_id)
    if revision is None:
        raise DraftCloudRefused("This package revision no longer exists.")
    rows, left_out = without_old_compare(_live_rows(session, package_revision_id))
    if not rows:
        raise DraftCloudRefused("There are no results to draw yet. Run the checks first.")
    items = open_items(session, package_revision_id, rows)

    from workflow.changed_values import changed_values_for_revision

    # The same marks the signed cloud PDF draws from its snapshot (#1363), from the live records.
    shown = [(finding, definition.rule_id) for finding, _run, _snapshot, definition in rows]
    marks = reviewer_marks_for(
        session,
        package_revision_id,
        findings=shown,
        decisions=live_decisions(session, shown),
        until=None,
    )
    architect, agreed = architect_compare_for_draft(session, package_revision_id)
    package = session.get(Package, revision.package_id)
    # Every question is optional (owner decision 2026-10-16): what nobody answered yet is drawn
    # grey, "Not confirmed by a person", as the signed file would print it if signed now.
    left = None if package is None else left_for_person(session, revision, package)
    drawing = render_draft_redline(
        session,
        store,
        package_revision_id=package_revision_id,
        findings=tuple(
            (finding, run, definition.rule_id, snapshot.snapshot_id)
            for finding, run, snapshot, definition in rows
        ),
        not_checked=items.not_checked_ids,
        changed_values=changed_values_for_revision(session, package_revision_id),
        reviewer_marks=(
            *redline_marks(marks),
            *agreed,
            *(() if left is None else left_for_person_marks(left)),
        ),
        architect=architect,
    )
    if drawing.artifact is None:
        raise DraftCloudRefused(f"The draft could not be drawn: {drawing.reason}.")
    screen_open = _screen_open_count(session, package, revision)
    front = _front_page(
        items,
        revision_number=revision.revision_number,
        vendor=None if package is None else package.vendor,
        requested_by=requested_by,
        made_at=now(),
        results=len(rows),
        architect=architect,
        old_compare_left_out=left_out,
        screen_open=screen_open,
        left_for_person=() if left is None else cover_groups(left),
    )
    document = with_banner(front, store.get(drawing.artifact.key).read())

    digest = hashlib.sha256(document).hexdigest()
    key = f"{draft_storage_prefix(package_revision_id, request_id)}{digest}.pdf"
    saved = store.put(key, BytesIO(document), content_type=PDF)
    if saved.sha256 != digest:
        raise DraftCloudRefused("The stored draft does not match what was drawn.")
    artifact = OutputArtifact(
        package_revision_id=package_revision_id,
        kind=OutputArtifactKind.DRAFT_REDLINE.value,
        storage_key=saved.key,
        sha256=digest,
        media_type=PDF,
        findings=len(rows),
        size=len(document),
    )
    session.add(artifact)
    session.flush()
    return {
        "draft_id": str(artifact.id),
        "findings": len(rows),
        "failures": len(items.failures),
        "needs_a_person": len(items.needs_a_person),
        "not_checked": len(items.not_checked),
        "open": screen_open if isinstance(screen_open, int) else None,
    }


def consume_draft_request(
    session: Session, store: ArtifactStore, payload: Mapping[str, object], request_id: str
) -> Mapping[str, object]:
    """The outbox consumer's entry: every failure becomes a sentence the request is parked with."""
    try:
        revision_id = UUID(str(payload["package_revision_id"]))
        requested_by = str(payload.get("requested_by") or "a reviewer")
        return render_draft_cloud(
            session, store, revision_id, UUID(request_id), requested_by=requested_by
        )
    except DraftCloudRefused:
        raise
    except Exception as error:
        raise DraftCloudRefused(
            f"The draft was not made: {type(error).__name__}: {error}"[:900]
        ) from error


# ---------------------------------------------------------------------------
# The front page and the banner
# ---------------------------------------------------------------------------


def _screen_open_count(
    session: Session, package: Package | None, revision: PackageRevision
) -> int | str:
    """The review screen's own "items need you" count (W1 `header.open`, Set 3 QA), so the cover
    and the screen print the same number; or why it could not be read, said on the cover."""
    if package is None:
        return "the package was not found"
    from app.review.review_items import review_items_for_revision

    try:
        listed = review_items_for_revision(
            session, project_id=package.project_id, package_id=package.id, revision=revision
        )
    except Exception as error:  # noqa: BLE001 - said on the cover, never silent
        return f"{type(error).__name__}: {error}"
    return listed.header.open


def _front_page(
    items: OpenItems,
    *,
    revision_number: int,
    vendor: str | None,
    requested_by: str,
    made_at: datetime,
    results: int,
    architect: ArchitectCompare | None = None,
    old_compare_left_out: int = 0,
    screen_open: int | str | None = None,
    left_for_person: Sequence[tuple[str, Sequence[str]]] = (),
) -> bytes:
    width, height = letter
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=letter, invariant=1)
    margin = 54.0
    top = height - margin - 30.0  # below the banner
    cursor = top
    size = 9.5
    columns = int((width - 2 * margin) / (size * 0.5))

    def line(text: str, *, font: str = "Helvetica", at: float = size, gap: float = 1.5) -> None:
        nonlocal cursor
        if cursor < margin:
            canvas.showPage()
            cursor = top
        canvas.setFont(font, at)
        canvas.setFillColorRGB(0.1, 0.1, 0.1)
        canvas.drawString(margin, cursor, text)
        cursor -= at * gap

    def paragraph(text: str, indent: str = "") -> None:
        for wrapped in textwrap.wrap(text, columns - len(indent)) or [""]:
            line(f"{indent}{wrapped}")

    line("Draft cloud PDF", font="Helvetica-Bold", at=20.0)
    paragraph(
        f"Revision {revision_number}"
        + (f", {vendor}" if vendor else "")
        + f". Made {made_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC for {requested_by}."
    )
    paragraph(
        "This is a draft. Nobody has signed it off, and it must not be sent to the vendor. The "
        "final cloud PDF is made after a reviewer signs off."
    )
    line("")
    if isinstance(screen_open, int):
        # The screen's own count (Set 3 QA: the screen said 51, the cover 44 + 2); the lists
        # below count results, and one result can hold several numbers to check.
        paragraph(
            f"{results} result(s) in force. Open items: {screen_open}, the same count as the "
            f"review screen. Results: {len(items.failures)} will be clouded as wrong, "
            f"{len(items.needs_a_person)} need a person (one result can hold several numbers), "
            f"{len(items.not_checked)} not checked."
        )
    else:
        paragraph(
            f"{results} result(s) in force. Open items: {len(items.failures)} will be clouded as "
            f"wrong, {len(items.needs_a_person)} need a person, {len(items.not_checked)} not "
            "checked."
        )
        if screen_open is not None:
            paragraph(f"The review screen's count could not be read: {screen_open}.")
    if old_compare_left_out:
        paragraph(old_compare_left_out_words(old_compare_left_out))
    line("")
    # The AI page review's differences are clouded by themselves; no person answers them
    # (decision 2026-10-16), so they are their own section, never "needs a person".
    ai = (
        architect
        if architect is not None and isinstance(architect.counts, AiReviewCounts)
        else None
    )
    nobody = (
        "None. No result is waiting for a person; the AI differences below are clouded by "
        "themselves."
        if ai is not None and ai.different
        else "None. Nothing is waiting for a person."
    )
    for title, entries, empty in (
        ("Will be clouded as wrong", items.failures, "None. No result is wrong."),
        ("Needs a person", items.needs_a_person, nobody),
    ):
        line(f"{title} ({len(entries)})", font="Helvetica-Bold", at=12.0)
        if not entries:
            paragraph(empty)
        for entry in entries:
            paragraph(entry.label)
            paragraph(entry.reason, "    ")
        line("")
    if left_for_person:
        # Every question is optional (owner decision 2026-10-16): signing now prints each of these
        # as not confirmed by a person, never drops it.
        total = sum(len(lines) for _kind, lines in left_for_person)
        line(f"Left for a person ({total})", font="Helvetica-Bold", at=12.0)
        paragraph(
            "Optional. If the review is signed now, each is printed as not confirmed by a person; "
            "a FAIL stays clouded as wrong."
        )
        for kind, lines in left_for_person:
            paragraph(f"{kind} ({len(lines)})")
            for text in lines:
                paragraph(text, "    ")
        line("")
    if ai is not None:
        line(
            f"Compared with the architect (AI read) ({len(ai.different)} different)",
            font="Helvetica-Bold",
            at=12.0,
        )
        paragraph(ai.counts.line)
        paragraph(AI_REVIEW_NOTE)
        for number, difference in enumerate(ai.different, start=1):
            # The front page is plain Helvetica, which has no ✗: the heading says "different".
            words = difference.words.removeprefix(f"{ARCHITECT_DIFFERENT_GLYPH} ")
            paragraph(f"A{number} {words} \u2014 {difference.subject}")
        line("")
    line(f"{NOT_CHECKED} ({len(items.not_checked)})", font="Helvetica-Bold", at=12.0)
    if not items.not_checked:
        paragraph("None. Every check ran.")
    # One reason said once, with every result it covers (a set waiting for the depth reader has
    # dozens with the same words).
    for reason, labels in grouped_by_reason(items.not_checked):
        paragraph(f"{NOT_CHECKED}: {len(labels)} check(s): {reason}")
        paragraph("; ".join(labels), "    ")
    line("")
    if items.settled:
        line(f"Settled by a person ({len(items.settled)})", font="Helvetica-Bold", at=12.0)
        paragraph(
            f"Not open. These checks never ran, so the drawing still prints them {NOT_CHECKED}."
        )
        for reason, labels in grouped_by_reason(items.settled):
            paragraph(f"{len(labels)} check(s): {reason}")
            paragraph("; ".join(labels), "    ")
        line("")
    paragraph(
        "Every result, including those that passed, is listed again on the summary pages after "
        "the drawing. A page with no cloud is not thereby approved."
    )
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def grouped_by_reason(entries: Sequence[OpenItem]) -> list[tuple[str, tuple[str, ...]]]:
    """Each distinct reason once, in first-seen order, with the labels of every item it covers."""
    groups: dict[str, list[str]] = {}
    for entry in entries:
        groups.setdefault(entry.reason, []).append(entry.label)
    return [(reason, tuple(labels)) for reason, labels in groups.items()]


def _box(page: PageObject) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    box = page.cropbox
    return (
        Decimal(str(box.left)),
        Decimal(str(box.bottom)),
        Decimal(str(box.right)),
        Decimal(str(box.top)),
    )


def _user_point(
    rotation: int, box: tuple[Decimal, Decimal, Decimal, Decimal], u: Decimal, v: Decimal
) -> tuple[Decimal, Decimal]:
    """A point measured on the page as it is shown (from its lower-left corner) in PDF user space."""
    x0, y0, x1, y1 = box
    if rotation == 90:
        return x1 - v, y0 + u
    if rotation == 180:
        return x1 - u, y1 - v
    if rotation == 270:
        return x0 + v, y1 - u
    return x0 + u, y0 + v


#: The DRAFT strip added above each page's visible top (QA 2026-10-16), in points: the banner
#: sits in it, never over the drawing's content.
BANNER_STRIP: Final = Decimal(14)


def _grow_top(page: PageObject, rotation: int, amount: Decimal) -> None:
    """Add `amount` points above the page's visible top edge, to both its boxes."""
    for name in ("/MediaBox", "/CropBox"):
        box = page.mediabox if name == "/MediaBox" else page.cropbox
        x0, y0, x1, y1 = (
            Decimal(str(value)) for value in (box.left, box.bottom, box.right, box.top)
        )
        if rotation == 0:
            y1 += amount
        elif rotation == 90:
            x0 -= amount
        elif rotation == 180:
            y0 -= amount
        else:
            x1 += amount
        grown = (float(x0), float(y0), float(x1), float(y1))
        if name == "/MediaBox":
            page.mediabox = RectangleObject(grown)
        else:
            page.cropbox = RectangleObject(grown)


def _banner_overlay(page: PageObject) -> PageObject:
    """A thin red strip with the DRAFT words, in the strip `_grow_top` added above the page."""
    rotation = (page.rotation or 0) % 360
    box = _box(page)
    shown_width = box[2] - box[0] if rotation in (0, 180) else box[3] - box[1]
    shown_height = box[3] - box[1] if rotation in (0, 180) else box[2] - box[0]
    size = BANNER_STRIP * Decimal("0.7")
    buffer = BytesIO()
    media = page.mediabox
    canvas = Canvas(
        buffer,
        pagesize=(float(media.right) - float(media.left), float(media.top) - float(media.bottom)),
        invariant=1,
    )
    canvas.translate(-float(media.left), -float(media.bottom))
    text_width = Decimal(str(canvas.stringWidth(BANNER, "Helvetica-Bold", float(size))))
    x, y = _user_point(rotation, box, Decimal(0), shown_height - BANNER_STRIP)
    canvas.saveState()
    canvas.translate(float(x), float(y))
    canvas.rotate(rotation)
    canvas.setFillColorRGB(0.75, 0.05, 0.05)
    canvas.rect(0, 0, float(shown_width), float(BANNER_STRIP), stroke=0, fill=1)
    canvas.setFillColorRGB(1, 1, 1)
    canvas.setFont("Helvetica-Bold", float(size))
    canvas.drawString(
        float((shown_width - text_width) / 2), float(BANNER_STRIP * Decimal("0.27")), BANNER
    )
    canvas.restoreState()
    canvas.showPage()
    canvas.save()
    return PdfReader(BytesIO(buffer.getvalue())).pages[0]


def with_banner(front: bytes, drawing: bytes) -> bytes:
    """The front page, then the drawing and its summary, each page bannered DRAFT."""
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(front)))
    writer.append(PdfReader(BytesIO(drawing)))
    for page in writer.pages:
        # A thin strip above the page's visible top carries the banner, so it never covers a
        # note or a dimension (QA 2026-10-16).
        _grow_top(page, (page.rotation or 0) % 360, BANNER_STRIP)
        page.merge_page(_banner_overlay(page))
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
