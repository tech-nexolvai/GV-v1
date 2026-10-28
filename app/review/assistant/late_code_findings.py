"""Code's own findings on the pages the late look compared (2026-10-28, Anant: "yes do 1 to 3").

A full check adds code's own findings once, before its proof steps (`ChatEngine.add_code_findings`,
#1641): the reading ledger's vendor-string sums, the vendor consistency check, GV's site-fit,
filler and sink rules, the splash's other views and the measuring by scale. The late look
(`full_check_roster._Run._late_pages`, #1980) then compares the pages left "out of money" from what
the finished steps left. Its items came after code's findings were made, so they never met them:
a vendor string a late helper read was never checked against code's own reading of that string (a
string that adds up was not dropped, one code's geometry proves was never verified, and an
unverified AI string is never red), the late helpers' strings never became the ledger's sums, and a
late item on a label GV's rule governs was not replaced by the rule's item.

**Not safe to call twice.** A second `add_code_findings` does nothing (it runs once per answer), and
without that guard it would duplicate: the site-fit, filler and sink-offset steps add their items
again (they never look for their own), the ledger re-adds its cross-view and completeness items,
the finish check appends its gaps again, the splash and scale steps redo every first-round item
(the scale step's item cap would be spent on them), and the consistency check's "hidden" strings do
not hold: code's please-check of an overall is already in the answer, so a late reading's string of
that overall is never replaced.

**So this round is scoped** to the late look's own items (`late_ids`: what the late closers added)
and the pages they compared:

- a code finding is added only on a late page, and never when the same one was made before (the
  book records every code finding it was given, `FindingBook.code_made`, added or not): the
  whole-set checks made their items for every page, the late pages too, before the late look;
- a first-round item is never changed, absorbed or proved again (`LateBook`): only late items are;
- the splash and scale steps see only the late items;
- the set-wide lines (the consistency summary, the "not checked" gaps) stay as the first round
  made them (the same checks over the same set);
- the finish check, code's own pairs and the sink-centre check are not run again: they only add
  their own items, made for every page in the first round; a late helper's copy of one is folded
  into one card at the finish (`one_card`), never proved a second time.

Everything this round adds then goes through the late look's proof steps with its other items
(`only_ids`, `_late_proofs`): the same rules, code decides red. Pure code, $0, no AI call.
Verification: `tests/review/assistant/test_late_code_findings.py` (made-up values).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from typing import Any

__all__ = ["LateBook", "ScopedEngine", "code_signature", "late_steps"]

_log = logging.getLogger(__name__)


def _words(value: object) -> str:
    return " ".join(str(value or "").split()).lower()


def code_signature(given: Mapping[str, Any], created_by: str) -> tuple[Any, ...]:
    """What makes two code findings the same one: who made it, its kind, page, item, values and
    vendor spot (the label it names, else its box to the point). The checks are code over the same
    set, so the same item comes back the same."""
    dims = given.get("dims")
    vendor = dims.get("vendor") if isinstance(dims, Mapping) else None
    label = (
        (vendor.get("page"), str(vendor.get("id")))
        if isinstance(vendor, Mapping) and vendor.get("id")
        else None
    )
    box = given.get("vendor_box")
    spot: tuple[int, ...] | None = None
    if isinstance(box, Mapping):
        try:
            spot = tuple(round(float(box[k])) for k in ("x0", "y0", "x1", "y1"))
        except (KeyError, TypeError, ValueError):
            spot = None
    return (
        created_by,
        str(given.get("source") or ""),
        given.get("page"),
        given.get("vs_page"),
        _words(given.get("item")),
        _words(given.get("architect")),
        _words(given.get("vendor")),
        label,
        spot,
    )


class LateBook:
    """The finding book as the late round of code's own findings sees it (see the module): every
    live item to look up (`live`; only the late items when `only_late`), code findings added only
    on a late page and never again, and only the late items changed, absorbed or proved."""

    def __init__(
        self,
        book: Any,
        pages: Collection[int],
        late_ids: set[str],
        made: Collection[tuple[Any, ...]],
        added: list[str],
        *,
        only_late: bool = False,
    ) -> None:
        self._book = book
        self._pages = frozenset(pages)
        self._late = late_ids
        """Shared by the round's views: the late look's items, then what this round adds."""
        self._made = frozenset(made)
        self._only = only_late
        self.added = added
        """The ids this round added (shared by the round's views)."""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._book, name)

    def live(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = self._book.live()
        if not self._only:
            return found
        return [f for f in found if str(f.get("id")) in self._late]

    def add_code_finding(
        self, given: Mapping[str, Any], *, created_by: str = "ledger", room: str | None = None
    ) -> dict[str, Any] | None:
        if given.get("page") not in self._pages:
            return None  # a first-round page: its code findings were made before the late look
        if code_signature(given, created_by) in self._made:
            return None  # made before (added, folded or refused then): never twice
        added: dict[str, Any] | None = self._book.add_code_finding(
            given, created_by=created_by, room=room
        )
        if added is not None:
            self._late.add(str(added["id"]))
            self.added.append(str(added["id"]))
        return added

    def replace(self, finding: Mapping[str, Any]) -> bool:
        if str(finding.get("id")) not in self._late:
            return False  # a first-round item is never changed again
        return bool(self._book.replace(finding))

    def absorb(self, finding_id: str) -> bool:
        if str(finding_id) not in self._late:
            return False
        return bool(self._book.absorb(finding_id))

    def prove_by_code(self, finding_id: str, changes: Mapping[str, Any]) -> dict[str, Any] | None:
        if str(finding_id) not in self._late:
            return None
        proved: dict[str, Any] | None = self._book.prove_by_code(finding_id, changes)
        return proved


class ScopedEngine:
    """The engine with the late round's book (`LateBook`); everything else is the engine's. What a
    step sets on it (the consistency summary line, the "not checked" gaps) stays here: the first
    round's set-wide lines stand."""

    def __init__(self, engine: Any, book: LateBook) -> None:
        self.__dict__["_engine"] = engine
        self.__dict__["book"] = book

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_engine"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        self.__dict__[name] = value  # kept on the view, never the engine's (see the class)


def _consistency(view: ScopedEngine, add: Callable[[Any], Any]) -> None:
    """The consistency check on the late items (its string judgement and its items), then the late
    readings' strings of an overall whose please-check code's own item already holds: replaced by
    it, as the first round replaces them (`vendor_consistency.replaced_by_own`)."""
    from app.review.assistant.vendor_consistency import replaced_by_own

    result = add(view)
    if result is None:
        return
    book = view.book
    with view.finding_lock:
        for finding_id, why in replaced_by_own(book.live(), result).items():
            if book.absorb(finding_id):
                view._show_dropped(finding_id, why)


def late_steps(
    engine: Any,
    pages: Collection[int],
    before: Collection[str],
    *,
    names: Mapping[str, str],
    ledger: Callable[[Any], None],
    checks: Mapping[str, Callable[[Any], Any]],
) -> tuple[list[tuple[str, Callable[[], object]]], list[str]]:
    """The late round's steps, in the first round's order, and the list the ids they add go to.
    `before`: the ids live before the late look; `ledger`: the engine's ledger step taking the
    round's book; `checks`: each module's hook by its step key (`consistency`, `run_fit`,
    `filler_range`, `sink_offset`, `splash_views`, `scale_measure`)."""
    book = engine.book
    late_ids = {str(f.get("id")) for f in book.live()} - {str(i) for i in before}
    made = tuple(getattr(book, "code_made", ()))
    added: list[str] = []
    every = LateBook(book, pages, late_ids, made, added)
    late_only = LateBook(book, pages, late_ids, made, added, only_late=True)
    view, items = ScopedEngine(engine, every), ScopedEngine(engine, late_only)
    steps: list[tuple[str, Callable[[], object]]] = [
        (names["ledger"], lambda: ledger(every)),
        (names["consistency"], lambda: _consistency(view, checks["consistency"])),
        (names["run_fit"], lambda: checks["run_fit"](view)),
        (names["filler_range"], lambda: checks["filler_range"](view)),
        (names["sink_offset"], lambda: checks["sink_offset"](view)),
        (names["splash_views"], lambda: checks["splash_views"](items)),
        (names["scale_measure"], lambda: checks["scale_measure"](items)),
    ]
    return steps, added
