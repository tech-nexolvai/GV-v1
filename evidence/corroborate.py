"""Auditable corroboration decisions across independent reading routes.

Confidence is deliberately absent from every decision. Numeric disagreement remains a
conflict, while numeric agreement can promote only when semantic association is also known.

**Agreement needs readers from different vendors (#775).** Two model readers from one vendor make
the same mistakes: on the human-read key two Mistral models both read a `2"` label as `2'`, and
that was confirmed (#757). The admin's rule on #641 is that *cross-vendor* agreement confirms a
reading on its own, so `independence_key` groups model readers by vendor. A disagreement is still a
conflict whoever the readers are — the vendor rule makes agreement harder and nothing else.

Source: ``docs/DESIGN.md`` section 3.14, plan section F2 and issue #120.
Verification: ``tests/evidence/test_corroborate.py``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from evidence.candidate import ObservationCandidate
from evidence.canonical import CorroborationLane, EvidenceStatus
from units.dual import DualDimension
from units.policy import Consistency, check_dual

#: Who made a model, by the start of the model id a model reader records as its version. A model
#: reader's extractor version *is* its model id (`extraction/models/nova.py`, `openmodel.py`).
MODEL_VENDORS: Final[dict[str, str]] = {
    "amazon.": "amazon",
    "anthropic.": "anthropic",
    "mistral.": "mistral",
    "meta.": "meta",
    "cohere.": "cohere",
    "ai21.": "ai21",
    "deepseek.": "deepseek",
    "qwen.": "qwen",
    "google.": "google",
    "openai.": "openai",
    "writer.": "writer",
    "nvidia.": "nvidia",
    "moonshot.": "moonshot",
}

#: A cross-region inference profile names the same model from another route; it is not another model.
_PROFILE_PREFIXES: Final = ("us.", "eu.", "apac.", "global.", "us-gov.")

#: Extractors that are model readers even when their model id names no known vendor.
_MODEL_EXTRACTOR_PREFIX: Final = "bedrock-"
_MODEL_EXTRACTORS: Final = frozenset({"nova", "openmodel"})

UNKNOWN_MODEL_VENDOR: Final = "vendor:unknown"


def independence_key(extractor: str, extractor_version: str) -> str:
    """What makes one reader independent of another, for agreement (#775).

    - **A model reader counts by its vendor**, from its model id: `amazon.nova-2-lite-v1:0` and
      `us.amazon.nova-pro-v1:0` are both Amazon; `mistral.ministral-3-3b-instruct` and
      `mistral.mistral-large-3-675b-instruct` are both Mistral.
    - **A model reader whose vendor is not known counts as an unknown model.** Two such readers are
      never independent of each other — the conservative side of not knowing.
    - **Anything else** — the file's own text, OCR, the reviewer's markup, the shape reader — counts
      by its extractor name, as it always has.
    """
    bare = extractor_version
    for prefix in _PROFILE_PREFIXES:
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
            break
    for prefix, vendor in MODEL_VENDORS.items():
        if bare.startswith(prefix):
            return f"vendor:{vendor}"
    if extractor.startswith(_MODEL_EXTRACTOR_PREFIX) or extractor in _MODEL_EXTRACTORS:
        return UNKNOWN_MODEL_VENDOR
    return f"route:{extractor}"


def _independent_for_agreement(candidates: Sequence[ObservationCandidate]) -> bool:
    return (
        len(
            {
                independence_key(candidate.extractor, candidate.extractor_version)
                for candidate in candidates
            }
        )
        >= 2
    )


_MM_TOKEN_RE = re.compile(r"\bmm\b", re.IGNORECASE)
_INCH_TOKEN_RE = re.compile(r"(\"|'|\bin\b|\binch\b|\binches\b|\bft\b|\bfeet\b)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CorroborationResult:
    """One evidence judgment with every contributing candidate identifier."""

    status: EvidenceStatus
    supported_by: tuple[str, ...]
    conflicts_with: tuple[str, ...]
    lane: CorroborationLane | None


def _candidate_ids(candidates: tuple[ObservationCandidate, ...]) -> tuple[str, ...]:
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate ids must be unique within one corroboration decision")
    return candidate_ids


def _dual_result(
    candidate: ObservationCandidate, dual_dimension: DualDimension
) -> CorroborationResult:
    if candidate.parsed_value is None:
        raise ValueError("the dual-unit candidate must have a parsed primary measurement")
    if (
        candidate.parsed_value.exact != dual_dimension.primary.exact
        or candidate.parsed_value.unit is not dual_dimension.primary.unit
    ):
        raise ValueError("the dual dimension primary must match its candidate measurement")

    candidate_ids = (candidate.candidate_id,)
    consistency = check_dual(dual_dimension)
    if consistency is Consistency.NOT_CORROBORATED:
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)
    if consistency is Consistency.INCONSISTENT:
        return CorroborationResult(
            EvidenceStatus.CONFLICTING,
            candidate_ids,
            candidate_ids,
            CorroborationLane.DUAL_UNIT,
        )

    status = (
        EvidenceStatus.CORROBORATED
        if candidate.semantic_guess is not None
        else EvidenceStatus.RAW_CANDIDATE
    )
    return CorroborationResult(status, candidate_ids, (), CorroborationLane.DUAL_UNIT)


def _same_numeric_reading(candidates: tuple[ObservationCandidate, ...]) -> bool:
    values = tuple(candidate.parsed_value for candidate in candidates)
    if any(value is None for value in values):
        return False
    first = values[0]
    assert first is not None
    return all(
        value is not None and value.unit is first.unit and value.exact == first.exact
        for value in values[1:]
    )


def _authored_unit_system(candidate: ObservationCandidate) -> str:
    if candidate.parsed_value is None:
        return "unknown"
    raw_text = candidate.parsed_value.raw_text or candidate.raw_text
    if "[" in raw_text and "]" in raw_text:
        return "dual"
    if _MM_TOKEN_RE.search(raw_text):
        return "mm"
    if _INCH_TOKEN_RE.search(raw_text):
        return "in"
    return candidate.parsed_value.unit.value


def corroborate(
    candidates: Sequence[ObservationCandidate],
    *,
    dual_dimension: DualDimension | None = None,
) -> CorroborationResult:
    """Judge independent readers or one candidate's authored dual-unit token.

    Different extractor names are what make a disagreement a conflict; version changes do not.
    **Agreement asks more:** the readers must be independent by `independence_key` — for model
    readers, different vendors (#775). A dual dimension belongs to exactly one candidate and is always
    delegated to :func:`units.policy.check_dual` so rounding policy has one implementation.
    """

    candidate_tuple = tuple(candidates)
    if not candidate_tuple:
        raise ValueError("corroboration requires at least one candidate")
    if any(not isinstance(candidate, ObservationCandidate) for candidate in candidate_tuple):
        raise TypeError("candidates must contain only ObservationCandidate values")
    candidate_ids = _candidate_ids(candidate_tuple)

    if dual_dimension is not None:
        if len(candidate_tuple) != 1:
            raise ValueError("a dual dimension must be attributed to exactly one candidate")
        return _dual_result(candidate_tuple[0], dual_dimension)

    if len(candidate_tuple) == 1:
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)

    independent = len({candidate.extractor for candidate in candidate_tuple}) >= 2
    if not independent:
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)

    values_present = all(candidate.parsed_value is not None for candidate in candidate_tuple)
    if not values_present:
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)
    authored_units = {_authored_unit_system(candidate) for candidate in candidate_tuple}
    if "dual" in authored_units or len(authored_units) != 1:
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)
    if not _same_numeric_reading(candidate_tuple):
        return CorroborationResult(
            EvidenceStatus.CONFLICTING,
            candidate_ids,
            candidate_ids,
            CorroborationLane.SECOND_READER,
        )

    # **Agreement from one vendor confirms nothing** (#775): readers trained alike misread alike.
    if not _independent_for_agreement(candidate_tuple):
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)

    semantics = {candidate.semantic_guess for candidate in candidate_tuple}
    status = (
        EvidenceStatus.CORROBORATED
        if None not in semantics and len(semantics) == 1
        else EvidenceStatus.RAW_CANDIDATE
    )
    return CorroborationResult(status, candidate_ids, (), CorroborationLane.SECOND_READER)
