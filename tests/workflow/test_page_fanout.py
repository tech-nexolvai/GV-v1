"""Fanning out over the manifest, and joining back (#163, B6.4).

Two requirements pull against each other and both matter: one unreadable page must not fail the package,
and must not be forgotten either. A package that quietly reports on 27 of 28 pages is the failure
`AGENTS.md` §2.2 exists to prevent — nothing in the output says what was missed.

So the tests are weighted to the join: what happens to the other pages when one fails, and whether the
result can be mistaken for a complete reading.

No engine and no PDFs — `run_page` is injected, so a page can be made to fail on demand.

Source: `docs/DESIGN_EXTRACTION.md` §3.1 · Verification: this file
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from extraction.manifest import PageManifest, PageRecord
from workflow.pages import (
    PAGE_TASK_TYPE,
    FanOutResult,
    PageFanOutStatus,
    PageOutcome,
    fan_out,
    page_tasks,
)

DOC = UUID("11111111-1111-1111-1111-111111111111")
TIMEOUT = 600
CONFIG: dict[str, object] = {"dpi": 300}


def _page(index: int) -> PageRecord:
    return PageRecord(
        index=index,
        content_hash=hashlib.sha256(f"page-{index}".encode()).hexdigest(),
        width_pt=Decimal(842),
        height_pt=Decimal(595),
        rotation=0,
        has_vector_text=True,
        render_failed=False,
    )


def _manifest(count: int = 3, document: UUID = DOC) -> PageManifest:
    return PageManifest(document_version_id=document, pages=tuple(_page(i) for i in range(count)))


def _fan(manifest: PageManifest, run_page) -> FanOutResult:  # type: ignore[no-untyped-def]
    return fan_out(
        manifest,
        run_page,
        extractor_version="reader-1.0.0",
        config=CONFIG,
        execution_timeout_seconds=TIMEOUT,
    )


# ---------------------------------------------------------------------------
# One task per page, and a failure isolates
# ---------------------------------------------------------------------------


def test_one_task_is_created_per_page() -> None:
    tasks = page_tasks(
        _manifest(4),
        extractor_version="reader-1.0.0",
        config=CONFIG,
        execution_timeout_seconds=TIMEOUT,
    )

    assert [task.page_index for task in tasks] == [0, 1, 2, 3]
    assert len({task.key for task in tasks}) == 4, "each page gets its own key"


def test_a_failing_page_is_recorded_and_the_others_continue() -> None:
    """The first acceptance criterion.

    A vendor package is dozens of pages. If page 1 will not render, the other pages are still worth
    reading — and the failure has to be visible rather than absorbed.
    """
    read: list[int] = []

    def run_page(page: PageRecord, task: object) -> None:
        if page.index == 1:
            raise RuntimeError("render failed")
        read.append(page.index)

    result = _fan(_manifest(4), run_page)

    assert read == [0, 2, 3], "the pages after the failure were still read"
    assert [outcome.page_index for outcome in result.failed] == [1]
    assert "render failed" in result.failed[0].detail


def test_a_failure_on_the_first_page_does_not_stop_the_rest() -> None:
    """Tested separately, because "continue after a failure" is easy to get right only mid-list."""
    read: list[int] = []

    def run_page(page: PageRecord, task: object) -> None:
        if page.index == 0:
            raise RuntimeError("first page unreadable")
        read.append(page.index)

    result = _fan(_manifest(3), run_page)

    assert read == [1, 2]
    assert len(result.failed) == 1


def test_every_page_failing_is_still_a_result_rather_than_an_exception() -> None:
    """A package where nothing could be read is a reportable outcome, not a crash.

    Raising here would lose the record of *which* pages failed and why, which is the only useful thing
    left in that situation.
    """

    def run_page(page: PageRecord, task: object) -> None:
        raise RuntimeError("nothing readable")

    result = _fan(_manifest(3), run_page)

    assert len(result.failed) == 3
    assert result.status is PageFanOutStatus.REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# The join is ordered by page, not by completion
# ---------------------------------------------------------------------------


def test_the_ordering_guarantee_starts_at_the_manifest() -> None:
    """The second acceptance criterion, and it is enforced one level up.

    My first version of this test built a manifest with pages out of order and claimed `PageManifest`
    permitted it. It does not — *"page records must run 0, 1, 2 … in order with none missing: a gap or a
    reorder means a page was dropped or renumbered, and this is the only place that can notice."* The
    manifest was right and my comment was wrong.

    So iterating the manifest *is* page order, and this asserts the guarantee at its source rather than
    re-testing it downstream. `workflow/pages.py` had a defensive `sorted()` which I removed: with this
    invariant in place it could never change anything.
    """
    with pytest.raises(ValueError, match="in order with none missing"):
        PageManifest(document_version_id=DOC, pages=(_page(2), _page(0), _page(1)))

    ordered = _fan(_manifest(3), lambda page, task: None)
    assert [outcome.page_index for outcome in ordered.outcomes] == [0, 1, 2]


def test_a_result_built_out_of_order_is_refused() -> None:
    """The invariant, not just the code path that happens to satisfy it.

    A caller assembling outcomes itself must not be able to produce a completion-ordered result.
    """
    with pytest.raises(ValueError, match="page order"):
        FanOutResult(
            DOC,
            (
                PageOutcome(1, "sha256:b", True, "read"),
                PageOutcome(0, "sha256:a", True, "read"),
            ),
        )


def test_a_page_appearing_twice_is_refused() -> None:
    """Two outcomes for one page would make the counts in `gap` wrong."""
    with pytest.raises(ValueError, match="twice"):
        FanOutResult(
            DOC,
            (PageOutcome(0, "sha256:a", True, "read"), PageOutcome(0, "sha256:a", False, "no")),
        )


# ---------------------------------------------------------------------------
# A package with a failed page cannot look clean
# ---------------------------------------------------------------------------


def test_any_failure_makes_the_status_review_required() -> None:
    """The third acceptance criterion."""

    def run_page(page: PageRecord, task: object) -> None:
        if page.index == 2:
            raise RuntimeError("unreadable")

    result = _fan(_manifest(3), run_page)
    assert result.status is PageFanOutStatus.REVIEW_REQUIRED


def test_a_fully_read_package_is_complete() -> None:
    """The other side, or `REVIEW_REQUIRED` everywhere would satisfy the test above and mean nothing."""
    result = _fan(_manifest(3), lambda page, task: None)

    assert result.status is PageFanOutStatus.COMPLETE
    assert result.failed == ()
    assert result.gap is None


def test_there_is_no_pass_this_module_can_produce() -> None:
    """This module reports whether the reading was complete; the verdict is computed from what it read.

    A `PASS` member here would let a fan-out result be mistaken for a verdict — and a package that read 27
    of 28 pages could then carry one.
    """
    assert "PASS" not in {member.name for member in PageFanOutStatus}
    assert not any(member.value == "PASS" for member in PageFanOutStatus)


def test_the_status_string_matches_the_engines_vocabulary() -> None:
    """Written separately from `verdict/` and pinned here, as in #185.

    A status the engine does not recognise would make an incomplete package unrepresentable in a finding.
    """
    from verdict.outcomes import ABSTAINING_OUTCOMES, Outcome, is_decision

    assert str(PageFanOutStatus.REVIEW_REQUIRED) == Outcome.REVIEW_REQUIRED.value
    assert Outcome.REVIEW_REQUIRED in ABSTAINING_OUTCOMES
    assert not is_decision(Outcome.REVIEW_REQUIRED)


def test_the_gap_names_the_pages_that_were_missed() -> None:
    """*"Surfaces the gap"* — a report that simply omits page 14 looks complete."""

    def run_page(page: PageRecord, task: object) -> None:
        if page.index in {1, 3}:
            raise RuntimeError("unreadable")

    result = _fan(_manifest(5), run_page)
    gap = result.gap

    assert gap is not None
    assert "2 of 5" in gap
    assert "1, 3" in gap, "which pages, not just how many"
    assert "cannot be reported as fully checked" in gap


def test_every_outcome_says_something() -> None:
    """A failure with no detail is a gap nobody can act on; a success with none is indistinguishable."""
    with pytest.raises(ValueError, match="says something"):
        PageOutcome(0, "sha256:a", True, "   ")


# ---------------------------------------------------------------------------
# Idempotent keys
# ---------------------------------------------------------------------------


def test_re_running_produces_the_same_keys() -> None:
    """The fourth acceptance criterion (C4). A retry has to recognise itself."""
    first = page_tasks(
        _manifest(3), extractor_version="r-1", config=CONFIG, execution_timeout_seconds=TIMEOUT
    )
    again = page_tasks(
        _manifest(3), extractor_version="r-1", config=CONFIG, execution_timeout_seconds=TIMEOUT
    )

    assert [t.key for t in first] == [t.key for t in again]


def test_a_changed_extractor_version_changes_every_key() -> None:
    """A changed reader is a different task, not a cache hit — `AGENTS.md` §2.7."""
    old = page_tasks(
        _manifest(3), extractor_version="r-1", config=CONFIG, execution_timeout_seconds=TIMEOUT
    )
    new = page_tasks(
        _manifest(3), extractor_version="r-2", config=CONFIG, execution_timeout_seconds=TIMEOUT
    )

    assert {t.key for t in old}.isdisjoint({t.key for t in new})


def test_a_changed_config_changes_every_key() -> None:
    """Rendering at 300 dpi and at 150 dpi are different reads of the same page."""
    coarse = page_tasks(
        _manifest(2),
        extractor_version="r-1",
        config={"dpi": 150},
        execution_timeout_seconds=TIMEOUT,
    )
    fine = page_tasks(
        _manifest(2),
        extractor_version="r-1",
        config={"dpi": 300},
        execution_timeout_seconds=TIMEOUT,
    )

    assert {t.key for t in coarse}.isdisjoint({t.key for t in fine})


def test_two_documents_do_not_share_page_keys() -> None:
    """Page 0 of one drawing is not page 0 of another."""
    ours = page_tasks(
        _manifest(2, DOC), extractor_version="r-1", config=CONFIG, execution_timeout_seconds=TIMEOUT
    )
    theirs = page_tasks(
        _manifest(2, uuid4()),
        extractor_version="r-1",
        config=CONFIG,
        execution_timeout_seconds=TIMEOUT,
    )

    assert {t.key for t in ours}.isdisjoint({t.key for t in theirs})


def test_the_key_comes_from_the_shared_key_function() -> None:
    """Not a third key function with its own canonicaliser.

    Two canonicalisers is how two keys come to disagree about whether `Decimal("1.0")` and
    `Decimal("1.00")` are the same thing — `workflow/idempotency.py` says so about the two it already has.
    """
    from workflow.idempotency import idempotency_key

    expected = idempotency_key(
        document_version_id=DOC,
        region="0",
        task_type=PAGE_TASK_TYPE,
        extractor_version="r-1",
        config=CONFIG,
    )
    tasks = page_tasks(
        _manifest(1), extractor_version="r-1", config=CONFIG, execution_timeout_seconds=TIMEOUT
    )

    assert tasks[0].key == expected


# ---------------------------------------------------------------------------
# The timeout is stated, never inherited
# ---------------------------------------------------------------------------


def test_every_page_task_carries_an_explicit_timeout() -> None:
    """The fifth acceptance criterion, in the part I can actually satisfy.

    hatchet-sdk defaults `execution_timeout` to 60 seconds — fine while stages do nothing, wrong the
    moment a page is rendered and OCR'd, and a task killed mid-page looks like a broken document rather
    than a budget. So no page task can be built without one.
    """
    tasks = page_tasks(
        _manifest(3), extractor_version="r-1", config=CONFIG, execution_timeout_seconds=900
    )
    assert all(task.execution_timeout_seconds == 900 for task in tasks)


def test_there_is_no_default_timeout_to_inherit() -> None:
    """**The criterion asked me to measure the duration, and I cannot.**

    Measuring it needs a real drawing set (#274), and §9 says a fixture invented today encodes today's
    guess as ground truth. So the argument is required rather than defaulted: nobody gets 60 seconds by
    accident, and the number stays the caller's to state until it can be measured.

    Asserted against the signature, so adding a default later fails here.
    """
    import inspect

    parameter = inspect.signature(page_tasks).parameters["execution_timeout_seconds"]
    assert (
        parameter.default is inspect.Parameter.empty
    ), "execution_timeout_seconds acquired a default, which is a measurement nobody has taken"
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("bad", [0, -1, -600])
def test_a_non_positive_timeout_is_refused(bad: int) -> None:
    """Zero would mean "no time at all", which reads as a broken document on every page."""
    with pytest.raises(ValueError, match="must be positive"):
        page_tasks(
            _manifest(1), extractor_version="r-1", config=CONFIG, execution_timeout_seconds=bad
        )


def test_this_module_does_not_import_the_verdict_engine() -> None:
    """The isolation guard. This module's status string agrees with `verdict/` by test, not by import."""
    import ast
    from pathlib import Path

    import workflow.pages as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "verdict" not in imported
