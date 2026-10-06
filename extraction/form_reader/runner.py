"""Bounded parallel per-page form reading with per-model pacing and stable result order."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Protocol

from evidence.corroborate import UNKNOWN_MODEL_VENDOR, independence_key
from extraction.form_reader.bedrock import (
    AttemptUsage,
    ConverseClient,
    MalformedFormAnswer,
    read_page,
)
from extraction.form_reader.pricing import RateLookup, require_priced_readers
from extraction.form_reader.prompt_v5 import BUILT_IN_PROMPT, FormPrompt
from extraction.form_reader.schema import PageFormAnswer


class ClientProvider(Protocol):
    def for_current_thread(self) -> ConverseClient: ...


@dataclass(slots=True)
class ThreadSafeAttemptRecorder:
    """Buffer per-call facts from reader threads for persistence on the owning DB thread."""

    _attempts: list[AttemptUsage] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def record(self, attempt: AttemptUsage) -> None:
        with self._lock:
            self._attempts.append(attempt)

    def snapshot(self) -> tuple[AttemptUsage, ...]:
        with self._lock:
            return tuple(self._attempts)


class ThreadLocalConverseClients:
    """Create at most one Bedrock Runtime client per worker thread."""

    def __init__(self, factory: Callable[[], ConverseClient]) -> None:
        self._factory = factory
        self._local = threading.local()

    def for_current_thread(self) -> ConverseClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._factory()
            self._local.client = client
        return client


class ModelPacer:
    """A process-local start-rate limiter, independent for each configured model."""

    def __init__(self, calls_per_minute: Mapping[str, int]) -> None:
        if not calls_per_minute:
            raise ValueError("per-model calls-per-minute limits must be stated")
        if any(
            not model.strip() or isinstance(rpm, bool) or not isinstance(rpm, int) or rpm <= 0
            for model, rpm in calls_per_minute.items()
        ):
            raise ValueError("each model must have a positive whole-number calls-per-minute limit")
        self._intervals = {model: 60.0 / rpm for model, rpm in calls_per_minute.items()}
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, model_id: str) -> None:
        interval = self._intervals.get(model_id)
        if interval is None:
            raise ValueError(f"no pacing limit is configured for reader {model_id!r}")
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next.get(model_id, now))
            self._next[model_id] = start + interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


ABSTAINED_NOTE = "answer malformed after one re-ask; nothing from this reader on this page"


@dataclass(frozen=True, slots=True)
class PageRead:
    page_index: int
    model_id: str
    answer: PageFormAnswer


def _is_throttle(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        error_data = response.get("Error")
        if isinstance(error_data, Mapping):
            code = error_data.get("Code")
            if isinstance(code, str) and code in {
                "ThrottlingException",
                "TooManyRequestsException",
                "RequestThrottledException",
            }:
                return True
    return type(error).__name__ in {
        "ThrottlingException",
        "TooManyRequestsException",
        "RequestThrottledException",
    }


def read_pages_parallel(
    pages: Sequence[tuple[int, bytes]],
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
    prompt: FormPrompt = BUILT_IN_PROMPT,
) -> tuple[PageRead, ...]:
    """Read every page with both configured readers and return results in page/reader order.

    Calls run in worker threads, but callers should persist results on the owning DB thread after
    this function returns. Throttling retries use exponential backoff; malformed answer retry policy
    remains inside `read_page` and is never triggered by a rule result. A reader whose answer is
    still malformed after that one re-ask abstains on that page (an empty answer), so the page's
    readings go to the person and the rest of the set is still read (#970).
    """
    if len(reader_ids) != 2 or not all(reader.strip() for reader in reader_ids):
        raise ValueError("exactly two named readers are required")
    vendors = tuple(independence_key("bedrock-form-reader", model) for model in reader_ids)
    if UNKNOWN_MODEL_VENDOR in vendors or vendors[0] == vendors[1]:
        raise ValueError("the two form readers must have known, distinct makers")
    if isinstance(max_concurrent_calls, bool) or max_concurrent_calls <= 0:
        raise ValueError("max_concurrent_calls must be a positive integer")
    if isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    if isinstance(max_throttle_retries, bool) or max_throttle_retries < 0:
        raise ValueError("max_throttle_retries must be a non-negative integer")
    if retry_backoff_seconds <= 0:
        raise ValueError("retry_backoff_seconds must be positive")
    ordered_pages = tuple(sorted(pages, key=lambda item: item[0]))
    if len({page_index for page_index, _ in ordered_pages}) != len(ordered_pages):
        raise ValueError("page indices must be unique")
    if any(isinstance(index, bool) or index < 0 for index, _ in ordered_pages):
        raise ValueError("page indices must be non-negative integers")
    pacer = ModelPacer(calls_per_minute)
    missing_limits = set(reader_ids) - set(calls_per_minute)
    if missing_limits:
        raise ValueError(
            f"missing per-model pacing limits for: {', '.join(sorted(missing_limits))}"
        )
    require_priced_readers(reader_ids, rates)

    jobs = tuple(
        (page_index, reader_id, png)
        for page_index, png in ordered_pages
        for reader_id in reader_ids
    )

    def invoke(job: tuple[int, str, bytes]) -> PageRead:
        page_index, model_id, png = job
        attempt_number = 0

        def record_job_attempt(attempt: AttemptUsage) -> None:
            nonlocal attempt_number
            attempt_number += 1
            record_attempt(replace(attempt, attempt_number=attempt_number))

        for throttle_attempt in range(max_throttle_retries + 1):
            pacer.wait(model_id)
            try:
                answer = read_page(
                    clients.for_current_thread(),
                    model_id=model_id,
                    page_png=png,
                    page_index=page_index,
                    max_tokens=max_tokens,
                    record_attempt=record_job_attempt,
                    prompt=prompt,
                )
                return PageRead(page_index, model_id, answer)
            except MalformedFormAnswer:
                return PageRead(
                    page_index,
                    model_id,
                    PageFormAnswer(page_index=page_index, countertops=[], notes=ABSTAINED_NOTE),
                )
            except Exception as error:
                if not _is_throttle(error) or throttle_attempt >= max_throttle_retries:
                    raise
                time.sleep(retry_backoff_seconds * (2**throttle_attempt))
        raise AssertionError("unreachable")

    completed: dict[tuple[int, str], PageRead] = {}
    with ThreadPoolExecutor(max_workers=max_concurrent_calls) as executor:
        futures = {executor.submit(invoke, job): (job[0], job[1]) for job in jobs}
        for future in as_completed(futures):
            result = future.result()
            completed[(result.page_index, result.model_id)] = result
    return tuple(completed[(page_index, model_id)] for page_index, model_id, _ in jobs)
