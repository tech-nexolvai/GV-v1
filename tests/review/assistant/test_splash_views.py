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
    assert read('BACKSPLASH HEIGHT: 4"') == [(Fraction(4), "height")]
    # A size counts only right beside the part's name: the others are another part's.
    assert read('24" WIDE COUNTERTOP W/ 4" BACKSPLASH') == [(Fraction(4), "height")]
    assert read('25" DEEP COUNTERTOP W/ BACKSPLASH') == []
    assert read('3/4" THK x 4" H QUARTZ BACKSPLASH') == []
    # A general note is never one item's size.
    assert read('ALL BACKSPLASHES 4" HIGH U.O.N.') == []
    assert read('4" BACKSPLASH TYP.') == [(Fraction(4), "height")]
    # Two sizes joined as one callout: which is the height is not said.
    assert read('4" X 3/4" BACKSPLASH') == []
    # A size after the part's name only when the phrase starts with it.
    assert read('COUNTERTOP W/ BACKSPLASH 25" DEEP') == []
    assert read('4" BACKSPLASH, 24" WIDE') == [(Fraction(4), "height")]
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
    label = {"text": '4"', "box": [110, 120, 120, 128], "file": FILE, "page": 2}
    named = _splash(
        status="same",
        architect='4"',
        dims={"architect": label},
        inches={"vendor": "4", "architect": "4"},
    )
    # A phrase beside the architect's label prints its same value as the splash's: alike.
    assert splash_views.unlike(named, {(FILE, 2): [_phrase('4" BACKSPLASH', 100, 100)]}) is None
    # A splash word near the label proves nothing (the label may be the top's own size).
    assert splash_views.unlike(named, {(FILE, 2): [_phrase("SPLASH", 100, 100)]}) is not None
    assert splash_views.unlike(named, {}) is not None
    wide = _splash(
        item="Vanity: back splash length",
        vendor='36"',
        status="same",
        architect='36"',
        dims={"architect": {**label, "text": '36"'}},
        inches={"vendor": "36", "architect": "36"},
    )
    for near in ('4" BACKSPLASH', 'COUNTERTOP 36" WIDE W/ BACKSPLASH'):
        assert splash_views.unlike(wide, {(FILE, 2): [_phrase(near, 100, 100)]}) is not None

    def printed(text: str) -> dict[str, Any]:
        return {"dims": {"architect": {"text": text, "box": [0, 0, 9, 9], "file": FILE, "page": 2}}}

    side = _splash(item="Vanity: side splash height", **printed('4" BACKSPLASH'))
    assert "side splash against a back splash" in str(splash_views.unlike(side))
    other = _splash(**printed('BACKSPLASH 1 1/2" THK'))
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
    phrases = {(FILE, 20): [_phrase('BACKSPLASH 36" LONG', 22, 10)]}
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
    # A few unread numbers on the page are no doubt (a hand-drafted page misses some).
    few = Search((ELEVATION,), stray={2: 2}, doubt=False)
    assert splash_views.settle(_look_said(), few, phrases) is None
    doubt = Search((ELEVATION,), stray={2: 17}, doubt=True)
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
    material = _splash(item="Counter: top/splash material", vendor="Quartz, 2cm")
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
    # On, the item joins the roster's existing look-again (no new call); off, nothing does.
    assert splash_views.splash_question_items([off]) == []
    assert [f["id"] for f in splash_views.splash_question_items([on])] == ["F1"]
    assert splash_views.splash_question_items([on], done={"F1"}) == []


def test_views_ask_the_architect_only_like_for_like() -> None:
    rules = splash_views._rules()
    phrases = {(FILE, 2): [_phrase('4" BACKSPLASH', 100, 100), _phrase('6" SIDE SPLASH', 500, 100)]}
    vague = _splash(item="Vanity: splash height")
    settled = splash_views.settle(vague, Search((SECTION, ELEVATION)), phrases, rules=rules)
    # The back splash and the side splash are two parts, not two views disagreeing; the vendor's
    # value does not say which it is: Not compared, no question to the architect.
    assert settled.added == [] and settled.finding["status"] == "check"
    # One view printing the same part two ways: GV asks the architect.
    twice = {(FILE, 2): [_phrase('4" BACKSPLASH', 100, 100), _phrase('6" BACKSPLASH', 100, 300)]}
    settled = splash_views.settle(_splash(), Search((SECTION,)), twice, rules=rules)
    [asked] = settled.added
    assert asked["why"].startswith("The architect's own drawings disagree: the section")
    assert "its the" not in asked["why"]


def test_a_splash_right_made_by_a_later_step_needs_like_for_like() -> None:
    from app.review.assistant.round_reference import point_at

    # The round reference links the vendor's label to an architect label another item compared:
    # a splash length against an architect size code never saw printed as the splash's.
    finding = _splash(
        item="Counter: back splash length",
        vendor='36"',
        inches={"vendor": "36"},
        round_match={"vendor": "p3.D1"},
    )
    label = {"id": "D8", "text": "914 [36]", "inches": "36", "page": 2, "file": FILE,
             "box": [0, 0, 9, 9], "side": "architect", "route": "text"}  # fmt: skip
    point_at(finding, label, "linked", "another item", by_code=False)
    assert finding["status"] == "check" and proof.proof_group(finding) == "not_compared"
    assert finding["splash_views"]["outcome"] == "not_like_for_like"
    # Code's own pairing by geometry stands.
    paired = _splash(item="Counter: back splash length", vendor='36"', inches={"vendor": "36"})
    point_at(paired, label, "linked", "code's pairs", by_code=True)
    assert paired["status"] == "same"
    # The AI's typed words for the architect's side never prove it; a size beside "with the
    # backsplash" is no splash item.
    typed = _splash(item="Counter: back splash length", vendor='36"', status="same")
    assert splash_views.downgrade_unlike({**typed, "architect": '36" long backsplash'}, {})
    depth = _splash(item="Countertop depth with backsplash", vendor='25 1/2"', status="same")
    assert splash_views.downgrade_unlike(depth, {}) is None


def test_the_escalation_keeps_the_splash_steps_reason() -> None:
    labels = {
        ("shop.pdf", 8): [
            {"id": "D1", "text": '4"', "inches": "4", "box": [10, 10, 20, 18], "side": "vendor"}
        ],
        (FILE, 20): [
            {"id": "D8", "text": '4"', "inches": "4", "box": [10, 10, 20, 18], "side": "architect"}
        ],
    }
    record = {"outcome": "not_compared", "why": "the architect shows no splash size (looked at: x)"}
    finding = _splash(
        page=8,
        vs_page=20,
        inches={"vendor": "4", "architect": "4"},
        checks={},
        vendor_box={"x0": 5, "y0": 5, "x1": 25, "y1": 22, "page": 8, "file": "shop.pdf"},
        arch_box={"x0": 5, "y0": 5, "x1": 25, "y1": 22, "page": 20, "file": FILE},
        splash_views=record,
    )
    result = escalate([finding], EscalationContext(main_file="shop.pdf", labels=labels))
    found = next((f for f in result.changed if f["id"] == "F1"), finding)
    assert found["status"] == "check"
    assert found["splash_views"]["why"] == record["why"]


def test_the_resolver_never_remakes_an_unlike_splash_right() -> None:
    from app.review.assistant import decision_resolver as dr

    # The roster's finish runs the resolver after this step: a splash length against an
    # architect size code never saw printed as the splash's stays Not compared.
    right = _splash(
        item="Vanity: back splash length",
        vendor='36"',
        architect='36"',
        status="same",
        inches={"vendor": "36", "architect": "36"},
        checks={"printed_vendor": "match", "printed_architect": "match"},
        dims={
            "architect": {"id": "D8", "text": '36"', "box": [0, 0, 9, 9], "file": FILE, "page": 2}
        },
    )
    down = splash_views.downgrade_unlike(right, {})
    assert down is not None
    resolved = dr.resolve([down], dr.ResolverContext())
    found = next((f for f in resolved.changed if f["id"] == "F1"), down)
    assert found["status"] == "check" and proof.proof_group(found) == "not_compared"


def test_no_sharper_read_is_paid_for_an_unlike_splash() -> None:
    from app.review.assistant import decision_resolver as dr

    asked: list[Any] = []

    def reader(*args: Any, **kwargs: Any) -> Any:
        asked.append(args)
        raise AssertionError("never asked")

    unread = _splash(
        item="Vanity: back splash length",
        vendor="illegible",
        architect='36"',
        inches={"architect": "36"},
        keep_reason="unreadable",
        checks={"printed_architect": "match"},
        vendor_box={"x0": 5, "y0": 5, "x1": 25, "y1": 22, "page": 1, "file": "shop.pdf"},
        dims={
            "architect": {"id": "D8", "text": '36"', "box": [0, 0, 9, 9], "file": FILE, "page": 2}
        },
    )
    settled, _reason, said = dr._settle_one(unread, dr.ResolverContext(read=reader))
    assert settled is None and said is None and asked == []


# ---- review of #1918 ------------------------------------------------------------------------------


def test_a_splash_whose_kind_is_said_on_one_side_only_is_never_alike() -> None:
    rules = splash_views._rules()
    vague = _splash(item="Vanity: splash height")
    side = {(FILE, 2): [_phrase('4" SIDE SPLASH', 100, 100)]}
    found = splash_views.settle(vague, Search((SECTION,)), side, rules=rules).finding
    assert found["status"] == "check" and proof.proof_group(found) != "right"
    # A splash of no said kind and a side splash in two views: two parts, no question.
    two = {(FILE, 2): [_phrase('4" SPLASH', 100, 100), _phrase('6" SIDE SPLASH', 500, 100)]}
    settled = splash_views.settle(_splash(), Search((SECTION, ELEVATION)), two, rules=rules)
    assert settled.added == []
    # A vendor value whose measure is not said is not alike to a printed height.
    label = {"text": '4" BACKSPLASH', "box": [0, 0, 9, 9], "file": FILE, "page": 2}
    unsaid = _splash(item="Vanity: back splash", vendor='4"', dims={"architect": label})
    assert splash_views.unlike(unsaid) is not None


def test_a_side_splash_never_takes_the_backsplash_job_value() -> None:
    job = {"thickness": splash_views.JobValue(Fraction(3, 4), '3/4"', "this project's value")}
    for item in ("Vanity: side splash thickness", "Vanity: splash thickness"):
        thick = _splash(item=item, vendor='3/4"', inches={"vendor": "0.75"})
        found = splash_views.settle(thick, Search((SECTION,)), {}, job=job).finding
        assert found["status"] == "check" and "job_value" not in found["splash_views"], item


def test_the_job_value_is_a_job_entry_never_a_company_default(tmp_path: Any) -> None:
    from types import SimpleNamespace

    def engine(origin: str) -> Any:
        skills = tmp_path / origin.replace(" ", "_")
        skills.mkdir()
        (skills / "rules.md").write_text(
            '| Backsplash thickness (`backsplash_thickness`) | 3/4" | ' + origin + " |\n",
            encoding="utf-8",
        )
        return SimpleNamespace(tools=SimpleNamespace(workspace=SimpleNamespace(skills=skills)))

    assert splash_views._job_values(engine("GV's company standard")) == {}
    assert "thickness" in splash_views._job_values(engine("this project's value"))


def test_a_countertop_depth_including_the_splash_is_no_splash_item() -> None:
    rules = splash_views._rules()
    for item in (
        "Countertop depth incl. backsplash",
        "Countertop depth excl. backsplash",
        "Top depth + backsplash",
        "Top depth & backsplash",
    ):
        finding = _splash(item=item, vendor='25 1/2"', inches={"vendor": "25.5"})
        assert splash_views.candidates([finding], rules) == [], item


def test_a_likely_problem_says_what_code_compared() -> None:
    sized = {(FILE, 2): [_phrase('4" HIGH BACKSPLASH', 100, 100)]}
    six = _splash(vendor='6"', inches={"vendor": "6"})
    found = splash_views.settle(six, Search((SECTION,)), sized).finding
    assert proof.proof_group(found) == "likely_problem"
    why = proof.classified(found).why
    assert '4" HIGH BACKSPLASH' in why and "gives no size" not in why


def _drawing(title: str, x: float, file: str = FILE, page: int = 2) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        titles=(title,) if title else (),
        file=file,
        own_page=page,
        box=(x, 0.0, x + 300.0, 300.0),
        labels=(),
        tags=(),
    )


def _work(views: list[Any], others: list[Any] = ()) -> Any:  # type: ignore[assignment]
    from types import SimpleNamespace

    sheet = SimpleNamespace(
        id="a1", file=FILE, page=2, views=views, drawings=views, project=None,
        reference=False, key_plan=False,
    )  # fmt: skip
    pair = SimpleNamespace(
        vendor=SimpleNamespace(file="shop.pdf", own_page=1),
        candidates=[sheet, *others],
        chosen=("a1",),
    )
    return SimpleNamespace(file="shop.pdf", across=[pair])


def test_the_same_sheet_gives_only_this_items_views() -> None:
    facts = splash_views.Facts()
    finding = _splash(item="Break room: back splash height")
    # A trade word in common is no item: the reception's countertop section is not used.
    views = [
        _drawing("BREAK ROOM COUNTERTOP PLAN", 0),
        _drawing("RECEPTION COUNTERTOP SECTION", 400),
        _drawing("SECTION", 800),
        _drawing("", 1200),
    ]
    found = splash_views.companion_views(_work(views), finding, facts)
    assert [v.title for v in found.views] == ["BREAK ROOM COUNTERTOP PLAN"]
    # A sheet of this item only: its bare SECTION and untitled views are its own.
    views = [_drawing("BREAK ROOM PLAN", 0), _drawing("SECTION", 400), _drawing("", 800)]
    found = splash_views.companion_views(_work(views), finding, facts)
    assert len(found.views) == 3
    # The item's words not known: a sheet that names items gives none of its views.
    unknown = _splash(item="Back splash height")
    views = [_drawing("ADA VANITY ELEVATION", 0), _drawing("SECTION", 400)]
    assert splash_views.companion_views(_work(views), unknown, facts).views == ()
    # Two item names both fit the item's words: which is its own is not known.
    vanity = _splash(item="Vanity: back splash height")
    views = [_drawing("ADA VANITY SECTION", 0), _drawing("PRIMARY VANITY SECTION", 400)]
    assert splash_views.companion_views(_work(views), vanity, facts).views == ()


def test_the_paired_sheets_copy_in_another_architect_file_is_never_used() -> None:
    from types import SimpleNamespace

    index = {
        f"{FILE}#2": {"item": "SAMPLE VANITY", "sheet": "A-501"},
        "older arch.pdf#2": {"item": "SAMPLE VANITY", "sheet": "A-501"},
    }
    facts = splash_views.Facts(index=index)
    older = SimpleNamespace(
        id="b1", file="older arch.pdf", page=2, project=None, reference=False, key_plan=False,
        views=[_drawing("SAMPLE VANITY SECTION", 0, "older arch.pdf")], drawings=[],
    )  # fmt: skip
    work = _work([_drawing("SAMPLE VANITY PLAN", 0)], [older])
    found = splash_views.companion_views(
        work, _splash(item="Sample vanity: back splash height"), facts
    )
    assert all(v.file == FILE for v in found.views)


def test_a_bare_thin_size_beside_the_splash_is_not_read_as_its_height() -> None:
    bare = {(FILE, 2): [_phrase('3/4" BACKSPLASH', 100, 100)]}
    found = splash_views.settle(_splash(), Search((SECTION,)), bare).finding
    assert proof.proof_group(found) == "not_compared"
    # A bare 4" or 6" beside the splash is its height, as drawings print it.
    same = {(FILE, 2): [_phrase('4" BACKSPLASH', 100, 100)]}
    assert splash_views.settle(_splash(), Search((SECTION,)), same).finding["status"] == "same"
    six = {(FILE, 2): [_phrase('6" BACKSPLASH', 100, 100)]}
    found = splash_views.settle(_splash(), Search((SECTION,)), six).finding
    assert proof.proof_group(found) == "likely_problem"
