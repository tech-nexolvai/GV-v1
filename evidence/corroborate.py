"""Auditable corroboration decisions across independent reading routes.

Confidence is deliberately absent from every decision. Numeric disagreement remains a
conflict, while numeric agreement can promote only when semantic association is also known.

**Agreement needs readers from different vendors (#775).** Two model readers from one vendor make
the same mistakes: on the human-read key two Mistral models both read a `2"` label as `2'`, and
that was confirmed (#757). The admin's rule on #641 is that *cross-vendor* agreement confirms a
reading on its own, so `independence_key` groups model readers by vendor. A disagreement is still a
conflict whoever the readers are — the vendor rule makes agreement harder and nothing else.

**Two readers agreeing on both halves of a dual label confirm it; a reading with no value abstains
(#924, the admin's decisions of 2026-10-04).** Among a region's readings:

1. **A reading with no parsed value abstains.** It neither confirms nor vetoes: the judgement is made
   among the readings that have a value, and needs two of them, from two extractors. Where they
   differ, that is still a conflict; where fewer than two have a value, nothing is decided.
2. **A dual label (`914 [36]`) is agreed only on both halves.** Every valued reading must state a
   dual label, each with the same millimetres and the same inches, each reader's own two halves
   consistent by `units.policy.check_dual`, and the readers from two vendors. **The inches are the
   value** (Q12): the millimetres only confirm that the inches were read right. Agreement on the
   inches alone — the millimetres different, missing, or inconsistent with them — confirms nothing.
3. **A dual label's inches are inches as written**, so they are compared with a plain inch reading:
   different inches are a conflict. A millimetre reading is never compared with either, because its
   value is a conversion.

A stacked fraction is still never agreed, by any number of readers; that check stays first (#726).

**Across passes, one dual label (#928).** `corroborate` judges one group of readers. Where a region
holds agreements from more than one pass, `one_dual_label_across_agreements` says whether every
agreed reading states the same millimetres and the same inches; where not, the region is a conflict
for a reviewer (`workflow/stages.py`, `app/evidence/automatic_typing.py`).

Source: ``docs/DESIGN.md`` section 3.14, plan section F2 and issue #120.
Verification: ``tests/evidence/test_corroborate.py``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

from evidence.candidate import STACKED_FRACTION_FLAG, ObservationCandidate
from evidence.canonical import CorroborationLane, EvidenceStatus
from units.dual import DualDimension
from units.errors import UnknownRoundingError
from units.measurement import Measurement, Unit
from units.notation import canonical_notation, is_compound
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
    "moonshotai.": "moonshot",
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


def _texts(candidate: ObservationCandidate) -> tuple[str, ...]:
    """What a reading says, as reported and as its value's token: two texts where a caller gave its
    value the canonical token (`36"`) of a reading that says more (`914 [36]`)."""
    value_text = None if candidate.parsed_value is None else candidate.parsed_value.raw_text
    return tuple(dict.fromkeys(text for text in (value_text, candidate.raw_text) if text))


def _authored_unit_system(candidate: ObservationCandidate) -> str:
    if candidate.parsed_value is None:
        return "unknown"
    # **A dual label is one in either text** (#924): agreement on its inches alone confirms
    # nothing, so a label is not taken for a plain inch reading because its value's token is one.
    if _states_a_dual_label(_texts(candidate)):
        return "dual"
    raw_text = candidate.parsed_value.raw_text or candidate.raw_text
    if _MM_TOKEN_RE.search(raw_text):
        return "mm"
    if _INCH_TOKEN_RE.search(raw_text):
        return "in"
    return candidate.parsed_value.unit.value


#: The authored unit systems whose values may be compared as written (#924): one system, or a dual
#: label beside a plain inch reading, both of whose values are inches as the drawing writes them.
_INCHES_AS_WRITTEN: Final = frozenset({"dual", Unit.INCH.value})


def _comparable(authored_units: set[str]) -> bool:
    return len(authored_units) == 1 or authored_units == _INCHES_AS_WRITTEN


def _dual_halves(candidate: ObservationCandidate) -> DualDimension | None:
    """Both halves of the dual label a reading states, or `None` where it states no such label.

    **The inches are the reading's own value**, the one the stage stored and the only half a verdict
    may use (Q12). The millimetres are taken from the text the value was read from, by
    `units.notation.canonical_notation` — the rule that valued it — and are never a value: they
    are kept only to be compared. The inches keep the token as written, which `check_dual` needs
    for its rounding band. Where the reading's two texts (`_texts`) state different labels, or one
    is two dimensions and an operator, it states no one label.
    """
    return _dual_of(candidate.parsed_value, _texts(candidate))


def _dual_of(value: Measurement | None, texts: Sequence[str]) -> DualDimension | None:
    if value is None or value.unit is not Unit.INCH:
        return None
    stated: set[tuple[str, str]] = set()
    for text in texts:
        if is_compound(text):
            return None
        inches, millimetres = canonical_notation(text)
        if millimetres is not None:
            stated.add((millimetres, inches))
    # One label, or none: two texts stating different labels are not one reading of either.
    if len(stated) != 1:
        return None
    ((millimetres, inches),) = stated
    return DualDimension(
        primary=Measurement(Fraction(millimetres), Unit.MM, millimetres),
        alternate=Measurement(value.exact, Unit.INCH, inches),
    )


def _consistent(dual: DualDimension) -> bool:
    """Whether a dual label's own two halves agree within their rounding; one whose rounding band
    no token gives cannot be shown to."""
    try:
        return check_dual(dual) is Consistency.CONSISTENT_WITHIN_ROUNDING
    except UnknownRoundingError:
        return False


def is_consistent_dual_label(value: Measurement, text: str) -> bool:
    """Whether `text` states a dual label whose inches are `value` and whose millimetres agree with
    them within rounding (`check_dual`) — the shape every reading of an agreed dual label has (#924).

    For the agreement gate's guards: a dual label is agreed only on both its halves, so a guard asked
    about an agreed reading can tell from that reading alone that its millimetres cross-checked it.
    """
    texts = tuple(dict.fromkeys(item for item in (value.raw_text, text) if item))
    dual = _dual_of(value, texts)
    return dual is not None and _consistent(dual)


def _states_a_dual_label(texts: Sequence[str]) -> bool:
    return any("[" in text and "]" in text for text in texts)


def one_dual_label_across_agreements(readings: Sequence[tuple[Measurement | None, str]]) -> bool:
    """Whether a region's agreed readings, from however many passes, state one dual label (#928).

    Each reading is its stored value and the text it was read from. `corroborate` agrees a dual label
    only on both its halves, but it judges one group of readers at a time: a region the first pass
    agreed as `914 [36]` and the reading agent's pair agreed as `915 [36]` holds two agreements on
    the same inches. **The inches decide (Q12), and millimetres that differ between the groups show
    that one group misread**, as #924 refuses within one group. So, across every agreement:

    - **No dual label anywhere:** true — there are no millimetres to compare, and the region is
      left as it was.
    - **Any dual label:** true only where every reading states one, with the same millimetres and the
      same inches. A reading with no millimetres beside it agrees on the inches alone, and one whose
      halves cannot be read agrees with nothing — as `corroborate` judges one group.

    Only ever towards a reviewer: a caller holds a region back on `False` and never seals on `True`.
    """
    labels: set[tuple[Fraction, Fraction]] = set()
    plain = False
    for value, text in readings:
        texts = tuple(
            dict.fromkeys(
                item for item in (None if value is None else value.raw_text, text) if item
            )
        )
        if not _states_a_dual_label(texts):
            plain = True
            continue
        dual = _dual_of(value, texts)
        if dual is None or dual.alternate is None:
            return False
        labels.add((dual.primary.exact, dual.alternate.exact))
    return not labels or (len(labels) == 1 and not plain)


def _same_dual_reading(candidates: tuple[ObservationCandidate, ...]) -> bool:
    """Whether every reading states one dual label: the same millimetres, the same inches, and each
    reader's own two halves consistent within their rounding (`check_dual`) (#924).

    A reading whose halves cannot be checked — a rounding band no token gives — agrees with nothing.
    """
    first: DualDimension | None = None
    for candidate in candidates:
        dual = _dual_halves(candidate)
        if dual is None or dual.alternate is None or not _consistent(dual):
            return False
        if first is None:
            first = dual
            continue
        assert first.alternate is not None
        if (
            dual.primary.exact != first.primary.exact
            or dual.alternate.exact != first.alternate.exact
        ):
            return False
    return first is not None


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

    **Only the readings with a value are judged (#924).** One with none abstains: it is not in
    `supported_by` of an agreement or `conflicts_with` of a conflict, and it blocks neither. Where
    the readers agree on a dual label, they agree on both its halves, as the module says.
    """

    candidate_tuple = tuple(candidates)
    if not candidate_tuple:
        raise ValueError("corroboration requires at least one candidate")
    if any(not isinstance(candidate, ObservationCandidate) for candidate in candidate_tuple):
        raise TypeError("candidates must contain only ObservationCandidate values")
    candidate_ids = _candidate_ids(candidate_tuple)

    # **A stacked fraction is never agreed into evidence** (#726). However many readers say the same,
    # and however exactly it was read, a reviewer confirms it: the whole group stays a raw candidate.
    if any(STACKED_FRACTION_FLAG in candidate.ambiguity_flags for candidate in candidate_tuple):
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)

    if dual_dimension is not None:
        if len(candidate_tuple) != 1:
            raise ValueError("a dual dimension must be attributed to exactly one candidate")
        return _dual_result(candidate_tuple[0], dual_dimension)

    if len(candidate_tuple) == 1:
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)

    # **A reading with no value abstains** (#924): it neither confirms nor vetoes. Everything below
    # is judged among the readings that have one, and needs two of them, from two extractors.
    valued = tuple(candidate for candidate in candidate_tuple if candidate.parsed_value is not None)
    if len(valued) < 2 or len({candidate.extractor for candidate in valued}) < 2:
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)
    valued_ids = tuple(candidate.candidate_id for candidate in valued)

    authored_units = {_authored_unit_system(candidate) for candidate in valued}
    if not _comparable(authored_units):
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)
    if not _same_numeric_reading(valued):
        return CorroborationResult(
            EvidenceStatus.CONFLICTING,
            valued_ids,
            valued_ids,
            CorroborationLane.SECOND_READER,
        )

    # **A dual label is agreed on both its halves, or not at all** (#924): the same millimetres and
    # the same inches from every reader, each reader's own halves consistent. The inches stay the
    # value; agreement on them alone confirms nothing.
    if "dual" in authored_units and (authored_units != {"dual"} or not _same_dual_reading(valued)):
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)

    # **Agreement from one vendor confirms nothing** (#775): readers trained alike misread alike.
    if not _independent_for_agreement(valued):
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)

    semantics = {candidate.semantic_guess for candidate in valued}
    status = (
        EvidenceStatus.CORROBORATED
        if None not in semantics and len(semantics) == 1
        else EvidenceStatus.RAW_CANDIDATE
    )
    return CorroborationResult(status, valued_ids, (), CorroborationLane.SECOND_READER)
