"""A vendor-file page code read no countertop row on is recorded with its reason, never dropped (#1193).

**Why.** The slot reader asks the AIs only about pages where code finds a countertop row candidate.
Before #1193 a page with none was skipped with no record at all (`workflow/stages.py`, the Claude
path): it was in neither the results nor the "pages with no countertop" list. On AI Set 3 that hid
14 of 60 pages, real vendor drawings among them. Now every such page is recorded as a row-choice
record of code's (`no-row:<code>`, `workflow/slot_row_scope.NO_ROW_FLAG`) with a plain reason, so it
is listed with the pages with no countertop and needs a reviewer's answer before sign-off (#1191).

**The reasons, from what code knows about the page.**

* `no-drawing` — no drawing is pasted on the page, in a file whose other pages carry pasted drawings:
  a title or divider page.
* `reference-only` — every drawing on it is a reference drawing (a brand prototype) by its own
  heading; they are never read for a countertop.
* `no-vendor-drawing` — no drawing is the vendor's: each is the architect's or a reference drawing,
  or its own heading is not one we know (quoted), or it has no heading of its own.
* `no-row-found` — a drawing is read as the vendor's (or the page prints no headings at all), but
  code found no row of dimensions on it that could be a countertop.
* `unreadable` — the page or its drawings could not be read.
* `blank-page` — code proved from the PDF that nothing is drawn on the page (#1195,
  `extraction/blank_page.py`): no pasted drawing, no picture, no ink beyond the page frame and its
  typed title. The proof's facts are stored with the record (`blank-proof:` flags). It is still
  listed, but needs no reviewer answer before sign-off.

* `budget-reached` — the drawing set's AI budget was reached before this page's own reading (its
  row and dimension questions) finished (#1206): "AI budget $X reached after page k of N. Use
  'Read the unread pages', or ask an admin to raise the limit." (or, when a call had no price,
  that no more calls were made and the model rates need checking). It needs the reviewer's answer
  (or that reading) before sign-off, like any listed page, and starts unticked.

A `no-drawing` page code could not prove blank carries `no-row-ink-found` and says so: something is
drawn or pictured on it (a scanned or flattened sheet) that code cannot read (#1195 review).

A page with a drawing whose role code could not tell (an unknown label, or none of its own) also
carries `no-row-unnamed-drawing`: it may hold the vendor's drawing, so the screen's "Confirm these
pages" action leaves it unticked (#1195).

Pure apart from `persist_unread_pages`. Source: issues #1193, #1195. Verification:
tests/workflow/test_unread_pages.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import ViewRole
from app.models.document import Page
from app.models.drawing import REFERENCE_ROLE
from app.models.evidence import ObservationCandidate
from extraction.blank_page import BLANK_PAGE_RULE, BlankPageFacts, prove_blank_pages
from extraction.panels import PanelLayout, PanelRoleProposal
from workflow.slot_row_scope import (
    BLANK_PROOF_FLAG,
    INK_FOUND_FLAG,
    NO_ROW_CODES,
    NO_ROW_FLAG,
    UNNAMED_DRAWING_FLAG,
)
from workflow.view_roles import CODE_CONFIRMERS

__all__ = [
    "BLANK_PAGE",
    "BUDGET_LIMIT_FLAG",
    "BUDGET_REACHED",
    "INK_FOUND_REASON",
    "ConfirmedRole",
    "UnreadPage",
    "blank_proof_flags",
    "budget_reached",
    "has_unnamed_drawing",
    "persist_unread_pages",
    "unread_page",
    "unread_page_reason",
    "with_blank_proofs",
]

#: The reason column's width (`ObservationCandidate.review_reason`).
_REASON_LIMIT: Final = 300
_QUOTED: Final = 50

UNREADABLE: Final = (
    "unreadable",
    "Code could not read this page's drawing, so nothing on it was read or checked.",
)
NO_DRAWING: Final = (
    "no-drawing",
    (
        "No drawing is pasted on this page (a title or divider page), so there is no countertop "
        "to check."
    ),
)

#: A page with no pasted drawing that code could not prove blank (#1195 review): something is drawn
#: or pictured on it (a scanned or flattened sheet, an underline), which code cannot read.
INK_FOUND_REASON: Final = (
    "Something is drawn on this page but no drawing is pasted, so code could not read it. Nothing "
    "on it was read or checked: look at the page before confirming it."
)

BLANK_PAGE: Final = (
    "blank-page",
    (
        "Title or divider page: nothing drawn. Code checked the PDF: no pasted drawing, no "
        "picture and no lines beyond the page frame and its typed title, so there is no "
        "countertop to check."
    ),
)


#: The `no-row:` code of a page left unread because the AI budget was reached (#1206).
BUDGET_REACHED: Final = "budget-reached"

#: On a `budget-reached` record, why "Read the unread pages" cannot help on its own (#1206):
#: `budget-limit:unpriced`, `budget-limit:fixed` or `budget-limit:maximum`.
BUDGET_LIMIT_FLAG: Final = "budget-limit:"


def budget_reached(
    *, cap_usd: Decimal, read_through: int, page_count: int, unpriced: bool = False
) -> tuple[str, str]:
    """`(no-row code, the reviewer's sentence)` for a page left unread when the drawing set's AI
    budget of `cap_usd` was reached after page `read_through` (1-based; 0 when no page was read in
    full) of a file of `page_count` pages (#1206). `unpriced`: the reading stopped because a call
    had no price, so no amount was reached; the admin checks the model rates."""
    if read_through < 0 or page_count < 1 or read_through > page_count:
        raise ValueError("the stop must name a page of the file")
    where = (
        f"after page {read_through} of {page_count}"
        if read_through
        else f"before page 1 of {page_count} was read"
    )
    if unpriced:
        return (
            BUDGET_REACHED,
            (
                f"AI reading stopped {where}: a call had no price, so no more calls were made; "
                "check the model rates."
            ),
        )
    amount = f"${cap_usd.quantize(Decimal('0.01'))}"
    return (
        BUDGET_REACHED,
        (
            f"AI budget {amount} reached {where}. Use 'Read the unread pages', or ask an admin "
            "to raise the limit."
        ),
    )


@dataclass(frozen=True, slots=True)
class UnreadPage:
    """One page code read no countertop row on, with its `no-row:` code and reason.

    `blank_proof` holds what code checked when it proved the page blank (#1195); `unnamed_drawing`
    says a drawing on it has no role code could tell (#1195); `ink_found` that no drawing is pasted
    on it but code could not prove it blank (#1195 review)."""

    page_id: UUID
    page_index: int
    document_version_id: UUID
    width_pt: Decimal
    height_pt: Decimal
    code: str
    reason: str
    blank_proof: BlankPageFacts | None = None
    unnamed_drawing: bool = False
    ink_found: bool = False
    budget_limit: str | None = None
    """On a `budget-reached` page (#1206), when "Read the unread pages" cannot help on its own:
    `unpriced` (a call had no price; the model rates must be fixed first), `fixed` (the set's fixed
    amount was reached) or `maximum` (the admin's maximum was reached); an admin raises those."""

    def __post_init__(self) -> None:
        if self.code not in NO_ROW_CODES:
            raise ValueError(f"unknown no-row code {self.code!r}")
        if not self.reason.strip():
            raise ValueError("an unread page needs its reason")
        if (self.code == BLANK_PAGE[0]) != (
            self.blank_proof is not None and self.blank_proof.blank
        ):
            raise ValueError("a blank page, and only a blank page, carries its full proof")


def _quoted(text: str | None) -> str:
    label = " ".join((text or "").split())
    return repr(label if len(label) <= _QUOTED else label[: _QUOTED - 1] + "…")


def _fit(text: str) -> str:
    return text if len(text) <= _REASON_LIMIT else text[: _REASON_LIMIT - 1] + "…"


def _grouped(kind: str, proposals: Sequence[PanelRoleProposal]) -> str:
    """ "the architect's drawing ('ID SET')" or "2 reference drawings ('BRAND PROTOTYPE')"."""
    labels = list(dict.fromkeys(_quoted(proposal.heading) for proposal in proposals))
    count = len(proposals)
    noun = f"{kind} drawing" if count == 1 else f"{count} {kind} drawings"
    lead = f"the {noun}" if count == 1 else noun
    return f"{lead} ({', '.join(labels)})"


@dataclass(frozen=True, slots=True)
class ConfirmedRole:
    """A drawing's confirmed role and who confirmed it: a person's name, or one of code's names
    (`workflow/view_roles.CODE_CONFIRMERS`)."""

    role: str
    confirmed_by: str

    @property
    def by_code(self) -> bool:
        return self.confirmed_by in CODE_CONFIRMERS

    def words(self, place: str, side: str) -> str:
        """ "code decided the lower drawing is the architect's", "a.person confirmed the …"."""
        if self.by_code:
            return f"code decided {place} is {side}"
        return f"{self.confirmed_by} confirmed {place} as {side}"


def unread_page_reason(
    layout: PanelLayout | None,
    confirmed: Mapping[int, ConfirmedRole],
    *,
    has_drawings_elsewhere: bool,
) -> tuple[str, str]:
    """`(no-row code, the reviewer's sentence)` for a page code found no countertop row on.

    `layout` is the page's headings and drawings (`extraction/panels.read_panels`), `None` when its
    annotations could not be read; `confirmed` a person's (or code's) confirmed role by stamp, with
    who confirmed it, which outranks the heading's suggestion; `has_drawings_elsewhere` whether another page of the same
    file carries pasted drawings (then a page with none is a title or divider page).
    """
    if layout is None:
        return UNREADABLE
    drawings = [proposal for proposal in layout.proposals if not proposal.nested]
    if not drawings:
        if has_drawings_elsewhere:
            return NO_DRAWING
        return (
            "no-row-found",
            (
                "Code found no row of dimensions on this page that could be a countertop, so "
                "nothing on it was read."
            ),
        )

    def role(proposal: PanelRoleProposal) -> str | None:
        decided = confirmed.get(proposal.annotation_index)
        return proposal.role if decided is None else decided.role

    vendor = [
        proposal
        for proposal in drawings
        if role(proposal) == ViewRole.SHOP.value or (role(proposal) is None and not layout.labelled)
    ]
    if vendor:
        where = "the vendor's drawing" if layout.labelled else "this page"
        return (
            "no-row-found",
            (
                f"Code found no row of dimensions on {where} that could be a countertop, so "
                "nothing on it was read."
            ),
        )
    references = [proposal for proposal in drawings if role(proposal) == REFERENCE_ROLE]
    if len(references) == len(drawings):
        return (
            "reference-only",
            _fit(
                "Only reference drawings (brand prototype) are on this page "
                f"({', '.join(dict.fromkeys(_quoted(p.heading) for p in references))}); they are "
                "never read for a countertop or compared."
            ),
        )
    parts: list[str] = []
    if layout.labelled and not any(proposal.known for proposal in layout.proposals):
        # A page whose only labels are ones we do not know (a capitals note at heading size, say):
        # it counts as a page with headings, so no drawing on it is read as the vendor's. Say so.
        labels = ", ".join(dict.fromkeys(_quoted(note.text) for note in layout.headings))
        parts.append(
            f"this page prints labels ({labels}) but none is one we know, so no drawing on it is "
            "read as the vendor's"
        )
    for proposal in drawings:
        if role(proposal) is not None:
            continue
        if proposal.heading is not None and not proposal.known:
            parts.append(
                f"the label {proposal.side or 'above'} {proposal.place} "
                f"({_quoted(proposal.heading)}) is not one we know"
            )
        elif proposal.reason.startswith("no label"):
            parts.append(f"{proposal.place} has no label of its own")
        else:
            parts.append(f"it is not clear which label names {proposal.place}")
    decided = [
        (proposal, decision)
        for proposal in drawings
        if (decision := confirmed.get(proposal.annotation_index)) is not None
        and decision.role == ViewRole.ARCH.value
    ]
    for proposal, decision in decided:
        parts.append(decision.words(proposal.place, "the architect's"))
    architect = [
        proposal
        for proposal in drawings
        if role(proposal) == ViewRole.ARCH.value and proposal.annotation_index not in confirmed
    ]
    if architect:
        parts.append(_grouped("architect's", architect))
    if references:
        parts.append(_grouped("reference", references) + ", never checked")
    return (
        "no-vendor-drawing",
        _fit(f"No vendor drawing on this page: {'; '.join(parts)}. Nothing on it was read."),
    )


def has_unnamed_drawing(layout: PanelLayout | None, confirmed: Mapping[int, ConfirmedRole]) -> bool:
    """Whether a drawing on a page that prints headings has no role code could tell (#1195): its
    label is not one we know, or it has none of its own. Such a drawing may be the vendor's."""
    if layout is None or not layout.labelled:
        return False
    return any(
        proposal.role is None and proposal.annotation_index not in confirmed
        for proposal in layout.proposals
        if not proposal.nested
    )


def blank_proof_flags(facts: BlankPageFacts) -> list[str]:
    """The proof as stored on the page's record: one `blank-proof:<fact>=<count>` per fact."""
    return [
        f"{BLANK_PROOF_FLAG}{name}={value}"
        for name, value in (
            ("rule", BLANK_PAGE_RULE),
            ("stamps", facts.stamps),
            ("other-annotations", facts.other_annotations),
            ("images", facts.images),
            ("drawn-paths", facts.drawn_paths),
            ("frames", facts.frames),
            ("white-paint", facts.white_paint),
            ("typed-texts", facts.typed_texts),
            ("typed-characters", facts.typed_characters),
        )
    ]


def with_blank_proofs(data: bytes, pages: Sequence[UnreadPage]) -> list[UnreadPage]:
    """The pages, each one code proves blank from the PDF's bytes recorded as `blank-page` with its
    proof (#1195). A page code could not read stays as it is. A `no-drawing` page that is not
    proven blank has something drawn or pictured on it that code cannot read (a scanned or
    flattened sheet, say): it says so and is marked doubtful (#1195 review). Any other page stays
    as it is."""
    asked = [page.page_index for page in pages if page.code != UNREADABLE[0]]
    facts = prove_blank_pages(data, asked) if asked else {}
    out: list[UnreadPage] = []
    for page in pages:
        proof = facts.get(page.page_index) if page.code != UNREADABLE[0] else None
        if proof is not None and proof.blank:
            code, reason = BLANK_PAGE
            out.append(replace(page, code=code, reason=reason, blank_proof=proof))
        elif page.code == NO_DRAWING[0]:
            out.append(replace(page, reason=INK_FOUND_REASON, ink_found=True))
        else:
            out.append(page)
    return out


def persist_unread_pages(
    session: Session, *, extraction_run_id: UUID, pages: Sequence[UnreadPage], dpi: int
) -> int:
    """One row-choice record of code's per page (`no-row:<code>`, the reason), in the slot-reader
    run, so the page is listed (`workflow/slot_row_scope.unchosen_row_pages`). Its region is the
    whole page, in image pixels at `dpi`: no line on it was chosen."""
    for page in pages:
        width = int((page.width_pt * dpi / 72).to_integral_value())
        height = int((page.height_pt * dpi / 72).to_integral_value())
        session.add(
            ObservationCandidate(
                id=uuid4(),
                document_version_id=page.document_version_id,
                page_id=page.page_id,
                extraction_run_id=extraction_run_id,
                raw_text="",
                value_numerator=None,
                value_denominator=None,
                unit=None,
                semantic_guess=None,
                polygon=[[0, 0], [width, 0], [width, height], [0, height]],
                coordinate_space="image",
                confidence=None,
                ambiguity_flags=[
                    "slot-reader-row-choice",
                    f"{NO_ROW_FLAG}{page.code}",
                    *(() if page.blank_proof is None else blank_proof_flags(page.blank_proof)),
                    *((UNNAMED_DRAWING_FLAG,) if page.unnamed_drawing else ()),
                    *((INK_FOUND_FLAG,) if page.ink_found else ()),
                    *(
                        ()
                        if page.budget_limit is None
                        else (f"{BUDGET_LIMIT_FLAG}{page.budget_limit}",)
                    ),
                ],
                review_reason=_fit(page.reason),
            )
        )
    session.flush()
    return len(pages)


def unread_page(
    page: Page, code_and_reason: tuple[str, str], *, unnamed_drawing: bool = False
) -> UnreadPage:
    """The page as recorded: its identity, size and code's reason."""
    code, reason = code_and_reason
    return UnreadPage(
        page_id=page.id,
        page_index=page.index,
        document_version_id=page.document_version_id,
        width_pt=Decimal(page.width_pt),
        height_pt=Decimal(page.height_pt),
        code=code,
        reason=reason,
        unnamed_drawing=unnamed_drawing,
    )
