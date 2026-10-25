"""The reference's pasted views (`pasted_views`) and what they settle (`pasted_compare`), on
made-up sheets: no client values."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import escalation, pasted_compare, pasted_views, proof, tag_reader
from app.review.assistant.chat_contract import ChatFindingDimOut
from app.review.assistant.pasted_views import Line, Shape, View

VENDOR = "VENDOR.pdf"
REF = "REF.pdf"

_PAGE_MAP = {
    "pages": [
        {
            "file": VENDOR,
            "page": 1,
            "role": "vendor",
            "halves": [{"role": "vendor", "box": [0, 0, 800, 600]}],
        },
        {
            "file": REF,
            "page": 2,
            "role": "architect",
            "halves": [{"role": "architect", "box": [0, 0, 800, 600]}],
        },
    ]
}


# ---- agreement ------------------------------------------------------------------------------------


def test_two_readings_agree_spaces_and_leader_marks_aside() -> None:
    assert pasted_views.agreed("ZORBAZ-5150", "ZORBA Z-5150") == "ZORBA Z-5150"
    assert pasted_views.agreed("2'-3\"COUNTERTOP", "2'-3\" COUNTERTOP.") == "2'-3\" COUNTERTOP"
    assert pasted_views.agreed("FACEOFWALL", "FACE OF WALL-") == "FACE OF WALL"


def test_readings_that_differ_never_agree() -> None:
    assert pasted_views.agreed('1-10%2" COUNTERTOP', '110%" COUNTERTOP') is None
    assert pasted_views.agreed("ZX-5150", "ZX-5160") is None
    assert pasted_views.agreed("", "") is None
    assert pasted_views.agreed("--", "--") is None


def test_the_tag_reader_agrees_when_one_engine_ran_the_words_together() -> None:
    assert tag_reader._agreed_keys(["ZORBA ZX-5150", "ZORBAZX-5150"]) == ("ZX-5150",)
    assert tag_reader._agreed_keys(["AB C-12", "ABC-12"]) == ()  # two different tags
    assert tag_reader._agreed_keys(["MAKER Q-5150", "MAKERQ-5150"]) == ("Q-5150",)
    assert tag_reader._agreed_keys(["MAKER Q-5150", "MAKER Q-5160"]) == ()
    assert tag_reader._agreed_keys(["Q-5150", "Q-5150"]) == ("Q-5150",)


# ---- the drawn splash -------------------------------------------------------------------------------


def _stipple(
    box: tuple[float, float, float, float], step: float = 0.5
) -> list[tuple[float, float]]:
    out = []
    y = box[1] + step / 2
    while y < box[3]:
        x = box[0] + step / 2
        while x < box[2]:
            out.append((x, y))
            x += step
        y += step
    return out


_TOP = (100.0, 140.0, 120.0, 142.5)
_SPLASH = (100.0, 127.5, 102.6, 140.0)


def test_a_splash_standing_on_the_top_at_its_end_is_found() -> None:
    dots = _stipple(_TOP) + _stipple(_SPLASH)
    found = pasted_views.drawn_splashes([Shape(_TOP), Shape(_SPLASH)], dots)
    assert found == [_SPLASH]


@pytest.mark.parametrize(
    ("splash", "fill_splash", "fill_top"),
    [
        ((108.0, 127.5, 110.6, 140.0), True, True),  # not at the top's end
        ((100.0, 120.0, 102.6, 132.0), True, True),  # not standing on the top
        (_SPLASH, False, True),  # not filled
        (_SPLASH, True, False),  # no filled top
        ((100.0, 136.0, 102.6, 140.0), True, True),  # too short: a lip, not a splash
    ],
)
def test_nothing_else_is_a_splash(
    splash: tuple[float, float, float, float], fill_splash: bool, fill_top: bool
) -> None:
    dots = (_stipple(_TOP) if fill_top else []) + (_stipple(splash) if fill_splash else [])
    assert pasted_views.drawn_splashes([Shape(_TOP), Shape(splash)], dots) == []


def test_another_fill_is_not_the_tops_splash() -> None:
    dots = _stipple(_TOP) + _stipple(_SPLASH, step=0.25)
    assert pasted_views.drawn_splashes([Shape(_TOP), Shape(_SPLASH)], dots) == []


# ---- which stamps are the architect's ------------------------------------------------------------


def test_only_stamps_on_the_architects_side_are_read() -> None:
    page_map = {
        "pages": [
            {
                "file": "C.pdf",
                "page": 1,
                "halves": [
                    {"role": "vendor", "box": [0, 0, 400, 600]},
                    {"role": "architect", "box": [400, 0, 800, 600]},
                ],
            }
        ]
    }
    marks = [
        {"file": "C.pdf", "page": 1, "kind": "Stamp", "box": [10, 10, 300, 300]},
        {"file": "C.pdf", "page": 1, "kind": "Stamp", "box": [450, 10, 700, 300]},
        {"file": "C.pdf", "page": 1, "kind": "FreeText", "box": [450, 10, 700, 300]},
        {"file": "C.pdf", "page": 2, "kind": "Stamp", "box": [450, 10, 700, 300]},
    ]
    assert pasted_views.architect_stamps(marks, page_map) == {
        ("C.pdf", 1): [(450.0, 10.0, 700.0, 300.0)]
    }


# ---- reading one view (stub engines) -------------------------------------------------------------


class _Engines:
    """Stub OCR: the first engine's lines (pixel boxes) and what the second reads of each."""

    def __init__(self, lines: list[tuple[tuple[float, float, float, float], str, str]]) -> None:
        self._lines = lines
        self._next = 0

    def lines(self, picture: Any) -> list[tuple[tuple[float, float, float, float], str]]:
        self._next = 0
        return [(box, first) for box, first, _second in self._lines]

    def line(self, picture: Any) -> str:
        second = self._lines[self._next][2]
        self._next += 1
        return second


def _picture() -> Any:
    np = pytest.importorskip("numpy")
    return np.full((600, 600, 3), 255, dtype=np.uint8)


def test_a_view_keeps_only_agreed_lines_and_reads_its_title() -> None:
    engines = _Engines(
        [
            ((60, 520, 90, 560), "B3", "B3"),
            ((100, 520, 500, 560), "VANITYSECTION", "VANITY SECTION"),
            ((100, 100, 200, 110), "ZORBAZ-5150", "ZORBA Z-5150"),
            ((100, 112, 260, 122), "UNDER-COUNTERSINK", "UNDER-COUNTER SINK"),
            ((100, 300, 200, 310), "1'-9%2\"COUNTERTOP", '19%" COUNTERTOP'),
            ((100, 330, 200, 340), "2'-3\"COUNTERTOP", "2'-3\" COUNTERTOP"),
        ]
    )
    view = pasted_views.read_view(
        REF, 2, (400.0, 50.0, 500.0, 150.0), _picture(), 6.0, engines, ([], [])
    )
    assert (view.title, view.kind, view.number) == ("VANITY SECTION", "section", "B3")
    assert view.words == "the section B3 'VANITY SECTION' (page 2)"
    assert [line.text for line in view.lines] == [
        "B3",
        "VANITY SECTION",
        "ZORBA Z-5150",
        "UNDER-COUNTER SINK",
        "2'-3\" COUNTERTOP",
    ]
    assert len(view.unagreed) == 1
    assert view.lines[2].box == pytest.approx(
        (400 + 100 / 6, 50 + 100 / 6, 400 + 200 / 6, 50 + 110 / 6)
    )
    assert pasted_views.model_tags(view, pasted_compare._SINK_WORD) == [("Z-5150", view.lines[2])]
    depths, garbled = pasted_views.countertop_depths(view)
    assert [(str(v), line.text) for v, line in depths] == [("27", "2'-3\" COUNTERTOP")]
    assert len(garbled) == 1


def test_a_model_far_from_any_sink_word_is_not_the_sinks() -> None:
    view = View(
        REF,
        2,
        (0, 0, 100, 100),
        lines=(
            Line("ZORBA Z-5150", (10, 10, 30, 12)),
            Line("UNDER-COUNTER SINK", (10, 60, 30, 62)),
        ),
    )
    assert pasted_views.model_tags(view, pasted_compare._SINK_WORD) == []


def test_a_line_saying_there_is_no_splash_names_none() -> None:
    view = View(
        REF,
        2,
        (0, 0, 100, 100),
        lines=(Line("NO BACKSPLASH", (1, 1, 9, 3)), Line("SOLID SURFACE SPLASH", (1, 5, 9, 7))),
    )
    assert [line.text for line in pasted_views.splash_words(view)] == ["SOLID SURFACE SPLASH"]


# ---- what the views settle, through the escalation -----------------------------------------------


def _section(**more: Any) -> View:
    base: dict[str, Any] = {
        "file": REF,
        "page": 2,
        "box": (400.0, 50.0, 600.0, 300.0),
        "title": "VANITY SECTION",
        "kind": "section",
        "number": "B3",
        "lines": (
            Line("ZORBA Z-5150", (420.0, 100.0, 460.0, 104.0)),
            Line("UNDER-COUNTER SINK", (418.0, 105.0, 462.0, 109.0)),
        ),
    }
    base.update(more)
    return View(**base)


def _views(*views: View) -> Any:
    def of(file: str, page: int) -> tuple[View, ...]:
        return tuple(v for v in views if (v.file, v.page) == (file, page))

    return of


def _vendor_phrases(*tags: str) -> dict[tuple[str, int], list[dict[str, Any]]]:
    return {
        (VENDOR, 1): [
            {
                "text": f"MAKER {tag}",
                "box": [100, 100 + 20 * n, 160, 108 + 20 * n],
                "side": "vendor",
            }
            for n, tag in enumerate(tags)
        ]
    }


def _sink(**more: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "F1",
        "page": 1,
        "vs_page": 2,
        "item": "Vanity: sink model",
        "vendor": "MAKER Z-5150",
        "architect": "sink label in the section, not read",
        "status": "check",
        "source": "architect",
        "category": "countertop",
        "created_by": "helper:1",
        "vendor_box": {"page": 1, "x0": 100, "y0": 100, "x1": 160, "y1": 108, "file": VENDOR},
        "arch_box": {"page": 2, "x0": 0, "y0": 0, "x1": 800, "y1": 600, "file": REF},
    }
    base.update(more)
    return base


def _ctx(views: Any, phrases: Any = None) -> escalation.EscalationContext:
    return escalation.EscalationContext(
        main_file=VENDOR, page_map=_PAGE_MAP, phrases=phrases or {}, pasted=views
    )


def _run(findings: list[dict[str, Any]], ctx: escalation.EscalationContext) -> Any:
    result = escalation.escalate(findings, ctx)
    return result, {str(f["id"]): f for f in result.changed}


def test_the_same_sink_model_on_both_is_right() -> None:
    ctx = _ctx(_views(_section()), _vendor_phrases("Z-5150", "Z-5150"))
    result, changed = _run([_sink()], ctx)
    found = changed["F1"]
    assert proof.proof_group(found) == "right"
    assert "same sink model" in found["pasted_views"]["why"]
    assert found["arch_box"]["page"] == 2 and found["arch_box"]["x0"] == 420.0
    assert result.settled.get("pasted_views") == 1


def test_another_sink_model_is_a_likely_problem_never_red() -> None:
    ctx = _ctx(_views(_section()), _vendor_phrases("Z-5160", "Z-5160"))
    _result, changed = _run([_sink(vendor="MAKER Z-5160", status="different")], ctx)
    found = changed["F1"]
    assert proof.proof_group(found) == "likely_problem"
    assert not proof.code_proved(found)
    assert proof.classified(found).reason == "tags_differ"


def test_a_copy_of_the_same_sink_difference_folds_into_the_looks_item() -> None:
    look = _sink(
        id="F9",
        created_by="whole_look",
        vendor="MAKER Z-5160",
        whole_look={"by": "whole_drawing_look", "kind": "size", "proof": "likely_only"},
    )
    helper = _sink(vendor="MAKER Z-5160")
    ctx = _ctx(_views(_section()), _vendor_phrases("Z-5160"))
    result, changed = _run([helper, look], ctx)
    assert "F1" in result.absorbed and "F9" in result.absorbed["F1"]
    assert proof.proof_group(changed["F9"]) == "likely_problem"


@pytest.mark.parametrize(
    ("views", "vendor_tags"),
    [
        (  # two models on the reference sheet: which is this sink's is not known
            (
                _section(),
                _section(
                    box=(100.0, 50.0, 300.0, 300.0),
                    lines=(
                        Line("ZORBA Z-5160", (120.0, 100.0, 160.0, 104.0)),
                        Line("SINK", (120.0, 105.0, 160.0, 109.0)),
                    ),
                ),
            ),
            ("Z-5150",),
        ),
        ((_section(),), ("Z-5150", "Z-5160")),  # the vendor's page prints two models
        ((_section(),), ()),  # the vendor's page prints none in its text
    ],
)
def test_a_model_code_cannot_pin_down_changes_nothing(
    views: tuple[View, ...], vendor_tags: tuple[str, ...]
) -> None:
    ctx = _ctx(_views(*views), _vendor_phrases(*vendor_tags))
    result, changed = _run([_sink()], ctx)
    assert "pasted_views" not in changed.get("F1", {}) and not result.absorbed


def test_nothing_is_read_off_a_page_that_is_not_the_architects() -> None:
    page_map = {"pages": [{**_PAGE_MAP["pages"][1], "role": "brand_prototype"}]}
    ctx = escalation.EscalationContext(
        main_file=VENDOR,
        page_map=page_map,
        phrases=_vendor_phrases("Z-5150"),
        pasted=_views(_section()),
    )
    _result, changed = _run([_sink()], ctx)
    assert "pasted_views" not in changed.get("F1", {})


def _look_splash() -> dict[str, Any]:
    return _sink(
        id="F5",
        item='this piece: Back splash: the vendor adds a 4" back splash the architect does not draw',
        vendor='4" back splash, 40" long',
        architect="not confirmed by code",
        created_by="whole_look",
        splash_views={"absence_not_confirmed": True, "why": "not confirmed", "outcome": "x"},
    )


def test_the_looks_no_splash_claim_is_withdrawn_when_the_section_draws_one() -> None:
    ctx = _ctx(_views(_section(splashes=((410.0, 120.0, 412.0, 130.0),))))
    result, _changed = _run([_look_splash()], ctx)
    assert "withdrawn" in result.absorbed["F5"]
    assert "section B3 'VANITY SECTION' (page 2) draws a splash" in result.absorbed["F5"]


def test_a_splash_named_in_the_sections_words_counts_too() -> None:
    named = _section(lines=(Line("SOLID SURFACE SPLASH", (410.0, 120.0, 450.0, 124.0)),))
    result, _changed = _run([_look_splash()], _ctx(_views(named)))
    assert "names a splash (SOLID SURFACE SPLASH)" in result.absorbed["F5"]


def test_the_claim_stays_when_no_view_shows_a_splash() -> None:
    result, changed = _run([_look_splash()], _ctx(_views(_section())))
    assert "F5" not in result.absorbed and "pasted_views" not in changed.get("F5", {})


def test_a_readings_no_splash_says_the_section_draws_it_and_compares_no_size() -> None:
    reading = _sink(
        id="F6",
        item="Vanity: side and back splashes",
        vendor="4in back splash and two 4in side splashes drawn",
        architect="no splash drawn on plan",
    )
    ctx = _ctx(_views(_section(splashes=((410.0, 120.0, 412.0, 130.0),))))
    result, changed = _run([reading], ctx)
    found = changed["F6"]
    assert "F6" not in result.absorbed
    assert proof.proof_group(found) == "not_compared"
    why = proof.classified(found).why or ""
    assert "draws a splash on the top, against the wall" in why
    assert "prints no size for it, so no size is compared" in why
    assert "the vendor's side splashes are not checked there" in why
    assert "no splash" not in why
    assert "code read" not in why  # plain words for the reviewer


def test_a_back_splash_reading_says_nothing_about_side_splashes() -> None:
    reading = _sink(
        id="F6",
        item="Vanity: back splash",
        vendor="4in back splash drawn",
        architect="no splash drawn on plan",
    )
    ctx = _ctx(_views(_section(splashes=((410.0, 120.0, 412.0, 130.0),))))
    _result, changed = _run([reading], ctx)
    why = proof.classified(changed["F6"]).why or ""
    assert "prints no size for it" in why and "side splash" not in why


def _depth(**more: Any) -> dict[str, Any]:
    base = _sink(
        id="F7",
        item="Vanity: top depth",
        vendor='24"',
        architect="533 [21]",
        status="different",
        inches={"vendor": "24", "architect": "21"},
        checks={"printed_vendor": "match", "printed_architect": "match"},
    )
    base.update(more)
    return base


def test_the_sections_countertop_depth_is_compared_with_the_tops_depth() -> None:
    depth = _section(lines=(Line("2'-0\" COUNTERTOP", (420.0, 60.0, 470.0, 64.0)),))
    _result, changed = _run([_depth()], _ctx(_views(depth)))
    found = changed["F7"]
    assert proof.proof_group(found) == "right"
    assert found["architect"] == "2'-0\" COUNTERTOP"
    # The section's label reads as a chat finding (a real run's answer failed to open on "ocr").
    label = ChatFindingDimOut.model_validate(found["dims"]["architect"])
    assert label.route == "drawn"
    assert (
        ChatFindingDimOut.model_validate({**found["dims"]["architect"], "route": "ocr"}).route
        == "drawn"
    )
    other = _section(lines=(Line("2'-3\" COUNTERTOP", (420.0, 60.0, 470.0, 64.0)),))
    _result, changed = _run([_depth(id="F8")], _ctx(_views(other)))
    assert proof.proof_group(changed["F8"]) == "likely_problem"
    assert not proof.code_proved(changed["F8"])


def test_a_depth_the_engines_read_differently_changes_no_outcome() -> None:
    garbled = _section(lines=(), unagreed=(('1-9%2" COUNTERTOP', '19%" COUNTERTOP', (0, 0, 1, 1)),))
    finding = _depth()
    before = proof.proof_group(finding)
    _result, changed = _run([finding], _ctx(_views(garbled)))
    found = changed["F7"]
    assert proof.proof_group(found) == before
    assert "read it differently" in found["pasted_views"]["why"]


# ---- a made-up PDF with a pasted section (stub OCR) ---------------------------------------------


def _pdf_with_stamp(path: Path) -> None:
    pikepdf = pytest.importorskip("pikepdf")
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(800, 600))
    # The snapshot, in PDF space (origin bottom left): a stippled top 20 x 2.5 with a stippled
    # splash 2.6 x 12.5 standing on its left end; both outlined.
    top = (100.0, 600 - 142.5, 20.0, 2.5)
    splash = (100.0, 600 - 140.0, 2.6, 12.5)
    ops = [b"0 0 0 RG 0.1 w"]
    for x, y, w, h in (top, splash):
        ops.append(f"{x} {y} {w} {h} re S".encode())
        yy = y + 0.25
        while yy < y + h:
            xx = x + 0.25
            while xx < x + w:
                ops.append(f"{xx:.2f} {yy:.2f} m {xx:.2f} {yy + 0.04:.2f} l S".encode())
                xx += 0.5
            yy += 0.5
    stream = pdf.make_stream(b"\n".join(ops))
    stream["/Type"] = pikepdf.Name.XObject
    stream["/Subtype"] = pikepdf.Name.Form
    stream["/BBox"] = [90, 440, 140, 490]  # its Rect: drawn where it is pasted, not scaled
    stamp = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name.Stamp,
            Rect=[90, 600 - 160, 140, 600 - 110],
            AP=pikepdf.Dictionary(N=stream),
        )
    )
    pdf.pages[0].obj["/Annots"] = pdf.make_indirect(pikepdf.Array([stamp]))
    pdf.save(path)


def test_the_reader_finds_a_splash_drawn_in_a_pasted_section(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    pytest.importorskip("pypdfium2")
    path = tmp_path / REF
    _pdf_with_stamp(path)
    engines = _Engines([((5, 5, 200, 20), "VANITYSECTION", "VANITY SECTION")])
    reader = pasted_views.PastedReader(
        lambda file: path if file == REF else None,
        {(REF, 1): [(90.0, 110.0, 140.0, 160.0)]},
        engines,
    )
    views = reader(REF, 1)
    assert len(views) == 1
    view = views[0]
    assert view.kind == "section" and view.title == "VANITY SECTION"
    assert len(view.splashes) == 1
    assert view.splashes[0] == pytest.approx((100.0, 127.5, 102.6, 140.0), abs=0.2)
    assert reader(REF, 1) is views  # read once
    assert reader(REF, 3) == ()  # no pasted drawing there


def test_a_looks_claim_that_the_vendor_leaves_the_splash_out_is_never_withdrawn() -> None:
    missing = _look_splash()
    missing["splash_views"] = {**missing["splash_views"], "look_kind": "missing_piece"}
    absent = _look_splash()
    absent["vendor"] = "not shown"
    for finding in (missing, absent):
        ctx = _ctx(_views(_section(splashes=((410.0, 120.0, 412.0, 130.0),))))
        result, changed = _run([finding], ctx)
        assert "F5" not in result.absorbed
        assert "pasted_views" not in changed.get("F5", {})


def test_an_item_code_proved_wrong_is_never_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proof, "code_proved", lambda finding: finding.get("id") == "F7")
    depth = _section(lines=(Line("2'-0\" COUNTERTOP", (420.0, 60.0, 470.0, 64.0)),))
    assert pasted_compare.changes([_depth()], _views(depth), {}, _PAGE_MAP, VENDOR) == []
    assert len(pasted_compare.changes([_depth(id="F8")], _views(depth), {}, _PAGE_MAP, VENDOR)) == 1


def test_a_faucets_tag_beside_the_sink_is_not_the_sinks_model() -> None:
    lines = (
        Line("FAUCET Z-5160", (420.0, 94.0, 460.0, 98.0)),
        Line("ZORBA Z-5150", (420.0, 100.0, 460.0, 104.0)),
        Line("UNDER-COUNTER SINK", (418.0, 105.0, 462.0, 109.0)),
    )
    ctx = _ctx(_views(_section(lines=lines)), _vendor_phrases("Z-5150"))
    _result, changed = _run([_sink()], ctx)
    assert proof.proof_group(changed["F1"]) == "right"
