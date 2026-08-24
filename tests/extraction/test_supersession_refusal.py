"""Unresolved supersession becomes REVIEW REQUIRED (#185, B11.3).

`docs/DESIGN_EXTRACTION.md` §7: *"Unresolved supersession produces REVIEW REQUIRED for every finding
drawn from that sheet. It never resolves to 'the last page wins' or 'the highest letter wins'. Every
other guard in the system assumes the source page was the right page, and this is the only place that
assumption is checked, so it fails closed."*

`tests/extraction/test_supersession.py` covers *when* the resolver refuses. This file covers what a
refusal **is**: the outcome it carries, that the outcome can never be a decision, and that the trace
gives a reviewer enough to settle it. Those are separable — a resolver that refused correctly and then
reported an outcome the engine treats as a pass would be worse than one that never refused, because the
refusal would look like it had been handled.

This file imports `verdict/` and `extraction/supersession.py` does not. That asymmetry is the point:
§2 forbids the module the import, and a test proving two vocabularies agree is not the same thing as a
module depending on one.

Source: `docs/DESIGN_EXTRACTION.md` §7 · Verification: this file
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

import pytest

from extraction.manifest import PageRecord
from extraction.revision import RevisionBlock, RevisionDate, RevisionHistoryRow, RevisionLabel
from extraction.supersession import (
    DATE_CONTRADICTS_ORDER,
    DUPLICATE_REVISION,
    NO_RECORDED_ORDER,
    NO_SHEET_NUMBER,
    UNKNOWN_REVISION,
    GoverningRevision,
    SheetPage,
    SupersessionStatus,
    Unresolved,
    governing_revision,
)

# The engine's own vocabulary. Imported *here* precisely because the module may not.
from verdict.outcomes import ABSTAINING_OUTCOMES, DECISIVE_OUTCOMES, Outcome, is_decision


def _page(index: int, sheet_number: str | None = "A-101") -> PageRecord:
    return PageRecord(
        index=index,
        content_hash=hashlib.sha256(f"page-{index}".encode()).hexdigest(),
        width_pt=Decimal(842),
        height_pt=Decimal(595),
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        sheet_number=sheet_number,
    )


def _labelled(index: int, label: str, number: str | None = "A-101") -> SheetPage:
    """A page carrying only its own revision label — no history, so nothing orders it."""
    return SheetPage(_page(index, number), RevisionBlock(current=RevisionLabel(label)))


def _with_history(index: int, *listed: str, dates: dict[str, date] | None = None) -> SheetPage:
    dates = dates or {}
    rows = tuple(
        RevisionHistoryRow(
            RevisionLabel(
                label,
                RevisionDate(dates[label].isoformat(), (dates[label],)) if label in dates else None,
                sequence_index=position,
            )
        )
        for position, label in enumerate(listed)
    )
    return SheetPage(_page(index), RevisionBlock(rows[-1].label, rows))


def _every_cause() -> dict[str, Unresolved]:
    """One refusal of each kind, so the tests below cover all five rather than the convenient one."""
    no_order = governing_revision([_labelled(0, "A"), _labelled(1, "C")])
    unknown = governing_revision([_with_history(0, "A", "B"), SheetPage(_page(1), RevisionBlock())])
    duplicate = governing_revision([_with_history(0, "A", "B"), _with_history(1, "A", "B")])
    no_number = governing_revision([_labelled(0, "A", None)])
    contradicting = governing_revision(
        [
            _with_history(0, "A", dates={"A": date(2026, 5, 1)}),
            _with_history(1, "A", "B", dates={"A": date(2026, 5, 1), "B": date(2026, 1, 15)}),
        ]
    )

    found = {
        NO_RECORDED_ORDER: no_order,
        UNKNOWN_REVISION: unknown,
        DUPLICATE_REVISION: duplicate,
        NO_SHEET_NUMBER: no_number,
        DATE_CONTRADICTS_ORDER: contradicting,
    }
    for cause, outcome in found.items():
        assert isinstance(outcome, Unresolved), f"{cause} did not refuse"
        assert outcome.cause == cause, f"expected {cause}, got {outcome.cause}"
    return found  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# The outcome is REVIEW REQUIRED, and can be nothing else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cause", sorted(_every_cause()))
def test_every_refusal_is_review_required(cause: str) -> None:
    """All five causes, not just the one that was easiest to construct.

    A refusal that reported a different outcome for one cause would be a hole exactly where nobody looks:
    the rare failure mode.
    """
    assert _every_cause()[cause].status is SupersessionStatus.REVIEW_REQUIRED


def test_the_status_matches_the_engines_own_vocabulary() -> None:
    """The string must be the engine's, or an unresolved sheet produces an outcome nothing recognises.

    `extraction/` may not import `verdict/` (§2), so the two vocabularies are written separately and
    pinned together here. Without this test the module's docstring would be the only thing claiming they
    agree — and a claim in a docstring is not a check.
    """
    assert str(SupersessionStatus.REVIEW_REQUIRED) == Outcome.REVIEW_REQUIRED.value


def test_review_required_is_an_abstention_and_not_a_decision() -> None:
    """The property that makes refusing safe.

    If `REVIEW_REQUIRED` were ever classed as decisive, an unresolved sheet would count towards the
    automation rate and the false-PASS metric would stop meaning what it says.
    """
    assert Outcome.REVIEW_REQUIRED in ABSTAINING_OUTCOMES
    assert Outcome.REVIEW_REQUIRED not in DECISIVE_OUTCOMES
    assert not is_decision(Outcome.REVIEW_REQUIRED)


def test_no_pass_can_be_produced_from_an_unresolved_sheet() -> None:
    """The fourth acceptance criterion, asserted structurally rather than by example.

    An example test proves one path does not produce a PASS. This proves there is no path: `status` is a
    property returning a constant, the dataclass is frozen, and the value it returns is not a decision by
    the engine's own definition. Together those close the question rather than sampling it.
    """
    for cause, refusal in _every_cause().items():
        status = str(refusal.status)
        assert status != Outcome.PASS.value, f"{cause} produced a PASS"
        assert status not in {
            outcome.value for outcome in DECISIVE_OUTCOMES
        }, f"{cause} produced a decisive outcome; an unresolved sheet must never decide"

    # And no `Unresolved` can be constructed that says otherwise, asserted structurally rather than by
    # catching an exception.
    #
    # **The first version caught `(FrozenInstanceError, AttributeError)` and failed on CI.** Assigning to
    # a property on a frozen slots dataclass raises `FrozenInstanceError` on Python 3.14 and `TypeError`
    # on 3.12 — and CI runs 3.12 while this machine runs 3.14. A test that asserts *which* exception a
    # language version happens to raise is testing the interpreter; what matters is that `status` is
    # derived and the class is closed, and both of those are checkable directly.
    from dataclasses import FrozenInstanceError

    refusal = Unresolved("A-101", NO_RECORDED_ORDER, "detail")

    assert (
        "status" not in Unresolved.__dataclass_fields__
    ), "status became a field, so it can be constructed with any value"
    assert isinstance(
        getattr(Unresolved, "status", None), property
    ), "status is no longer a derived property"
    assert Unresolved.__dataclass_params__.frozen, "Unresolved is no longer frozen"

    # A real field, to show the frozen-ness is not theoretical.
    with pytest.raises(FrozenInstanceError):
        refusal.cause = "something_else"  # type: ignore[misc]


def test_a_resolved_sheet_is_not_review_required() -> None:
    """The other side, or `REVIEW_REQUIRED` everywhere would satisfy every test above and be useless."""
    outcome = governing_revision([_with_history(0, "A"), _with_history(1, "A", "B")])

    assert isinstance(outcome, GoverningRevision)
    assert outcome.status is SupersessionStatus.RESOLVED
    assert str(outcome.status) != Outcome.REVIEW_REQUIRED.value


# ---------------------------------------------------------------------------
# It never resolves to a convention
# ---------------------------------------------------------------------------


def test_it_does_not_resolve_to_the_last_page_wins() -> None:
    """§7, quoted. The later page is `A` here, so "last page wins" would pick the earlier revision."""
    outcome = governing_revision([_labelled(0, "C"), _labelled(1, "A")])

    assert isinstance(outcome, Unresolved)
    assert outcome.status is SupersessionStatus.REVIEW_REQUIRED


def test_it_does_not_resolve_to_the_highest_letter_wins() -> None:
    """§7, quoted. `C` is the obvious answer to a human and is still not established by any history."""
    outcome = governing_revision([_labelled(0, "A"), _labelled(1, "C")])

    assert isinstance(outcome, Unresolved)
    assert "highest letter wins" in outcome.detail


# ---------------------------------------------------------------------------
# The trace settles it in seconds
# ---------------------------------------------------------------------------


def test_the_trace_names_every_competing_revision() -> None:
    """The third criterion. A reviewer opening this needs the shortlist, not a message about a gap."""
    refusal = _every_cause()[NO_RECORDED_ORDER]
    trace = json.loads(refusal.trace())

    printed = [entry["revision_as_printed"] for entry in trace["competing"]]
    assert sorted(printed) == ["A", "C"], f"the competing revisions are not both named: {printed}"
    assert [entry["page_index"] for entry in trace["competing"]] == [0, 1], "and which page to open"
    assert trace["cause"] == NO_RECORDED_ORDER
    assert trace["status"] == "REVIEW_REQUIRED"
    assert trace["sheet_number"] == "A-101"


def test_the_trace_shows_a_date_as_printed_with_its_readings() -> None:
    """A reviewer settling a supersession needs the thing on the paper, not one reading of it.

    `03/04/26` is shown as `03/04/26`, with both interpretations beside it — presenting either one as the
    date would be this module quietly making the decision it refused to make.
    """
    ambiguous = RevisionDate("03/04/26", (date(2026, 3, 4), date(2026, 4, 3)), century_assumed=True)
    pages = [
        SheetPage(_page(0), RevisionBlock(current=RevisionLabel("A"))),
        SheetPage(_page(1), RevisionBlock(current=RevisionLabel("C", ambiguous))),
    ]

    outcome = governing_revision(pages)
    assert isinstance(outcome, Unresolved)
    entry = next(e for e in json.loads(outcome.trace())["competing"] if e["page_index"] == 1)

    assert entry["date_as_printed"] == "03/04/26"
    assert entry["date_readings"] == ["2026-03-04", "2026-04-03"]


def test_the_trace_is_byte_identical_for_the_same_refusal() -> None:
    """A stored trace has to be comparable with a recomputed one.

    Sorted keys, and candidates ordered by page index rather than by argument order — so a caller passing
    the same pages in a different sequence produces the same trace. Without that, "the trace changed"
    would sometimes mean nothing at all.
    """
    first = governing_revision([_labelled(0, "A"), _labelled(1, "C")])
    shuffled = governing_revision([_labelled(1, "C"), _labelled(0, "A")])

    assert isinstance(first, Unresolved) and isinstance(shuffled, Unresolved)
    assert (
        first.trace() == shuffled.trace()
    ), "the trace depends on the order the caller passed pages in"


def test_the_trace_carries_no_drawing_content() -> None:
    """`AGENTS.md` §6: references and hashes only, never crops or page images.

    A trace is a log with an audience, and this one is written to be read by a person — which is exactly
    the kind of place a helpful `page_bytes` field gets added later.
    """
    refusal = _every_cause()[NO_RECORDED_ORDER]
    trace = refusal.trace()

    for marker in ("%PDF", "\\u0089PNG", "data:image", ";base64", "rgb_bytes", "content"):
        assert marker not in trace, f"{marker} appears in the refusal trace"

    entry_keys = set(json.loads(trace)["competing"][0])
    assert entry_keys == {
        "page_index",
        "revision_as_printed",
        "revision_normalised",
        "date_as_printed",
        "date_readings",
    }, f"the trace grew a field: {entry_keys}"


def test_the_trace_is_valid_json_for_every_cause() -> None:
    """Including the one with no sheet number, where `sheet_number` is null.

    A trace that failed to serialise for one cause would take the refusal down with it, turning a
    fail-closed path into an error — and an error is not REVIEW REQUIRED.
    """
    for cause, refusal in _every_cause().items():
        decoded = json.loads(refusal.trace())
        assert decoded["cause"] == cause
        assert decoded["status"] == "REVIEW_REQUIRED"
        assert isinstance(decoded["competing"], list)


def test_the_module_still_does_not_import_the_engine() -> None:
    """The asymmetry this file relies on, asserted rather than assumed.

    This test imports `verdict/`; `extraction/supersession.py` must not. If that ever changes, the two
    vocabularies stop being independent and this file's central claim — that they agree by check rather
    than by construction — becomes circular.
    """
    import ast
    from pathlib import Path

    import extraction.supersession as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "verdict" not in imported, (
        "extraction/supersession.py now imports verdict/, so this file's comparison of the two "
        "vocabularies no longer proves anything"
    )
