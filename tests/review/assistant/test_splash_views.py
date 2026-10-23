"""The splash step's code rules (`splash_views`), on made-up facts, $0, no AI, no PDF.

The whole-pipeline cases (a section beside the paired plan, a callout to a section sheet, the
brand prototype, pen strokes, measured by scale) are in `test_hard_uploads.py`; here the rules
themselves: which part a text names, like for like, what the architect's words print, a
whole-drawing look's "no splash drawn", the job value, and the escalation's Right.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import pytest

from app.review.assistant import classification, proof, splash_views
from app.review.assistant.escalation import EscalationContext, escalate
from app.review.assistant.splash_views import Companion, Part, Search, part_of

FILE = "arch.pdf"
SECTION = Companion(
    FILE, 2, "section", "VANITY SECTION", (0.0, 0.0, 400.0, 400.0), "the same sheet"
)
ELEVATION = Companion(
    FILE, 2, "elevation", "VANITY ELEVATION", (400.0, 0.0, 800.0, 400.0), "the same sheet"
)


def _phrase(text: str, x: float = 100.0, y: float = 100.0) -> dict[str, Any]:
    return {"text": text, "box": [x, y, x + 60, y + 8], "side": "architect"}


def _splash(**more: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 1,
        "vs_page": 2,
        "item": "Vanity: back splash height",
        "vendor": '4"',
        "architect": "no size printed",
        "status": "check",
        "keep_reason": "architect_prints_nothing",
        "inches": {"vendor": "4"},
        "checks": {"printed_vendor": "match"},
        "source": "architect",
        "category": "countertop",
    }
    finding.update(more)
    return finding


@pytest.mark.parametrize(
    ("text", "part"),
    [
        ("Vanity: back splash height", Part("splash", "back", "height")),
        ('4" SIDE SPLASH', Part("splash", "side", None)),
        ("Makeup counter: back splash length", Part("splash", "back", "length")),
        ('BACKSPLASH 3/4" THK', Part("splash", "back", "thickness")),
        ("back splash and 2 side splashes drawn", Part("splash", None, None)),
        ("Vanity top overall width", None),
    ],
)
def test_the_part_a_text_names(text: str, part: Part | None) -> None:
    assert part_of(text) == part


def test_what_the_architects_words_print() -> None:
    def read(text: str) -> list[tuple[Fraction, str | None]]:
        found = splash_views.note_readings(SECTION, [_phrase(text)], splash_views._rules())
        return [(r.value, r.part.measure) for r in found]

    assert read('4" BACKSPLASH') == [(Fraction(4), "height")]
    assert read('4" HIGH BACKSPLASH') == [(Fraction(4), "height")]
    assert read('BACKSPLASH 3/4" THK') == [(Fraction(3, 4), "thickness")]
    assert read('3/4" THK x 4" H QUARTZ BACKSPLASH') == [
        (Fraction(3, 4), "thickness"),
        (Fraction(4), "height"),
    ]
    # A size whose measure the words do not say is never used.
    assert read('3/4" X 4" QUARTZ BACKSPLASH') == []
    assert read("SOLID SURFACE SPLASH SS 01") == []
    assert read("BACKSPLASH 20 mm") == []  # millimetres never decide
    # Outside the view: not the view's.
    outside = splash_views.note_readings(
        SECTION, [_phrase('4" BACKSPLASH', 500, 500)], splash_views._rules()
    )
    assert outside == []


def test_like_for_like() -> None:
    near = {(FILE, 2): [_phrase("SPLASH", 100, 100)]}
    label = {"text": '4"', "box": [110, 120, 120, 128], "file": FILE, "page": 2}
    named = _splash(status="same", architect='4"', dims={"architect": label})
    # The architect's label is beside its own word "SPLASH": alike.
    assert splash_views.unlike(named, near) is None
    # Nothing names the architect's size a splash's: not alike (the meeting set's 36" top width).
    assert splash_views.unlike(named, {}) is not None
    side = _splash(item="Vanity: side splash height", architect='4" BACKSPLASH')
    assert "side splash against a back splash" in str(splash_views.unlike(side))
    other = _splash(architect='1 1/2" THK BACKSPLASH')
    assert "height against its thickness" in str(splash_views.unlike(other))
    assert splash_views.unlike(_splash(item="Vanity top width", vendor='36"')) is None
    # A splash named only as another dimension's end is not a splash item.
    banded = _splash(item="Vanity: counter height to the top of the splash", vendor='38"')
    assert splash_views.unlike({**banded, "architect": '38"', "status": "same"}) is None
    outlet = _splash(item="Outlet in the backsplash: height", vendor='42"')
    assert splash_views.candidates([outlet], splash_views._rules()) == []


def test_a_right_on_unlike_parts_is_not_compared() -> None:
    right = _splash(item="Counter: back splash length", vendor='36"', architect="914 [36]")
    right.update(status="same", dims={"architect": {"text": "914 [36]", "box": [0, 0, 9, 9]}})
    down = splash_views.downgrade_unlike(right, {})
    assert down is not None and down["status"] == "check"
    said = classification.classify(down, proved=False)
    assert said.reason == "different_reference" and said.why.startswith("not like for like")
    # Code's own geometric pairing and the reviewer's decision stand.
    assert splash_views.downgrade_unlike({**right, "paired_by": "code"}, {}) is None
    assert splash_views.downgrade_unlike({**right, "decision": {"decision": "agree"}}, {}) is None


def test_the_escalations_right_needs_like_for_like() -> None:
    # The meeting set's false Right: code read 36" at both spots, a splash length against the
    # plan's top width.
    labels = {
        ("shop.pdf", 8): [
            {"id": "D1", "text": '36"', "inches": "36", "box": [10, 10, 20, 18], "side": "vendor"}
        ],
        (FILE, 20): [
            {
                "id": "D8",
                "text": "914 [36]",
                "inches": "36",
                "box": [10, 10, 20, 18],
                "side": "architect",
            }
        ],
    }
    finding = _splash(
        page=8,
        vs_page=20,
        item="Makeup counter: back splash length",
        vendor='36"',
        architect="914 [36]",
        inches={"vendor": "36", "architect": "36"},
        checks={},
        vendor_box={"x0": 5, "y0": 5, "x1": 25, "y1": 22, "page": 8, "file": "shop.pdf"},
        arch_box={"x0": 5, "y0": 5, "x1": 25, "y1": 22, "page": 20, "file": FILE},
    )
    finding.pop("keep_reason")
    result = escalate([finding], EscalationContext(main_file="shop.pdf", labels=labels))
    found = next(f for f in result.changed if f["id"] == "F1")
    assert found["status"] == "check" and proof.proof_group(found) == "not_compared"
    assert found["splash_views"]["outcome"] == "not_like_for_like"
    # The same item with the architect's own word beside its label is settled Right.
    phrases = {(FILE, 20): [_phrase("BACKSPLASH", 22, 10)]}
    result = escalate(
        [dict(finding)], EscalationContext(main_file="shop.pdf", labels=labels, phrases=phrases)
    )
    assert next(f for f in result.changed if f["id"] == "F1")["status"] == "same"


def _look_said(**more: Any) -> dict[str, Any]:
    return _splash(
        item='this piece: back splash 4" added',
        vendor='4" back splash, 49" long',
        architect="no splash drawn in the elevation or the plan",
        whole_look={"by": "whole_drawing_look", "kind": "extra_piece", "proof": "likely_only"},
        created_by="whole_look",
        **more,
    )


def test_no_splash_drawn_is_kept_only_when_code_has_no_doubt() -> None:
    phrases: dict[Any, Any] = {}
    # Nothing code reads says otherwise: the look's Likely problem stays as it is.
    assert splash_views.settle(_look_said(), Search((ELEVATION,)), phrases) is None
    # The page holds numbers in pen strokes code cannot read (a section drawn in strokes): code
    # cannot confirm "no splash drawn", so it is no Likely problem.
    doubt = Search((ELEVATION,), stray={2: 17})
    settled = splash_views.settle(_look_said(), doubt, phrases)
    assert settled is not None
    found = settled.finding
    assert "whole_look" not in found and proof.proof_group(found) == "not_compared"
    assert found["architect"] == "not confirmed by code"
    assert "17 elsewhere on page 2" in found["splash_views"]["why"]
    # A view that prints the splash's size: compared like any item.
    sized = {(FILE, 2): [_phrase('4" BACKSPLASH', 500, 100)]}
    found = splash_views.settle(_look_said(), Search((ELEVATION,)), sized).finding
    assert found["status"] == "same" and "whole_look" not in found


def test_a_thickness_no_view_prints_takes_the_job_value() -> None:
    job = {"thickness": splash_views.JobValue(Fraction(3, 4), '3/4"', "this project's value")}
    thick = _splash(item="Vanity: back splash thickness", vendor='3/4"', inches={"vendor": "0.75"})
    found = splash_views.settle(thick, Search((SECTION,)), {}, job=job).finding
    assert found["status"] == "same" and proof.proof_group(found) == "right"
    assert "job value" in found["architect"]
    other = {**thick, "vendor": '1"', "inches": {"vendor": "1"}}
    found = splash_views.settle(other, Search((SECTION,)), {}, job=job).finding
    # A different one is a Likely problem, never red.
    assert proof.proof_group(found) == "likely_problem"
    # A height never takes the thickness's job value.
    found = splash_views.settle(_splash(), Search((SECTION,)), {}, job=job).finding
    assert found["status"] == "check" and "job_value" not in found["splash_views"]


def test_only_splash_sizes_are_looked_for() -> None:
    rules = splash_views._rules()
    material = _splash(item="Counter: top/splash material", vendor="Calacatta stone, 2cm")
    material.pop("inches")
    sized_by_architect = _splash(architect='4"')
    decided = _splash(decision={"decision": "agree"})
    found = splash_views.candidates([material, sized_by_architect, decided, _splash()], rules)
    assert [f["id"] for f in found] == ["F1"] and found[0]["architect"] == "no size printed"


def test_the_small_ai_question_is_off_unless_asked() -> None:
    off = splash_views.settle(_splash(), Search((SECTION,)), {}).finding
    assert splash_views.look_in_line(off) is None
    on = splash_views.settle(_splash(), Search((SECTION,)), {}, question=True).finding
    assert splash_views.look_in_line(on) == (
        "look for the splash size in: the section 'VANITY SECTION' (page 2)"
    )
