"""Run the form-first reader pair and attach display-only, Qwen-sourced locations.

This bridge intentionally returns no field keys and performs no persistence. A caller must use the
existing reviewer-owned mapping/proposal flow before a reading can fill an operand-bearing form
field. The page location only decides what picture to show a person.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from evidence.coordinates import PageTransform
from evidence.polygon import Polygon
from extraction.form_reader.agreement import ComparedReading, compare_page_answers
from extraction.form_reader.bedrock import AttemptUsage
from extraction.form_reader.locator import LocatedBox, locate_box
from extraction.form_reader.pricing import RateLookup
from extraction.form_reader.runner import ClientProvider, read_pages_parallel
from extraction.form_reader.schema import PageFormAnswer


@dataclass(frozen=True, slots=True)
class FormPageImage:
    """A rendered vendor-only page plus the measured transform and safe snap candidates."""

    page_index: int
    png: bytes
    width_px: int
    height_px: int
    document_version_id: UUID
    transform: PageTransform
    regions: tuple[tuple[str, Polygon], ...]


@dataclass(frozen=True, slots=True)
class LocatedReading:
    """A two-reader comparison and optional Qwen display placement, never a semantic operand."""

    comparison: ComparedReading
    location: LocatedBox | None


def read_form_pages(
    pages: Sequence[FormPageImage],
    *,
    reader_ids: tuple[str, str],
    clients: ClientProvider,
    rates: RateLookup | None,
    calls_per_minute: Mapping[str, int],
    max_concurrent_calls: int,
    max_tokens: int,
    max_throttle_retries: int,
    retry_backoff_seconds: float,
    record_attempt: Callable[[AttemptUsage], None],
) -> tuple[LocatedReading, ...]:
    """Read all supplied pages in parallel, apply outputs in fixed order, and locate Qwen boxes.

    Page indices are internal zero-based indices. Reader/model metadata is request-owned, not echoed
    from either response. Geometry is validated against each page's stored transform before a box
    can be returned.
    """
    page_by_index = {page.page_index: page for page in pages}
    if len(page_by_index) != len(pages):
        raise ValueError("form page indices must be unique")
    reads = read_pages_parallel(
        tuple((page.page_index, page.png) for page in pages),
        reader_ids=reader_ids,
        clients=clients,
        rates=rates,
        calls_per_minute=calls_per_minute,
        max_concurrent_calls=max_concurrent_calls,
        max_tokens=max_tokens,
        max_throttle_retries=max_throttle_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        record_attempt=record_attempt,
    )
    answers: dict[int, dict[str, PageFormAnswer]] = defaultdict(dict)
    for read in reads:
        answers[read.page_index][read.model_id] = read.answer

    result: list[LocatedReading] = []
    for page_index in sorted(page_by_index):
        page = page_by_index[page_index]
        per_model = answers[page_index]
        first = per_model[reader_ids[0]]
        second = per_model[reader_ids[1]]
        comparisons = compare_page_answers(
            first,
            second,
            first_maker=reader_ids[0],
            second_maker=reader_ids[1],
        )
        for comparison in comparisons:
            dimension = comparison.qwen_dimension
            location = None
            if dimension is not None and dimension.box is not None:
                location = locate_box(
                    dimension.box,
                    width_px=page.width_px,
                    height_px=page.height_px,
                    transform=page.transform,
                    document_version_id=page.document_version_id,
                    page_index=page.page_index,
                    regions=page.regions,
                )
            result.append(LocatedReading(comparison=comparison, location=location))
    return tuple(result)
