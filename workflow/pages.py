"""Fanning the workflow out over the page manifest, and joining it back (#163, B6.4).

`docs/DESIGN_EXTRACTION.md` §3.1: the manifest is the unit of work. One durable task per page, joined into
a package-level result.

**One unreadable page must not fail the package, and must not be forgotten either.** Those two pull in
opposite directions and both matter. A vendor package is dozens of pages; if page 14 will not render, the
other 27 are still worth reading — but a package that quietly reports on 27 of 28 pages is the failure
`AGENTS.md` §2.2 exists to prevent, because nothing in the output says what was missed. So a failed page is
recorded, the rest continue, and the join refuses to call the result complete.

**The join is ordered by page, never by completion.** Tasks finish in whatever order the engine schedules
them, and a result whose order depended on that would differ between two identical runs — which makes a
re-run impossible to compare against the first.

The ordering guarantee starts one level up: `PageManifest` already refuses pages that are not `0, 1, 2 …`
in order with none missing. So iterating the manifest *is* page order, and `FanOutResult` refuses
out-of-order outcomes as well — because a caller assembling them itself would otherwise be able to append
in completion order. I had a `sorted()` here too and removed it: with the manifest's invariant in place it
could never change anything, and a guard that cannot fire reads as protection while providing none.

**Keys come from `workflow/idempotency.py`, not from a third key function.** `idempotency_key` already
takes exactly the components `AGENTS.md` §2.7 names — document version, region, task type, extractor
version, config — and the region is the page. A second key function with its own canonicaliser is how two
keys come to disagree about whether `Decimal("1.0")` and `Decimal("1.00")` are the same thing.

**`execution_timeout` is a required argument with no default, and that is the honest answer to a criterion
I cannot satisfy.** The issue says to *measure* the timeout and set it, because hatchet-sdk defaults to 60
seconds — fine while stages do nothing, wrong the moment a page is rendered and OCR'd, and a task killed
mid-page looks like a broken document rather than a budget. But the measurement needs a real drawing set,
and there is none (#274); §9 is explicit that a fixture invented today encodes today's guess as ground
truth. So this refuses to *inherit* the default — no caller can get 60 seconds by accident — while
declining to invent the number. The duration is the caller's to state and the admin's to decide once pages
can be timed.

Source: backend proposal §9.2 · Design: `docs/DESIGN_EXTRACTION.md` §3.1 ·
Verification: `tests/workflow/test_page_fanout.py`
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final
from uuid import UUID

from workflow.idempotency import idempotency_key

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from extraction.manifest import PageManifest, PageRecord

__all__ = [
    "PAGE_TASK_TYPE",
    "FanOutResult",
    "PageFanOutStatus",
    "PageOutcome",
    "PageTask",
    "fan_out",
    "page_tasks",
]

#: The task type that goes inside a page task's idempotency key.
#:
#: One constant rather than a literal at each call site: the key includes it, so two spellings would make
#: the same work look like two different tasks and neither would recognise the other's result.
PAGE_TASK_TYPE: Final = "page_read"


class PageFanOutStatus(StrEnum):
    """Whether every page was read, or a reviewer has to look.

    `REVIEW_REQUIRED` is spelled out here rather than imported from `verdict/`, following the precedent
    `evidence/crop.py` and `extraction/supersession.py` set: the string must equal
    `verdict.outcomes.Outcome.REVIEW_REQUIRED` and a test asserts it, but this module holding a dependency
    on the engine is a different thing from agreeing with it.

    There is deliberately no `PASS` member. This module cannot pass anything — it reports whether the
    reading was complete, and the verdict is computed elsewhere from what it read.
    """

    COMPLETE = "COMPLETE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class PageTask:
    """One page's task: which page, and the key that makes re-running it recognisable."""

    page_index: int
    key: str
    execution_timeout_seconds: int
    """Explicit, always. See the module docstring: inheriting hatchet-sdk's 60-second default would kill
    a task mid-page and report it as a broken document."""


@dataclass(frozen=True, slots=True)
class PageOutcome:
    """What happened to one page."""

    page_index: int
    key: str
    succeeded: bool
    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError(
                "every page outcome says something. A failure with no detail is a gap a reviewer cannot "
                "act on, and a success with no detail makes the two indistinguishable in a log."
            )


@dataclass(frozen=True, slots=True)
class FanOutResult:
    """Every page's outcome, in page order, and whether the package was fully read."""

    document_version_id: UUID
    outcomes: tuple[PageOutcome, ...]

    def __post_init__(self) -> None:
        indices = [outcome.page_index for outcome in self.outcomes]
        if indices != sorted(indices):
            raise ValueError(
                "outcomes must be in page order. Ordering by completion makes two identical runs produce "
                "different results, and then a re-run cannot be compared with the first."
            )
        if len(set(indices)) != len(indices):
            raise ValueError("a page appears twice in the outcomes")

    @property
    def failed(self) -> tuple[PageOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.succeeded)

    @property
    def status(self) -> PageFanOutStatus:
        """`COMPLETE` only when every page was read.

        A property with no setter and no argument: there is no way to construct a result that claims
        completeness while carrying a failure.
        """
        return PageFanOutStatus.COMPLETE if not self.failed else PageFanOutStatus.REVIEW_REQUIRED

    @property
    def gap(self) -> str | None:
        """What was missed, in plain English, or `None` when nothing was.

        *"Surfaces the gap"* is the acceptance criterion, and this is it. A package that read 27 of 28
        pages must say which one it did not, because a report that simply omits it looks complete.
        """
        if not self.failed:
            return None
        pages = ", ".join(str(outcome.page_index) for outcome in self.failed)
        return (
            f"{len(self.failed)} of {len(self.outcomes)} page(s) could not be read: {pages}. "
            "Every finding drawn from this package is missing whatever those pages say, so the package "
            "cannot be reported as fully checked."
        )


def page_tasks(
    manifest: PageManifest,
    *,
    extractor_version: str,
    config: Mapping[str, object],
    execution_timeout_seconds: int,
) -> tuple[PageTask, ...]:
    """One task per page, in page order, with a stable key each.

    Re-running this against the same manifest produces the same keys — which is what makes the fan-out
    idempotent (C4). The keys come from `workflow/idempotency.py`, so the extractor version and config are
    inside them: a changed reader is a different task, not a cache hit.

    `execution_timeout_seconds` is required and validated. There is no default to inherit.
    """
    if execution_timeout_seconds <= 0:
        raise ValueError(
            "execution_timeout_seconds must be positive. It has no default on purpose: hatchet-sdk's own "
            "default is 60 seconds, which would kill a page task mid-render and report it as a broken "
            "document rather than a budget."
        )

    return tuple(
        PageTask(
            page_index=page.index,
            key=idempotency_key(
                document_version_id=manifest.document_version_id,
                # The page is the region. `idempotency_key` already names this component, so a page task
                # needs no new key function.
                region=str(page.index),
                task_type=PAGE_TASK_TYPE,
                extractor_version=extractor_version,
                config=config,
            ),
            execution_timeout_seconds=execution_timeout_seconds,
        )
        # The manifest guarantees page order; see the module docstring.
        for page in manifest.pages
    )


def fan_out(
    manifest: PageManifest,
    run_page: Callable[[PageRecord, PageTask], None],
    *,
    extractor_version: str,
    config: Mapping[str, object],
    execution_timeout_seconds: int,
) -> FanOutResult:
    """Run `run_page` for every page, isolate failures, and join in page order.

    `run_page` is injected rather than imported, for the reason `workflow/outbox.py` injects its starter:
    this module holds no opinion about what reading a page means, and a test can make one page fail on
    demand without an engine or a PDF.

    **An exception from one page is recorded and the loop continues.** Anything else means one unreadable
    page discards the reading of every page after it — and which pages those were would depend on the
    order they happened to run in.
    """
    tasks = {
        task.page_index: task
        for task in page_tasks(
            manifest,
            extractor_version=extractor_version,
            config=config,
            execution_timeout_seconds=execution_timeout_seconds,
        )
    }

    outcomes: list[PageOutcome] = []
    for page in manifest.pages:
        task = tasks[page.index]
        try:
            run_page(page, task)
        except Exception as failure:  # noqa: BLE001 - recorded per page, never swallowed silently
            outcomes.append(
                PageOutcome(
                    page_index=page.index,
                    key=task.key,
                    succeeded=False,
                    detail=(
                        f"page {page.index} could not be read: {type(failure).__name__}: {failure}. "
                        "The other pages were still read; this page's content is missing from the "
                        "package."
                    ),
                )
            )
        else:
            outcomes.append(
                PageOutcome(
                    page_index=page.index,
                    key=task.key,
                    succeeded=True,
                    detail=f"page {page.index} read",
                )
            )

    return FanOutResult(manifest.document_version_id, tuple(outcomes))
