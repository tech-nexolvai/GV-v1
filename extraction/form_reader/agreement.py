"""Compare the two form readers without assigning semantic operands.

The array positions are only structural slots in the two answers. They are not a part kind or
rule-field identity. A later deterministic mapping or reviewer action must assign a reading to a
measurement field before it can participate in a check.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import zip_longest
from typing import Literal

from evidence.corroborate import UNKNOWN_MODEL_VENDOR, independence_key
from extraction.form_reader.parser import ParsedDimension, parse_dimension
from extraction.form_reader.schema import CountertopForm, FormDimension, PageFormAnswer
from units.measurement import Measurement

ReviewReason = Literal[
    "readers-differ",
    "one-reader-missing",
    "combined",
    "stacked",
    "unreadable",
    "scope-not-run",
    "reader-topology-differs",
    "appliance-span",
    "gv-mark",
    "label-location-unknown",
    "gv-mark-unchecked",
]


@dataclass(frozen=True, slots=True)
class ComparedReading:
    """A pairwise numeric result; deliberately has no field key or operand authority."""

    page_index: int
    countertop_index: int
    slot: Literal["overall", "chain"]
    chain_index: int | None
    state: Literal["corroborated", "review_required"]
    value: Measurement | None
    reason: ReviewReason | None
    qwen_dimension: FormDimension | None
    first_dimension: FormDimension | None = None

    @property
    def exact_inches(self) -> Fraction | None:
        """Normalized inches for comparisons/reporting, never populated from boxes or components."""
        if self.value is None:
            return None
        return self.value.exact


def compare_page_answers(
    first: PageFormAnswer,
    second: PageFormAnswer,
    *,
    first_maker: str,
    second_maker: str,
) -> tuple[ComparedReading, ...]:
    """Pair by stable response position, and corroborate only exact parsed-text agreement.

    The pair must be from distinct makers and the same page. If the answer topology differs, the
    mismatched slots are sent to review rather than shifted into another slot. `kind`, boxes, and
    the model-supplied fraction components cannot establish identity or value.
    """
    first_vendor = independence_key("bedrock-form-reader", first_maker)
    second_vendor = independence_key("bedrock-form-reader", second_maker)
    if (
        not first_maker.strip()
        or not second_maker.strip()
        or first_vendor == UNKNOWN_MODEL_VENDOR
        or second_vendor == UNKNOWN_MODEL_VENDOR
        or first_vendor == second_vendor
    ):
        raise ValueError("form corroboration requires two readers with known, distinct makers")
    if first.page_index != second.page_index:
        raise ValueError("form answers must be for the same page")

    result: list[ComparedReading] = []
    for countertop_index, (left, right) in enumerate(
        zip_longest(first.countertops, second.countertops)
    ):
        if left is None or right is None:
            existing = left if right is None else right
            assert existing is not None
            result.extend(
                _missing_countertop_slots(first.page_index, countertop_index, existing, qwen=right)
            )
            continue

        result.append(
            _compare_dimension(
                first.page_index,
                countertop_index,
                "overall",
                None,
                left.overall,
                right.overall,
                scope_ok=(left.overall_scope == "run" and right.overall_scope == "run"),
                scope_stated=(left.overall_scope is not None and right.overall_scope is not None),
                appliance_span=any(
                    dimension.kind == "appliance_space" for dimension in (*left.chain, *right.chain)
                ),
            )
        )
        result[-1] = _with_first(result[-1], left.overall)
        if left.overall_scope != right.overall_scope:
            result[-1] = _review(result[-1], "readers-differ")

        if len(left.chain) != len(right.chain):
            # Do not shift later chain members to compensate for a missing item.
            for chain_index, (left_dim, right_dim) in enumerate(
                zip_longest(left.chain, right.chain)
            ):
                if left_dim is None or right_dim is None:
                    qwen_dim = right_dim
                    result.append(
                        _reading(
                            first.page_index,
                            countertop_index,
                            "chain",
                            chain_index,
                            "review_required",
                            None,
                            "one-reader-missing",
                            qwen_dim,
                        )
                    )
                else:
                    compared = _compare_dimension(
                        first.page_index,
                        countertop_index,
                        "chain",
                        chain_index,
                        left_dim,
                        right_dim,
                    )
                    result.append(
                        _with_first(_review(compared, "reader-topology-differs"), left_dim)
                    )
            continue

        for chain_index, (left_dim, right_dim) in enumerate(zip(left.chain, right.chain)):
            result.append(
                _compare_dimension(
                    first.page_index,
                    countertop_index,
                    "chain",
                    chain_index,
                    left_dim,
                    right_dim,
                )
            )
            result[-1] = _with_first(result[-1], left_dim)
    return tuple(result)


def _missing_countertop_slots(
    page_index: int, countertop_index: int, form: CountertopForm, *, qwen: CountertopForm | None
) -> list[ComparedReading]:
    result = [
        _reading(
            page_index,
            countertop_index,
            "overall",
            None,
            "review_required",
            None,
            "one-reader-missing",
            None if qwen is None else qwen.overall,
        )
    ]
    for chain_index in range(len(form.chain)):
        result.append(
            _reading(
                page_index,
                countertop_index,
                "chain",
                chain_index,
                "review_required",
                None,
                "one-reader-missing",
                None if qwen is None or chain_index >= len(qwen.chain) else qwen.chain[chain_index],
            )
        )
    return result


def _compare_dimension(
    page_index: int,
    countertop_index: int,
    slot: Literal["overall", "chain"],
    chain_index: int | None,
    first: FormDimension | None,
    qwen: FormDimension | None,
    *,
    scope_ok: bool = True,
    scope_stated: bool = True,
    appliance_span: bool = False,
) -> ComparedReading:
    expected_position = None if chain_index is None else chain_index + 1
    if (
        slot == "overall"
        and first is not None
        and qwen is not None
        and (first.position != 0 or qwen.position != 0)
    ):
        return _reading(
            page_index,
            countertop_index,
            slot,
            chain_index,
            "review_required",
            None,
            "reader-topology-differs",
            qwen,
        )
    if (
        slot == "chain"
        and first is not None
        and qwen is not None
        and expected_position is not None
        and (first.position != expected_position or qwen.position != expected_position)
    ):
        return _reading(
            page_index,
            countertop_index,
            slot,
            chain_index,
            "review_required",
            None,
            "reader-topology-differs",
            qwen,
        )
    if first is None or qwen is None:
        return _reading(
            page_index,
            countertop_index,
            slot,
            chain_index,
            "review_required",
            None,
            "one-reader-missing",
            qwen,
        )
    if slot == "overall" and appliance_span:
        return _reading(
            page_index,
            countertop_index,
            slot,
            chain_index,
            "review_required",
            None,
            "appliance-span",
            qwen,
        )
    if slot == "overall" and not scope_ok:
        reason: ReviewReason = "scope-not-run" if scope_stated else "one-reader-missing"
        return _reading(
            page_index, countertop_index, slot, chain_index, "review_required", None, reason, qwen
        )
    left = parse_dimension(first)
    right = parse_dimension(qwen)
    special = _special_reason(left, right)
    if special is not None:
        return _reading(
            page_index,
            countertop_index,
            slot,
            chain_index,
            "review_required",
            None,
            special,
            qwen,
        )
    if left.value is None or right.value is None:
        return _reading(
            page_index,
            countertop_index,
            slot,
            chain_index,
            "review_required",
            None,
            "unreadable",
            qwen,
        )
    if left.value.exact != right.value.exact:
        return _reading(
            page_index,
            countertop_index,
            slot,
            chain_index,
            "review_required",
            None,
            "readers-differ",
            qwen,
        )
    return _reading(
        page_index,
        countertop_index,
        slot,
        chain_index,
        "corroborated",
        left.value,
        None,
        qwen,
    )


def _special_reason(left: ParsedDimension, right: ParsedDimension) -> ReviewReason | None:
    for reason in (left.reason, right.reason):
        if reason == "combined":
            return "combined"
        if reason == "stacked":
            return "stacked"
    return None


def _reading(
    page_index: int,
    countertop_index: int,
    slot: Literal["overall", "chain"],
    chain_index: int | None,
    state: Literal["corroborated", "review_required"],
    value: Measurement | None,
    reason: ReviewReason | None,
    qwen: FormDimension | None,
) -> ComparedReading:
    return ComparedReading(
        page_index=page_index,
        countertop_index=countertop_index,
        slot=slot,
        chain_index=chain_index,
        state=state,
        value=value,
        reason=reason,
        qwen_dimension=qwen,
        first_dimension=None,
    )


def _review(reading: ComparedReading, reason: ReviewReason) -> ComparedReading:
    return ComparedReading(
        page_index=reading.page_index,
        countertop_index=reading.countertop_index,
        slot=reading.slot,
        chain_index=reading.chain_index,
        state="review_required",
        value=None,
        reason=reason,
        qwen_dimension=reading.qwen_dimension,
        first_dimension=reading.first_dimension,
    )


def _with_first(reading: ComparedReading, first: FormDimension | None) -> ComparedReading:
    return ComparedReading(
        page_index=reading.page_index,
        countertop_index=reading.countertop_index,
        slot=reading.slot,
        chain_index=reading.chain_index,
        state=reading.state,
        value=reading.value,
        reason=reading.reason,
        qwen_dimension=reading.qwen_dimension,
        first_dimension=first,
    )
