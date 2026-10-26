"""Two independent readings before any red (final plan #2): made-up findings and labels."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.review.assistant import proof, reading_routes
from app.review.assistant.reading_routes import DRAWN, OCR, TEXT, UNITS

ADVOCATE_PASSED = {
    "model": "devil's advocate",
    "result": "passed",
    "note": "",
    "answers": {"pairing": "code"},
}


@dataclass
class _Check:
    agrees: bool
    drawn_in: float


@dataclass
class _Fit:
    checks: dict[str, _Check] = field(default_factory=dict)

    def check(self, label_id: object) -> _Check | None:
        return self.checks.get(str(label_id))


def _label(key: str, text: str, inches: str, side: str, **more: Any) -> dict[str, Any]:
    return {
        "id": key,
        "page": 1 if side == "vendor" else 2,
        "file": "set.pdf",
        "text": text,
        "inches": inches,
        "side": side,
        "route": "text",
        "box": [10, 10, 30, 18],
        **more,
    }


def _wrong(vendor: dict[str, Any], architect: dict[str, Any], **more: Any) -> dict[str, Any]:
    """A Wrong code proved in every way but its reading: both values printed at their spots and
    code paired the two itself."""
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 1,
        "vs_page": 2,
        "item": "Run: depth",
        "category": "cabinet",
        "vendor": vendor["text"],
        "architect": architect["text"],
        "status": "different",
        "source": "architect",
        "trust_level": "confirmed",
        "witnesses": ["printed"],
        "paired_by": "code",
        "critic": ADVOCATE_PASSED,
        "checks": {
            "printed_vendor": "match",
            "printed_architect": "match",
            "second_read": "agrees",
            "lead_checked": True,
            "critic": "passed",
        },
        "inches": {
            "vendor": vendor["inches"],
            "architect": architect["inches"],
            "difference": "1",
        },
        "dims": {"vendor": vendor, "architect": architect},
    }
    finding.update(more)
    return finding


def _marked(finding: dict[str, Any], fit: _Fit | None) -> dict[str, Any]:
    labels = {(la["file"], la["page"]): [la] for la in finding["dims"].values()}
    reading_routes.mark(finding, lambda f, p: labels.get((f, p)), lambda f, p: fit)
    return finding


def test_text_and_drawn_length_on_both_numbers_may_be_red() -> None:
    fit = _Fit({"D1": _Check(True, 23.0), "D9": _Check(True, 24.0)})
    finding = _marked(
        _wrong(_label("D1", '23"', "23", "vendor"), _label("D9", '24"', "24", "architect")), fit
    )
    assert reading_routes.held(finding) is None
    assert proof.proof_group(finding) == "clouded"
    record = finding[reading_routes.FIELD]
    assert record["enough"] is True
    assert [v["routes"] for v in record["values"]] == [[TEXT, DRAWN], [TEXT, DRAWN]]
    assert reading_routes.agreed(finding)[0]["routes"] == [TEXT, DRAWN]


def test_one_route_only_is_a_likely_problem_never_red() -> None:
    finding = _wrong(_label("D1", '23"', "23", "vendor"), _label("D9", '24"', "24", "architect"))
    _marked(finding, None)
    assert reading_routes.verdict(finding).kind == "missing"
    assert proof.proof_group(finding) == "likely_problem"
    words = proof.please_check_words(finding)
    assert words.startswith("Please check and confirm: the vendor's 23\"")
    assert "is read from the PDF's text only" in words
    assert proof.proof_of(finding)["reason"] == words


def test_millimetres_agreeing_are_a_second_route() -> None:
    finding = _wrong(
        _label("D1", "584 [23]", "23", "vendor", units="agrees"),
        _label("D9", "610 [24]", "24", "architect"),
    )
    _marked(finding, None)
    values = reading_routes.verdict(finding).values
    assert [v.routes for v in values] == [(TEXT, UNITS), (TEXT, UNITS)]
    assert proof.proof_group(finding) == "clouded"


def test_a_drawn_length_that_disagrees_holds_it_with_the_reason() -> None:
    fit = _Fit({"D1": _Check(False, 24.1875), "D9": _Check(True, 24.0)})
    finding = _marked(
        _wrong(
            _label("D1", "584 [23]", "23", "vendor", units="agrees"),
            _label("D9", "610 [24]", "24", "architect"),
        ),
        fit,
    )
    found = reading_routes.verdict(finding)
    assert found.kind == "doubt"
    assert found.reason() == (
        "the vendor's 584 [23] is read from the PDF's text and the millimetres printed with it, "
        "but its drawn length at its view's own scale is 24 3/16\""
    )
    assert proof.proof_group(finding) == "likely_problem"


def test_millimetres_that_disagree_and_a_lost_slash_are_doubts() -> None:
    finding = _marked(
        _wrong(
            _label("D1", "584 [25]", "25", "vendor"),
            _label("D9", '24"', "24", "architect"),
        ),
        _Fit({"D1": _Check(True, 25.0), "D9": _Check(True, 24.0)}),
    )
    assert "millimetres and inches disagree" in (reading_routes.held(finding) or "")
    assert reading_routes.lost_slash('7 14"')
    assert reading_routes.lost_slash("15 12")
    assert not reading_routes.lost_slash('12"')
    assert not reading_routes.lost_slash('7 1/4"')
    assert not reading_routes.lost_slash('7 19"')


def test_an_unsure_second_reading_holds_it() -> None:
    fit = _Fit({"D1": _Check(True, 23.0), "D9": _Check(True, 24.0)})
    finding = _wrong(_label("D1", '23"', "23", "vendor"), _label("D9", '24"', "24", "architect"))
    finding["checks"] = {
        **finding["checks"],
        "second_read": "unsure",
        "adjudication": "unsure",
    }
    _marked(finding, fit)
    assert reading_routes.held(finding) == "a second reading of the drawing was unsure"


def test_ocr_on_the_ink_and_a_pen_stroke_guess() -> None:
    vendor = _label("D1", '23"', "23", "vendor", route="drawn")
    architect = _label("D9", '24"', "24", "architect")
    finding = _wrong(vendor, architect)
    finding["checks"] = {
        **finding["checks"],
        "printed_vendor": "no_text",
        "verify_architect": {
            "result": "match",
            "words": 'Read by code by shape: 24"',
            "value": '24"',
        },
    }
    _marked(finding, _Fit({"D1": _Check(True, 23.0)}))
    values = {v.side: v for v in reading_routes.verdict(finding).values}
    assert values["vendor"].routes == (OCR, DRAWN)
    assert values["architect"].doubts == (
        "it is a pen-stroke reading that no OCR or text reading confirms",
    )
    assert proof.proof_group(finding) == "likely_problem"


def test_a_vendor_string_needs_every_part_read_twice() -> None:
    labels = [
        _label("D1", '30"', "30", "vendor"),
        _label("D2", '12"', "12", "vendor"),
        _label("D3", '17"', "17", "vendor"),
    ]
    finding: dict[str, Any] = {
        "id": "F7",
        "status": "different",
        "source": "vendor_arithmetic",
        "vendor": '30"',
        "architect": '29"',
        "vendor_box": {"file": "set.pdf", "page": 1, "x0": 0, "y0": 0, "x1": 1, "y1": 1},
        "dims": {"vendor": labels[0]},
        "ledger_ids": ["p1.D1", "p1.D2", "p1.D3"],
        "checks": {"printed_vendor": "match", "arithmetic": "does_not_add_up"},
    }
    fit = _Fit({"D1": _Check(True, 30.0), "D2": _Check(True, 12.0)})
    reading_routes.mark(finding, lambda f, p: labels, lambda f, p: fit)
    found = reading_routes.verdict(finding)
    assert [v.label for v in found.values] == ["p1.D1", "p1.D2", "p1.D3"]
    assert found.reason() == (
        "the vendor's 17\" is read from the PDF's text only: no second, different reading agrees "
        "yet (its drawn length, its millimetres or an OCR reading)"
    )
    fit.checks["D3"] = _Check(True, 17.0)
    reading_routes.mark(finding, lambda f, p: labels, lambda f, p: fit)
    assert reading_routes.held(finding) is None


def test_a_string_with_no_record_is_held_and_words_are_never_judged() -> None:
    string = {
        "status": "different",
        "source": "vendor_arithmetic",
        "dims": {"vendor": _label("D1", "584 [23]", "23", "vendor")},
        "checks": {"printed_vendor": "match"},
    }
    assert reading_routes.verdict(string).kind == "missing"
    finish = {"status": "different", "source": "architect", "witnesses": ["text"]}
    assert reading_routes.verdict(finish).judged is False
    assert reading_routes.held(finish) is None


def test_the_record_follows_the_labels_the_item_pairs_now() -> None:
    fit = _Fit({"D1": _Check(True, 23.0), "D9": _Check(True, 24.0)})
    finding = _marked(
        _wrong(_label("D1", '23"', "23", "vendor"), _label("D9", '24"', "24", "architect")), fit
    )
    finding["dims"]["architect"] = _label("D8", '25"', "25", "architect")
    values = {v.side: v for v in reading_routes.verdict(finding).values}
    assert values["architect"].label == "p2.D8"
    assert values["architect"].routes == (TEXT,)


def test_a_typed_number_read_again_by_two_ocr_engines_is_a_second_route() -> None:
    finding = _wrong(_label("D1", '23"', "23", "vendor"), _label("D9", '24"', "24", "architect"))
    labels = {(la["file"], la["page"]): [la] for la in finding["dims"].values()}
    reading_routes.mark(
        finding, lambda f, p: labels.get((f, p)), lambda f, p: None, lambda label: ("agrees", None)
    )
    assert [v.routes for v in reading_routes.verdict(finding).values] == [(TEXT, OCR), (TEXT, OCR)]
    assert proof.proof_group(finding) == "clouded"
    reading_routes.mark(
        finding,
        lambda f, p: labels.get((f, p)),
        lambda f, p: None,
        lambda label: ("disagrees", '28"'),
    )
    assert reading_routes.held(finding) == (
        "the vendor's 23\" is read from the PDF's text, but two OCR engines read 28\" at its spot"
    )


def test_the_number_readers_ruler_and_stroke_shapes_are_other_routes() -> None:
    finding = _wrong(
        _label("D1", '3/4"', "0.75", "vendor", route="drawn"),
        _label("D9", '24"', "24", "architect"),
    )
    labels = {(la["file"], la["page"]): [la] for la in finding["dims"].values()}
    finding["checks"] = {**finding["checks"], "printed_vendor": "no_text"}
    fit = _Fit({"D9": _Check(True, 24.0)})
    reading_routes.mark(
        finding,
        lambda f, p: labels.get((f, p)),
        lambda f, p: fit,
        None,
        lambda label: (("ocr", "ruler"), None) if label["id"] == "D1" else None,
    )
    values = {v.side: v for v in reading_routes.verdict(finding).values}
    assert values["vendor"].routes == (OCR, DRAWN)
    reading_routes.mark(
        finding,
        lambda f, p: labels.get((f, p)),
        lambda f, p: fit,
        None,
        lambda label: ((), '1/4"') if label["id"] == "D1" else None,
    )
    assert reading_routes.held(finding) == (
        "the vendor's 3/4\" is read from two OCR engines on the drawn ink, but code's number "
        'reader reads 1/4" at its spot'
    )
