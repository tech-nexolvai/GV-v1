"""Run the form-first reader pair and attach display-only, Qwen-sourced locations.

This bridge intentionally returns no field keys and performs no persistence. A caller must use the
existing reviewer-owned mapping/proposal flow before a reading can fill an operand-bearing form
field. The page location only decides what picture to show a person.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from uuid import UUID, uuid4

from app.config import Settings
from app.runs.rates import ModelRates
from evidence.coordinates import PageTransform
from evidence.polygon import Polygon
from extraction.form_reader.agreement import ComparedReading, compare_page_answers
from extraction.form_reader.bedrock import AttemptUsage
from extraction.form_reader.locator import LocatedBox, locate_box
from extraction.form_reader.mapping import FormMapping, map_page_to_fields
from extraction.form_reader.prompt_v5 import BUILT_IN_PROMPT, FormPrompt, prompt_from_guidance_file
from extraction.form_reader.runner import ClientProvider, read_pages_parallel
from extraction.form_reader.schema import PageFormAnswer


@dataclass(frozen=True, slots=True)
class FormReaderRuntime:
    reader_ids: tuple[str, str]
    clients: ClientProvider
    rates: ModelRates
    calls_per_minute: Mapping[str, int]
    max_concurrent_calls: int
    max_tokens: int
    max_throttle_retries: int
    retry_backoff_seconds: float
    prompt: FormPrompt


def configured_form_reader(settings: Settings) -> FormReaderRuntime | None:
    """Build the opt-in, fully priced worker runtime without creating a client when disabled."""
    if not bool(getattr(settings, "form_reader_enabled", False)):
        return None
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    from app.runs.rates import rates_from_environment
    from extraction.form_reader.pricing import require_priced_readers
    from extraction.form_reader.runner import ThreadLocalConverseClients

    readers = (
        str(settings.form_reader_primary_model),
        str(settings.form_reader_second_model),
    )
    prompt_file = getattr(settings, "form_reader_prompt_file", None)
    if prompt_file is None:
        raise ValueError("enabled form readers require GV_FORM_READER_PROMPT_FILE")
    prompt = prompt_from_guidance_file(prompt_file)
    rates = rates_from_environment()
    require_priced_readers(readers, rates)
    if rates is None:
        raise ValueError("enabled form readers require a priced model-rates file")
    region = str(settings.bedrock_region)
    connect_timeout = int(settings.bedrock_connect_timeout)
    read_timeout = int(settings.bedrock_read_timeout)
    provider = ThreadLocalConverseClients(
        lambda: boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(connect_timeout=connect_timeout, read_timeout=read_timeout),
        )
    )
    concurrent = settings.form_reader_max_concurrent_calls
    max_tokens = settings.form_reader_max_tokens
    retries = settings.form_reader_max_throttle_retries
    backoff = settings.form_reader_retry_backoff_seconds
    if concurrent is None or max_tokens is None or retries is None or backoff is None:
        raise ValueError(
            "enabled form readers require explicit concurrency, token and retry bounds"
        )
    return FormReaderRuntime(
        reader_ids=readers,
        clients=provider,
        rates=rates,
        calls_per_minute=dict(settings.form_reader_model_rpm),
        max_concurrent_calls=int(concurrent),
        max_tokens=int(max_tokens),
        max_throttle_retries=int(retries),
        retry_backoff_seconds=float(backoff),
        prompt=prompt,
    )


@dataclass(frozen=True, slots=True)
class FormPageImage:
    """A rendered vendor-only page plus the measured transform and safe snap candidates."""

    page_index: int
    png: bytes
    width_px: int
    height_px: int
    document_version_id: UUID
    page_id: UUID
    transform: PageTransform
    regions: tuple[tuple[str, Polygon], ...]
    gv_mark_checker: Callable[[Polygon], bool | None] | None = None
    """The existing page-mark detector for a located display crop; None means it could not check."""


@dataclass(frozen=True, slots=True)
class LocatedReading:
    """A two-reader comparison and optional Qwen display placement, never a semantic operand."""

    comparison: ComparedReading
    location: LocatedBox | None
    first_countertop_count: int
    second_countertop_count: int
    document_version_id: UUID
    page_id: UUID
    image_polygon: tuple[tuple[int, int], ...]


def guard_located_comparison(
    comparison: ComparedReading,
    location: LocatedBox | None,
    mark_checker: Callable[[Polygon], bool | None] | None,
) -> ComparedReading:
    """Keep a numeric agreement review-only unless its located label is proved free of GV marks."""
    if comparison.state != "corroborated":
        return comparison
    if location is None:
        return replace(
            comparison,
            state="review_required",
            value=None,
            reason="label-location-unknown",
        )
    if mark_checker is None:
        return replace(
            comparison,
            state="review_required",
            value=None,
            reason="gv-mark-unchecked",
        )
    try:
        marked = mark_checker(location.polygon)
    except (ArithmeticError, TypeError, ValueError):
        marked = None
    if marked is True:
        return replace(
            comparison,
            state="review_required",
            value=None,
            reason="gv-mark",
        )
    if marked is None:
        return replace(
            comparison,
            state="review_required",
            value=None,
            reason="gv-mark-unchecked",
        )
    return comparison


def read_form_pages(
    pages: Sequence[FormPageImage],
    *,
    reader_ids: tuple[str, str],
    clients: ClientProvider,
    rates: ModelRates | None,
    calls_per_minute: Mapping[str, int],
    max_concurrent_calls: int,
    max_tokens: int,
    max_throttle_retries: int,
    retry_backoff_seconds: float,
    record_attempt: Callable[[AttemptUsage], None],
    prompt: FormPrompt = BUILT_IN_PROMPT,
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
        prompt=prompt,
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
            comparison = guard_located_comparison(
                comparison,
                location,
                page.gv_mark_checker,
            )
            result.append(
                LocatedReading(
                    comparison=comparison,
                    location=location,
                    first_countertop_count=len(first.countertops),
                    second_countertop_count=len(second.countertops),
                    document_version_id=page.document_version_id,
                    page_id=page.page_id,
                    image_polygon=(
                        ()
                        if location is None
                        else tuple(
                            (point.x, point.y)
                            for point in (
                                page.transform.from_stored(stored)
                                for stored in location.polygon.points
                            )
                        )
                    ),
                )
            )
    return tuple(result)


def persist_form_proposals(
    session: object,
    *,
    package_revision_id: UUID,
    extraction_run_id: UUID,
    reader_ids: tuple[str, str],
    readings: Sequence[LocatedReading],
    prompt_id: str,
) -> int:
    """Persist candidates and candidate-only MeasurementProposal links; never save form values."""
    from sqlalchemy.orm import Session

    from app.models.document import Page as PageModel
    from app.models.evidence import MeasurementProposal, ObservationCandidate
    from evidence.canonical import CorroborationLane
    from extraction.form_reader.parser import parse_dimension
    from units.measurement import Unit

    if not isinstance(session, Session):
        raise TypeError("session must be a SQLAlchemy Session")
    grouped: dict[tuple[UUID, UUID], list[LocatedReading]] = defaultdict(list)
    for located in readings:
        grouped[(located.document_version_id, located.page_id)].append(located)
    proposal_rows: list[MeasurementProposal] = []
    candidate_count = 0
    for (_version_id, page_id), page_readings in sorted(
        grouped.items(), key=lambda item: str(item[0][1])
    ):
        first_count = page_readings[0].first_countertop_count
        second_count = page_readings[0].second_countertop_count
        comparisons = tuple(item.comparison for item in page_readings)
        mapping: FormMapping = map_page_to_fields(
            comparisons,
            first_countertop_count=first_count,
            second_countertop_count=second_count,
        )
        mapped = {id(proposal.reading): proposal for proposal in mapping.proposals}
        proposal_id = uuid4()
        page = session.get(PageModel, page_id)
        if page is None:
            raise ValueError("form-reader page disappeared before proposal persistence")
        candidate_by_reading: dict[int, ObservationCandidate] = {}
        for located in page_readings:
            reading = located.comparison
            dimension = reading.qwen_dimension
            raw_text = "" if dimension is None or dimension.text is None else dimension.text
            parsed = parse_dimension(dimension) if dimension is not None else None
            accepted = id(reading) in mapped
            value = reading.value if accepted else (None if parsed is None else parsed.value)
            polygon: list[list[int]] = []
            if located.image_polygon:
                polygon = [list(point) for point in located.image_polygon]
            candidate = ObservationCandidate(
                document_version_id=located.document_version_id,
                page_id=located.page_id,
                extraction_run_id=extraction_run_id,
                raw_text=raw_text,
                value_numerator=None if value is None else value.exact.numerator,
                value_denominator=None if value is None else value.exact.denominator,
                unit=None if value is None else Unit.INCH.value,
                semantic_guess=None,
                polygon=polygon,
                coordinate_space="image",
                confidence=None,
                ambiguity_flags=[],
                corroboration_status="CORROBORATED" if accepted else None,
                corroboration_lane=(CorroborationLane.SECOND_READER.value if accepted else None),
                review_reason=(
                    None
                    if accepted
                    else next(
                        (
                            question.review_reason
                            for question in mapping.questions
                            if question.reading is reading
                        ),
                        "review this reading before assigning it",
                    )
                ),
            )
            session.add(candidate)
            session.flush()
            candidate_count += 1
            candidate_by_reading[id(reading)] = candidate
        page = session.get(PageModel, page_id)
        assert page is not None
        for proposal in mapping.proposals:
            candidate = candidate_by_reading[id(proposal.reading)]
            proposal_rows.append(
                MeasurementProposal(
                    package_revision_id=package_revision_id,
                    page_number=page.index + 1,
                    proposal_id=proposal_id,
                    field_key=proposal.field_key,
                    position=proposal.position,
                    candidate_id=candidate.id,
                    placement_verified=False,
                    model_id=f"{reader_ids[0]} + {reader_ids[1]}",
                    prompt_id=prompt_id,
                )
            )
    session.add_all(proposal_rows)
    session.flush()
    return candidate_count
