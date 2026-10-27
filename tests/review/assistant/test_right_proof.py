"""A Right needs code's proof that both dimensions measure the same thing: made-up drawings, no
client data.

Two drawings of a kitchenette elevation. The reference prints 4" from the floor to an outlet once.
The vendor prints a 4" toe kick at the floor and a 4" band at the counter's edge. An item the AI
called the same pairs one of the vendor's 4" sizes with the reference's.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import dim_ends, proof, right_proof, row_places, same_ends
from app.review.assistant.chat_trust import judged
from app.review.assistant.escalation import EscalationContext, escalate

VENDOR = "vendor.pdf"
REFERENCE = "reference.pdf"


def _label(
    label_id: str,
    inches: str,
    side: str,
    *,
    at: float,
    low: float,
    high: float,
    way: str = "v",
    line: bool = True,
) -> dict[str, Any]:
    """A printed size beside its line: an upright line at x=`at` from y=`low` to `high` (or a
    level one at y=`at` from x=`low` to `high`)."""
    middle = (low + high) / 2
    if way == "v":
        box = [at - 12, middle - 3, at - 2, middle + 3]
        ends = [[at, low], [at, high]]
    else:
        box = [middle - 5, at - 10, middle + 5, at - 2]
        ends = [[low, at], [high, at]]
    return {
        "id": label_id,
        "text": f'{inches}"',
        "inches": inches,
        "box": box,
        "side": side,
        "orientation": way,
        "route": "text",
        "dim_line": ends if line else None,
    }


def _finding(vendor_id: str, reference_id: str, value: str = "4") -> dict[str, Any]:
    return {
        "id": "F1",
        "page": 1,
        "vs_page": 1,
        "item": "Kitchenette: floor to outlet",
        "vendor": f'{value}"',
        "architect": f'{value}"',
        "status": "same",
        "source": "architect",
        "category": "cabinet",
        "created_by": "helper:1",
        "inches": {"vendor": value, "architect": value, "difference": "0"},
        "checks": {"printed_vendor": "match", "printed_architect": "match"},
        "witnesses": ["printed"],
        "dims": {
            "vendor": {"id": vendor_id, "page": 1, "file": VENDOR, "inches": value},
            "architect": {"id": reference_id, "page": 1, "file": REFERENCE, "inches": value},
        },
    }


def _reference() -> list[dict[str, Any]]:
    return [
        _label("R1", "4", "architect", at=400, low=700, high=708),  # floor to the outlet
        _label("R2", "36", "architect", at=420, low=636, high=708),
    ]


def _vendor(*, band: bool = True) -> list[dict[str, Any]]:
    found = [
        _label("V1", "4", "vendor", at=100, low=752, high=760),  # the toe kick, at the floor
        _label("V2", "36", "vendor", at=120, low=688, high=760),
    ]
    if band:
        found.append(_label("V3", "4", "vendor", at=100, low=620, high=628))  # the counter's band
    return found


def _reader(vendor: list[dict[str, Any]], reference: list[dict[str, Any]]) -> right_proof.Labels:
    pages = {VENDOR: vendor, REFERENCE: reference}

    def read(file: str, page: int) -> list[dict[str, Any]]:
        return [{**label, "file": file, "page": page} for label in pages.get(file, [])]

    return read


def _judge(finding: dict[str, Any], vendor: list[dict[str, Any]]) -> dict[str, Any] | None:
    return right_proof.judge(finding, _reader(vendor, _reference()))


# ---- the only such size on both drawings ---------------------------------------------------------


def test_the_only_such_size_on_both_drawings_proves_the_right() -> None:
    record = _judge(_finding("V1", "R1"), _vendor(band=False))
    assert record is not None
    assert record["proved"] is True
    assert record["how"] == "only_size"
    assert record["vendor"] == f"{VENDOR}|1|V1"
    assert record["architect"] == f"{REFERENCE}|1|R1"


def test_two_such_sizes_on_one_drawing_prove_nothing() -> None:
    record = _judge(_finding("V3", "R1"), _vendor())
    assert record is not None
    assert record["proved"] is False
    assert record["how"] == "none"
    assert "the vendor's drawing prints 2 dimensions of that size" in record["why"]


def test_the_same_size_printed_again_at_the_same_place_is_one_dimension() -> None:
    # A plan above its elevation prints the run's 60" twice, its line's ends at the same places.
    vendor = [
        _label("V1", "60", "vendor", at=100, low=200, high=320, way="h"),
        _label("V2", "60", "vendor", at=400, low=200.5, high=320.5, way="h"),
    ]
    reference = [_label("R1", "60", "architect", at=100, low=50, high=230, way="h")]
    finding = _finding("V2", "R1", "60")
    assert right_proof.only_size(vendor[1], vendor) == 1
    record = right_proof.judge(finding, _reader(vendor, reference))
    assert record is not None and record["proved"] is True


def test_texts_with_no_line_count_as_copies_only_at_the_same_place_along_the_run() -> None:
    copies = [
        _label("V1", "72", "vendor", at=50, low=460, high=480, way="h", line=False),
        _label("V2", "72", "vendor", at=120, low=460, high=480, way="h", line=False),
    ]
    assert right_proof.only_size(copies[0], copies) == 1
    apart = [*copies, _label("V3", "72", "vendor", at=50, low=660, high=680, way="h", line=False)]
    assert right_proof.only_size(apart[0], apart) == 2


def test_sizes_running_the_other_way_or_on_the_other_side_are_not_counted() -> None:
    labels = [
        _label("V1", "4", "vendor", at=100, low=752, high=760),
        _label("V2", "4", "vendor", at=300, low=200, high=208, way="h"),
        _label("R9", "4", "architect", at=100, low=600, high=608),
    ]
    assert right_proof.only_size(labels[0], labels) == 1


# ---- code's geometry ------------------------------------------------------------------------------


def test_geometry_that_ties_an_end_elsewhere_takes_the_right_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prove(*_args: Any, **_kw: Any) -> tuple[str, str]:
        return "different_extent", "one end lands where the other drawing has another place"

    monkeypatch.setattr(same_ends, "prove_labels", prove)
    record = _judge(_finding("V3", "R1"), _vendor(band=False) + [_vendor()[2]])
    assert record is not None
    assert record["proved"] is False
    assert record["how"] == "different"
    assert "another place" in record["why"]


def test_geometry_that_proves_the_same_ends_keeps_the_right(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prove(*_args: Any, **_kw: Any) -> tuple[str, str]:
        return "same_extent", "both ends are tied on both drawings (the floor and the outlet)"

    monkeypatch.setattr(same_ends, "prove_labels", prove)
    record = _judge(_finding("V1", "R1"), _vendor())
    assert record is not None
    assert record["proved"] is True
    assert record["how"] == "geometry"


def test_a_plan_drawn_turned_is_not_taken_as_different_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prove(*_args: Any, **_kw: Any) -> tuple[str, str]:
        return "different_extent", "one is a width and the other a height"

    monkeypatch.setattr(same_ends, "prove_labels", prove)
    vendor = [_label("V1", "25", "vendor", at=100, low=200, high=250, way="h")]
    reference = [_label("R1", "25", "architect", at=300, low=400, high=450)]
    record = right_proof.judge(_finding("V1", "R1", "25"), _reader(vendor, reference))
    assert record is not None
    assert record["how"] == "only_size"
    assert record["proved"] is True


# ---- what is left as it is ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"paired_by": "code"},
        {"formula": {"id": "chain", "outcome": "same"}},
        {"field_cut": {"outcome": "allowed"}},
        {"created_by": "sink_centre"},
        {"decision": {"decision": "agree"}},
        {"status": "different"},
    ],
)
def test_items_code_compared_itself_need_no_record(extra: dict[str, Any]) -> None:
    assert _judge({**_finding("V3", "R1"), **extra}, _vendor()) is None


def test_a_row_piece_code_placed_and_ends_proved_alike_need_no_record() -> None:
    placed = _finding("V3", "R1")
    placed[row_places.FIELD] = {
        "outcome": "same_place",
        "vendor": row_places.side_key(placed, "vendor"),
        "architect": row_places.side_key(placed, "architect"),
    }
    assert _judge(placed, _vendor()) is None
    alike = _finding("V3", "R1")
    alike[dim_ends.LIKE_FOR_LIKE] = {
        "proved": True,
        "vendor": f"{VENDOR}|1|V3",
        "architect": f"{REFERENCE}|1|R1",
    }
    assert _judge(alike, _vendor()) is None


def test_a_label_code_never_read_proves_nothing() -> None:
    record = _judge(_finding("V7", "R1"), _vendor())
    assert record is not None
    assert record["proved"] is False


# ---- the outcome ------------------------------------------------------------------------------


def _escalated(finding: dict[str, Any], vendor: list[dict[str, Any]]) -> dict[str, Any]:
    ctx = EscalationContext(
        main_file=VENDOR,
        labels={(VENDOR, 1): vendor, (REFERENCE, 1): _reference()},
    )
    result = escalate([finding], ctx)
    changed = {str(f["id"]): f for f in result.changed}
    return judged(changed.get("F1", finding))


def test_an_unproved_right_stays_right_marked_ends_not_proved() -> None:
    # Anant's option B (2026-10-27): with no proof either way the item stays a Right, named
    # "Matches (ends not proved)" on its card.
    found = _escalated(_finding("V3", "R1"), _vendor())
    assert found[right_proof.FIELD]["proved"] is False
    assert found[right_proof.FIELD]["how"] == "none"
    assert proof.proof_group(found) == "right"
    assert proof.matches_unproved(found)
    assert proof.MATCHES_UNPROVED == "Matches (ends not proved)"


def test_a_proved_right_stays_right_and_is_not_named_unproved() -> None:
    found = _escalated(_finding("V1", "R1"), _vendor(band=False))
    assert found[right_proof.FIELD]["proved"] is True
    found[dim_ends.LIKE_FOR_LIKE] = {
        "proved": False,
        "vendor": f"{VENDOR}|1|V1",
        "architect": f"{REFERENCE}|1|R1",
    }
    assert proof.proof_group(found) == "right"
    assert not proof.matches_unproved(found)


def test_different_ends_are_different_reference_points() -> None:
    finding = _finding("V3", "R1")
    finding[right_proof.FIELD] = {
        "proved": False,
        "how": "different",
        "why": "the drawn run: one end lands where the other drawing has another place",
        "vendor": f"{VENDOR}|1|V3",
        "architect": f"{REFERENCE}|1|R1",
    }
    classified = proof.classified(finding)
    assert classified.outcome == "not_compared"
    assert classified.reason == "different_reference"
    assert classified.why is not None
    assert classified.why.startswith("the two sizes do not measure the same thing")
    assert "AI" not in classified.why
    assert proof.proof_group(finding) == "not_compared"
    assert not proof.matches_unproved(finding)


def test_a_stale_record_is_ignored_once_the_item_pairs_other_labels() -> None:
    finding = _finding("V3", "R1")
    finding[right_proof.FIELD] = {
        "proved": False,
        "how": "different",
        "why": "the drawn run: one end lands where the other drawing has another place",
        "vendor": f"{VENDOR}|1|V3",
        "architect": f"{REFERENCE}|1|R1",
    }
    finding["dims"]["vendor"]["id"] = "V1"
    assert right_proof.different(finding) is None
    assert right_proof.ends_not_proved(finding) is None
    assert proof.proof_group(finding) == "right"


def test_a_saved_right_with_no_record_keeps_its_outcome() -> None:
    assert proof.proof_group(_finding("V3", "R1")) == "right"
