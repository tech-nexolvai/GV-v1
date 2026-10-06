"""Verification for issue #120: each corroboration case states input, outcome, and reason."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from evidence.candidate import STACKED_FRACTION_FLAG, ObservationCandidate
from evidence.canonical import CorroborationLane, EvidenceStatus
from evidence.coordinates import ImagePoint
from evidence.corroborate import CorroborationResult, corroborate
from rules.semantic_types import SemanticType
from units.dual import parse_dual
from units.measurement import Measurement, Unit


def _candidate(
    candidate_id: str,
    extractor: str,
    *,
    exact: Fraction = Fraction(984),
    unit: Unit = Unit.MM,
    raw_text: str = "984",
    semantic_guess: SemanticType | None = SemanticType.CABINET_WIDTH,
    confidence: Decimal = Decimal("0.5"),
    extractor_version: str = "1.0",
) -> ObservationCandidate:
    return ObservationCandidate(
        candidate_id=candidate_id,
        extractor=extractor,
        extractor_version=extractor_version,
        raw_text=raw_text,
        parsed_value=Measurement(exact, unit, raw_text),
        unit_guess=unit,
        semantic_guess=semantic_guess,
        page=2,
        polygon=(ImagePoint(10, 10), ImagePoint(20, 10), ImagePoint(20, 20)),
        confidence=confidence,
        ambiguity_flags=(),
    )


def test_two_independent_readers_agreeing_numerically_and_semantically_are_corroborated() -> None:
    """Input: equal PDF/OCR readings. Outcome: CORROBORATED. Why: routes and meaning agree."""

    vector = _candidate("vector-1", "pdfplumber")
    ocr = _candidate("ocr-1", "paddleocr")

    result = corroborate((vector, ocr))

    assert result == CorroborationResult(
        EvidenceStatus.CORROBORATED,
        ("vector-1", "ocr-1"),
        (),
        CorroborationLane.SECOND_READER,
    )


def test_numeric_agreement_with_different_semantics_stays_raw() -> None:
    """Input: equal numbers, different meanings. Outcome: RAW. Why: reading is not association."""

    cabinet = _candidate("vector-1", "pdfplumber")
    filler = _candidate(
        "ocr-1",
        "paddleocr",
        semantic_guess=SemanticType.FILLER_WIDTH,
    )

    result = corroborate((cabinet, filler))

    assert result.status is EvidenceStatus.RAW_CANDIDATE
    assert result.lane is CorroborationLane.SECOND_READER
    assert result.conflicts_with == ()


def test_numeric_agreement_with_an_unknown_semantic_type_stays_raw() -> None:
    """Input: one unknown association. Outcome: RAW. Why: position never supplies a meaning."""

    known = _candidate("vector-1", "pdfplumber")
    unknown = _candidate("ocr-1", "paddleocr", semantic_guess=None)

    assert corroborate((known, unknown)).status is EvidenceStatus.RAW_CANDIDATE


def test_same_extractor_at_different_versions_is_not_independent() -> None:
    """Input: two versions of one reader. Outcome: RAW. Why: systematic errors can repeat."""

    first = _candidate("ocr-1", "paddleocr", extractor_version="1.0")
    second = _candidate("ocr-2", "paddleocr", extractor_version="2.0")

    result = corroborate((first, second))

    assert result.status is EvidenceStatus.RAW_CANDIDATE
    assert result.lane is None


def test_disagreeing_readers_are_conflicting_regardless_of_confidence() -> None:
    """Input: high-confidence 985 vs low-confidence 984. Outcome: CONFLICTING. Why: no winner."""

    high_confidence = _candidate(
        "ocr-1",
        "paddleocr",
        exact=Fraction(985),
        raw_text="985",
        confidence=Decimal("0.999"),
    )
    low_confidence = _candidate(
        "vector-1",
        "pdfplumber",
        confidence=Decimal("0.1"),
    )

    result = corroborate((high_confidence, low_confidence))

    assert result.status is EvidenceStatus.CONFLICTING
    assert result.supported_by == ("ocr-1", "vector-1")
    assert result.conflicts_with == ("ocr-1", "vector-1")
    assert result.lane is CorroborationLane.SECOND_READER


def test_cross_unit_conversion_is_not_a_second_reader() -> None:
    """Input: 25.4 mm and 1 inch. Outcome: RAW. Why: conversion is not independence."""

    millimetres = _candidate(
        "vector-1",
        "pdfplumber",
        exact=Fraction(1),
        unit=Unit.INCH,
        raw_text="25.4 mm",
    )
    inches = _candidate(
        "ocr-1",
        "paddleocr",
        exact=Fraction(1),
        unit=Unit.INCH,
        raw_text='1"',
    )

    result = corroborate((millimetres, inches))

    assert result.status is EvidenceStatus.RAW_CANDIDATE
    assert result.lane is None


def test_single_candidate_without_an_independent_lane_stays_raw() -> None:
    """Input: one reading. Outcome: RAW. Why: one route cannot corroborate itself."""

    result = corroborate((_candidate("vector-1", "pdfplumber"),))

    assert result == CorroborationResult(
        EvidenceStatus.RAW_CANDIDATE,
        ("vector-1",),
        (),
        None,
    )


def test_consistent_dual_unit_token_corroborates_a_known_semantic_reading() -> None:
    """Input: 984 [38 3/4]. Outcome: CORROBORATED. Why: authored readings agree in-band."""

    candidate = _candidate("vector-1", "pdfplumber")

    result = corroborate((candidate,), dual_dimension=parse_dual("984 [38 3/4]"))

    assert result.status is EvidenceStatus.CORROBORATED
    assert result.supported_by == ("vector-1",)
    assert result.lane is CorroborationLane.DUAL_UNIT


def test_consistent_dual_unit_token_cannot_promote_an_unknown_semantic_association() -> None:
    """Input: agreeing token, unknown meaning. Outcome: RAW. Why: the lane checks reading only."""

    candidate = _candidate("vector-1", "pdfplumber", semantic_guess=None)

    result = corroborate((candidate,), dual_dimension=parse_dual("984 [38 3/4]"))

    assert result.status is EvidenceStatus.RAW_CANDIDATE
    assert result.lane is CorroborationLane.DUAL_UNIT


def test_single_unit_token_is_not_corroboration_or_conflict() -> None:
    """Input: 984 with no alternate. Outcome: RAW. Why: absence is not agreement or conflict."""

    candidate = _candidate("vector-1", "pdfplumber")

    result = corroborate((candidate,), dual_dimension=parse_dual("984"))

    assert result.status is EvidenceStatus.RAW_CANDIDATE
    assert result.conflicts_with == ()
    assert result.lane is None


def test_inconsistent_dual_unit_token_becomes_conflicting_without_a_preferred_side() -> None:
    """Input: 984 [39 3/4]. Outcome: CONFLICTING. Why: authored readings disagree."""

    candidate = _candidate("vector-1", "pdfplumber")

    result = corroborate((candidate,), dual_dimension=parse_dual("984 [39 3/4]"))

    assert result.status is EvidenceStatus.CONFLICTING
    assert result.supported_by == ("vector-1",)
    assert result.conflicts_with == ("vector-1",)
    assert result.lane is CorroborationLane.DUAL_UNIT


def test_empty_input_raises_instead_of_inventing_an_evidence_state() -> None:
    """Input: no candidates. Outcome: ValueError. Why: there is no observation to judge."""

    with pytest.raises(ValueError, match="at least one"):
        corroborate(())


def test_dual_dimension_must_be_attributed_to_exactly_one_matching_candidate() -> None:
    """Input: ambiguous or mismatched owner. Outcome: ValueError. Why: provenance must be exact."""

    first = _candidate("vector-1", "pdfplumber")
    second = _candidate("ocr-1", "paddleocr")
    with pytest.raises(ValueError, match="exactly one"):
        corroborate((first, second), dual_dimension=parse_dual("984 [38 3/4]"))

    mismatched = _candidate("vector-2", "pdfplumber", exact=Fraction(985), raw_text="985")
    with pytest.raises(ValueError, match="primary must match"):
        corroborate((mismatched,), dual_dimension=parse_dual("984 [38 3/4]"))


# ---------------------------------------------------------------------------
# Agreement needs readers from different vendors (#775)
# ---------------------------------------------------------------------------

NOVA_2_LITE = ("bedrock-nova-2-lite", "amazon.nova-2-lite-v1:0")
MINISTRAL_3B = ("bedrock-ministral-3-3b", "mistral.ministral-3-3b-instruct")
MISTRAL_LARGE_3 = ("bedrock-mistral-large-3", "mistral.mistral-large-3-675b-instruct")


def _model(candidate_id: str, reader: tuple[str, str], **changes: object) -> ObservationCandidate:
    extractor, model_id = reader
    return _candidate(candidate_id, extractor, extractor_version=model_id, **changes)  # type: ignore[arg-type]


def test_two_models_of_one_vendor_agreeing_confirm_nothing() -> None:
    """**The #757 scorecard's confirmed wrong value (#775).** Input: Ministral 3B and mistral-large-3
    agree. Outcome: RAW with no lane. Why: both are Mistral models, and readers trained alike misread
    alike — #641 measured same-vendor pairs agreeing wrong most."""
    result = corroborate((_model("a", MINISTRAL_3B), _model("b", MISTRAL_LARGE_3)))

    assert result == CorroborationResult(EvidenceStatus.RAW_CANDIDATE, ("a", "b"), (), None)


def test_two_models_of_different_vendors_agreeing_still_confirm() -> None:
    """Input: Nova 2 Lite and Ministral 3B agree — today's pair. Outcome: CORROBORATED, as before."""
    result = corroborate((_model("a", NOVA_2_LITE), _model("b", MINISTRAL_3B)))

    assert result.status is EvidenceStatus.CORROBORATED
    assert result.lane is CorroborationLane.SECOND_READER


QWEN3_VL = ("bedrock-qwen3-vl-235b", "qwen.qwen3-vl-235b-a22b")
NOVA_2_LITE_TAUGHT = ("bedrock-nova-2-lite-taught", "us.amazon.nova-2-lite-v1:0")


def test_the_new_pair_agreeing_confirms_it_two_vendors() -> None:
    """**#907's pair.** Input: Qwen3-VL and Nova 2 Lite (as the trial measured it, through its
    profile) agree. Outcome: CORROBORATED on the second-reader lane — Qwen and Amazon are two
    vendors (#775)."""
    result = corroborate((_model("a", QWEN3_VL), _model("b", NOVA_2_LITE_TAUGHT)))

    assert result.status is EvidenceStatus.CORROBORATED
    assert result.lane is CorroborationLane.SECOND_READER


def test_nova_2_lite_asked_two_ways_is_still_one_vendor() -> None:
    """Input: Nova 2 Lite on the tool path and Nova 2 Lite taught agree. Outcome: RAW with no lane:
    one model asked twice is one witness, whatever the words or the picture."""
    result = corroborate((_model("a", NOVA_2_LITE), _model("b", NOVA_2_LITE_TAUGHT)))

    assert result == CorroborationResult(EvidenceStatus.RAW_CANDIDATE, ("a", "b"), (), None)


def test_a_disagreement_is_a_conflict_whatever_vendor_the_readers_are() -> None:
    """**The vendor rule makes agreement harder, and nothing else.** Input: two Mistral models
    disagree. Outcome: CONFLICTING. Why: a disagreement is a reviewer's to decide, however alike
    the readers are."""
    result = corroborate(
        (_model("a", MINISTRAL_3B), _model("b", MISTRAL_LARGE_3, exact=Fraction(985)))
    )

    assert result.status is EvidenceStatus.CONFLICTING


def test_a_third_vendor_makes_a_one_vendor_pair_independent() -> None:
    """Input: two Mistral models and Nova 2 Lite agree. Outcome: CORROBORATED — two vendors agree."""
    result = corroborate(
        (_model("a", MINISTRAL_3B), _model("b", MISTRAL_LARGE_3), _model("c", NOVA_2_LITE))
    )

    assert result.status is EvidenceStatus.CORROBORATED


def test_a_model_and_the_files_own_text_are_independent() -> None:
    """Input: the vector text and a model agree. Outcome: CORROBORATED, as before."""
    result = corroborate((_candidate("v", "pdfplumber"), _model("m", MINISTRAL_3B)))

    assert result.status is EvidenceStatus.CORROBORATED


@pytest.mark.parametrize(
    ("extractor", "version", "key"),
    [
        ("bedrock-nova-2-lite", "amazon.nova-2-lite-v1:0", "vendor:amazon"),
        ("bedrock-nova-2-lite", "us.amazon.nova-2-lite-v1:0", "vendor:amazon"),
        ("bedrock-nova-pro", "global.amazon.nova-pro-v1:0", "vendor:amazon"),
        ("bedrock-ministral-3-3b", "mistral.ministral-3-3b-instruct", "vendor:mistral"),
        ("bedrock-claude-haiku-4-5", "anthropic.claude-haiku-4-5", "vendor:anthropic"),
        ("bedrock-kimi-k3", "us.moonshotai.kimi-k3", "vendor:moonshot"),
        ("bedrock-kimi-k3", "moonshotai.kimi-k3", "vendor:moonshot"),
        ("nova", "amazon.nova-lite-v1:0", "vendor:amazon"),
        ("bedrock-new-reader", "someone.model-1", "vendor:unknown"),
        ("openmodel", "openbmb/MiniCPM-V-4", "vendor:unknown"),
        ("pdfplumber", "0.11.4", "route:pdfplumber"),
        ("extraction.annotations", "1", "route:extraction.annotations"),
    ],
)
def test_the_independence_key(extractor: str, version: str, key: str) -> None:
    from evidence.corroborate import independence_key

    assert independence_key(extractor, version) == key


def test_two_models_whose_vendor_is_unknown_are_never_independent() -> None:
    """Input: two model readers nobody has named a vendor for. Outcome: RAW. Why: not knowing is
    not independence."""
    first = _model("a", ("bedrock-new-reader", "someone.model-1"))
    second = _model("b", ("openmodel", "openbmb/MiniCPM-V-4"))

    assert corroborate((first, second)).lane is None


def test_every_defined_vision_reader_has_a_known_vendor() -> None:
    """**The drift guard.** A reader added to `VISION_READERS` with a vendor this table does not
    know would count as an unknown model and never corroborate — found here, not in a run."""
    from evidence.corroborate import independence_key
    from extraction.models.nova import VISION_READERS

    keys = {
        reader.extractor: independence_key(reader.extractor, reader.model_id)
        for reader in VISION_READERS
    }

    assert "vendor:unknown" not in keys.values(), keys
    assert keys["bedrock-nova-2-lite"] != keys["bedrock-ministral-3-3b"]
    assert keys["bedrock-ministral-3-3b"] == keys["bedrock-mistral-large-3"]


def test_a_stacked_fraction_is_never_agreed_into_evidence() -> None:
    """**#726, held where agreement is decided.** Input: two independent readers — one the exact
    text of a stacked `24 3/4"`, flagged stacked — agreeing in value and meaning. Outcome: still a
    raw candidate, with no lane: a reviewer confirms a stacked fraction, however many readers agree.
    """
    from dataclasses import replace

    exact = _candidate(
        "stamp-1", "extraction.stamp_text", exact=Fraction(99, 4), raw_text='24 3/4"'
    )
    stacked = replace(exact, ambiguity_flags=(STACKED_FRACTION_FLAG,))
    vision = _candidate(
        "vision-1",
        "extraction.models.vision",
        exact=Fraction(99, 4),
        raw_text='24 3/4"',
        extractor_version="us.amazon.nova-2-lite-v1:0",
    )

    assert corroborate((exact, vision)).status is EvidenceStatus.CORROBORATED  # the control
    result = corroborate((stacked, vision))

    assert result.status is EvidenceStatus.RAW_CANDIDATE
    assert result.lane is None
    assert result.conflicts_with == ()


# ---------------------------------------------------------------------------
# Both halves of a dual label, and a reading with no value (#924)
# ---------------------------------------------------------------------------

#: The issue's label: 914 mm over 36 inches. 36" is 914.4 mm, inside 914's rounding.
DUAL = "914 [36]"


def _dual(
    candidate_id: str, reader: tuple[str, str], text: str = DUAL, inches: Fraction = Fraction(36)
) -> ObservationCandidate:
    """A model's reading of a dual label, valued as the stage values it: its inches (Q12)."""
    return _model(candidate_id, reader, exact=inches, unit=Unit.INCH, raw_text=text)


def _blank(candidate_id: str, reader: tuple[str, str], text: str = "914") -> ObservationCandidate:
    """A reading the stage stored with no value — half a label, `914`, with no unit."""
    from dataclasses import replace

    return replace(_model(candidate_id, reader, raw_text=text), parsed_value=None)


def test_two_vendors_agreeing_on_both_halves_of_a_dual_label_confirm_it() -> None:
    """**#924, decision 1.** Input: Qwen3-VL and Nova 2 Lite (two vendors) both read `914 [36]`.
    Outcome: the second-reader lane, as for any agreement. Why: two independent readers agree on the
    millimetres and the inches, and each reader's own halves agree within rounding."""
    result = corroborate((_dual("a", QWEN3_VL), _dual("b", NOVA_2_LITE_TAUGHT)))

    assert result == CorroborationResult(
        EvidenceStatus.CORROBORATED, ("a", "b"), (), CorroborationLane.SECOND_READER
    )


def test_a_dual_label_written_with_its_unit_word_is_the_same_label() -> None:
    """Input: `914mm [36"]` and `914 [36]`. Outcome: agreed — the notation module reads both as the
    same two halves, as it valued them."""
    result = corroborate(
        (_dual("a", QWEN3_VL, '914mm [36"]'), _dual("b", NOVA_2_LITE_TAUGHT, DUAL))
    )

    assert result.lane is CorroborationLane.SECOND_READER
    assert result.status is EvidenceStatus.CORROBORATED


def test_one_vendor_agreeing_on_a_dual_label_confirms_nothing() -> None:
    """Input: Nova 2 Lite asked two ways reads `914 [36]` both times. Outcome: RAW with no lane — one
    vendor is one witness (#775), on a dual label as on any other."""
    result = corroborate((_dual("a", NOVA_2_LITE), _dual("b", NOVA_2_LITE_TAUGHT)))

    assert result == CorroborationResult(EvidenceStatus.RAW_CANDIDATE, ("a", "b"), (), None)


def test_the_same_inches_with_different_millimetres_confirm_nothing() -> None:
    """Input: `914 [36]` and `920 [36]` — both consistent, the same inches. Outcome: RAW with no lane,
    and no conflict. Why: agreement on the inches alone does not confirm; a reader who misread one
    half may have misread the other."""
    result = corroborate((_dual("a", QWEN3_VL), _dual("b", NOVA_2_LITE_TAUGHT, "920 [36]")))

    assert result == CorroborationResult(EvidenceStatus.RAW_CANDIDATE, ("a", "b"), (), None)


def test_the_inches_of_a_dual_label_beside_a_plain_inch_reading_confirm_nothing() -> None:
    """Input: `914 [36]` and `36"`. Outcome: RAW with no lane. Why: the millimetres are missing from
    one reading, so the two agree on the inches alone."""
    inches = _model("b", NOVA_2_LITE_TAUGHT, exact=Fraction(36), unit=Unit.INCH, raw_text='36"')

    result = corroborate((_dual("a", QWEN3_VL), inches))

    assert result == CorroborationResult(EvidenceStatus.RAW_CANDIDATE, ("a", "b"), (), None)


def test_millimetres_inconsistent_with_their_inches_confirm_nothing() -> None:
    """Input: both readers read `914 [38]` — the same halves, but 38" is 965.2 mm, outside 914's and
    38's rounding. Outcome: RAW with no lane. Why: `check_dual` fails, so the millimetres do not
    show the inches were read right, however many readers agree on them."""
    result = corroborate(
        (
            _dual("a", QWEN3_VL, "914 [38]", Fraction(38)),
            _dual("b", NOVA_2_LITE_TAUGHT, "914 [38]", Fraction(38)),
        )
    )

    assert result == CorroborationResult(EvidenceStatus.RAW_CANDIDATE, ("a", "b"), (), None)


def test_a_dual_label_whose_inches_differ_is_a_conflict() -> None:
    """Input: `914 [36]` and `895 [35]`, each consistent on its own. Outcome: CONFLICTING. Why: the
    inches are the value, and different values are a conflict, whatever else agrees."""
    result = corroborate(
        (_dual("a", QWEN3_VL), _dual("b", NOVA_2_LITE_TAUGHT, "895 [35]", Fraction(35)))
    )

    assert result == CorroborationResult(
        EvidenceStatus.CONFLICTING, ("a", "b"), ("a", "b"), CorroborationLane.SECOND_READER
    )


def test_a_dual_labels_inches_and_a_plain_inch_reading_that_differ_are_a_conflict() -> None:
    """Input: `914 [36]` and `35"`. Outcome: CONFLICTING. Why: a dual label's inches are inches as
    written, so they are compared with an inch reading like any other."""
    inches = _model("b", NOVA_2_LITE_TAUGHT, exact=Fraction(35), unit=Unit.INCH, raw_text='35"')

    assert corroborate((_dual("a", QWEN3_VL), inches)).status is EvidenceStatus.CONFLICTING


def test_a_millimetre_reading_is_never_compared_with_a_dual_labels_inches() -> None:
    """Input: `914 [36]` and `914 mm`, valued as its exact conversion. Outcome: RAW with no lane and
    no conflict — a conversion is not a second reading, as before."""
    millimetres = _model(
        "b",
        NOVA_2_LITE_TAUGHT,
        exact=Fraction(914) * Fraction(5, 127),
        unit=Unit.INCH,
        raw_text="914 mm",
    )

    result = corroborate((_dual("a", QWEN3_VL), millimetres))

    assert result == CorroborationResult(EvidenceStatus.RAW_CANDIDATE, ("a", "b"), (), None)


def test_a_reading_with_no_value_abstains_from_an_agreement() -> None:
    """**#924, decision 2: the measured case.** Input: Qwen3-VL and the taught Nova read
    `914 [36]`, and the forced-tool Nova returns half the label, `914`, with no value. Outcome: the
    two agree; the third is not among the readings that support it. Why: a reading with no value
    neither confirms nor vetoes."""
    result = corroborate(
        (_blank("c", NOVA_2_LITE), _dual("a", QWEN3_VL), _dual("b", NOVA_2_LITE_TAUGHT))
    )

    assert result == CorroborationResult(
        EvidenceStatus.CORROBORATED, ("a", "b"), (), CorroborationLane.SECOND_READER
    )


def test_a_reading_with_no_value_abstains_from_a_plain_agreement_too() -> None:
    """Input: two vendors read `24"`, a third reading has no value. Outcome: CORROBORATED by the two."""
    result = corroborate(
        (
            _model("a", QWEN3_VL, exact=Fraction(24), unit=Unit.INCH, raw_text='24"'),
            _blank("b", MINISTRAL_3B, "24"),
            _model("c", NOVA_2_LITE, exact=Fraction(24), unit=Unit.INCH, raw_text='24"'),
        )
    )

    assert result == CorroborationResult(
        EvidenceStatus.CORROBORATED, ("a", "c"), (), CorroborationLane.SECOND_READER
    )


def test_a_reading_with_no_value_never_makes_a_second_vendor() -> None:
    """Input: Nova 2 Lite asked two ways agree on `914 [36]`; Qwen3-VL returns no value. Outcome: RAW
    with no lane. Why: an abstaining reader is no witness — the agreement still needs two vendors
    among the readings with a value."""
    result = corroborate(
        (_dual("a", NOVA_2_LITE), _dual("b", NOVA_2_LITE_TAUGHT), _blank("c", QWEN3_VL))
    )

    assert result == CorroborationResult(EvidenceStatus.RAW_CANDIDATE, ("a", "b", "c"), (), None)


def test_one_value_beside_a_reading_with_none_decides_nothing() -> None:
    """Input: one reading with a value and one without. Outcome: RAW with no lane — one value is not
    an agreement, and the empty one is no conflict."""
    result = corroborate((_dual("a", QWEN3_VL), _blank("b", NOVA_2_LITE_TAUGHT)))

    assert result == CorroborationResult(EvidenceStatus.RAW_CANDIDATE, ("a", "b"), (), None)


def test_a_region_where_no_reading_has_a_value_stays_raw() -> None:
    result = corroborate((_blank("a", QWEN3_VL), _blank("b", NOVA_2_LITE_TAUGHT)))

    assert result == CorroborationResult(EvidenceStatus.RAW_CANDIDATE, ("a", "b"), (), None)


def test_different_values_beside_a_reading_with_none_are_still_a_conflict() -> None:
    """**#924: a conflict is still a conflict.** Input: `24"` and `25"` from two vendors, and a third
    reading with no value. Outcome: CONFLICTING among the two with values. Why: the empty reading
    abstains, so it cannot hide their disagreement either."""
    result = corroborate(
        (
            _model("a", QWEN3_VL, exact=Fraction(24), unit=Unit.INCH, raw_text='24"'),
            _blank("b", NOVA_2_LITE),
            _model("c", NOVA_2_LITE_TAUGHT, exact=Fraction(25), unit=Unit.INCH, raw_text='25"'),
        )
    )

    assert result == CorroborationResult(
        EvidenceStatus.CONFLICTING, ("a", "c"), ("a", "c"), CorroborationLane.SECOND_READER
    )


def test_a_stacked_fraction_is_never_agreed_on_a_dual_label_either() -> None:
    """**#726, still first.** Input: two vendors agree on both halves of `629 [24 3/4]`, one reading
    flagged stacked. Outcome: RAW with no lane."""
    from dataclasses import replace

    first = _dual("a", QWEN3_VL, "629 [24 3/4]", Fraction(99, 4))
    second = _dual("b", NOVA_2_LITE_TAUGHT, "629 [24 3/4]", Fraction(99, 4))
    assert corroborate((first, second)).lane is CorroborationLane.SECOND_READER  # the control

    stacked = replace(second, ambiguity_flags=(STACKED_FRACTION_FLAG,))

    assert corroborate((first, stacked)) == CorroborationResult(
        EvidenceStatus.RAW_CANDIDATE, ("a", "b"), (), None
    )


def test_a_stacked_reading_with_no_value_still_keeps_the_group_raw() -> None:
    """Input: two vendors agree on `24"`; a third reading over a stacked fraction has no value.
    Outcome: RAW with no lane. Why: the stacked check comes before any reading abstains."""
    from dataclasses import replace

    stacked = replace(_blank("c", MINISTRAL_3B, "24"), ambiguity_flags=(STACKED_FRACTION_FLAG,))
    result = corroborate(
        (
            _model("a", QWEN3_VL, exact=Fraction(24), unit=Unit.INCH, raw_text='24"'),
            _model("b", NOVA_2_LITE, exact=Fraction(24), unit=Unit.INCH, raw_text='24"'),
            stacked,
        )
    )

    assert result == CorroborationResult(EvidenceStatus.RAW_CANDIDATE, ("a", "b", "c"), (), None)


def test_a_dual_label_is_one_whichever_of_a_readings_texts_states_it() -> None:
    """Input: two vendors' readings that say `914 [36]` and `920 [36]`, each valued by the canonical
    token `36"`, as the scorecards value a reading. Outcome: RAW with no lane. Why: the label is a
    dual one whichever text says so, and agreement on its inches alone confirms nothing."""
    from dataclasses import replace

    def token_valued(candidate: ObservationCandidate) -> ObservationCandidate:
        return replace(candidate, parsed_value=Measurement(Fraction(36), Unit.INCH, '36"'))

    first = token_valued(_dual("a", QWEN3_VL))
    second = token_valued(_dual("b", NOVA_2_LITE_TAUGHT, "920 [36]"))
    assert corroborate((first, token_valued(_dual("b", NOVA_2_LITE_TAUGHT)))).lane is (
        CorroborationLane.SECOND_READER
    )  # the control: both halves agree

    assert corroborate((first, second)) == CorroborationResult(
        EvidenceStatus.RAW_CANDIDATE, ("a", "b"), (), None
    )
