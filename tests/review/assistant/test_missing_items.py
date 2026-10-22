"""A missing millwork item is red (#1847): code proves the vendor's drawings leave it out, $0.

All synthetic: made-up labels, words and values, no PDF, no model call. The architect's drawing
of a made-up coffee bar (page 4, top half) dimensions an outlet inside the base cabinet: 1'-4"
from the left end and 1'-3" above the floor. The vendor's round 1 (page 4, bottom half) and
round 2 (page 5) draw the cabinet (35" tall) without it.
"""

from __future__ import annotations

from typing import Any

from app.review.assistant import chat_trust, proof
from app.review.assistant.escalation import EscalationContext, escalate
from app.review.assistant.missing_items import kind_of

FILE = "set.pdf"
ARCHITECT_HALF = [0.0, 0.0, 600.0, 300.0]
VENDOR_HALF = [0.0, 320.0, 600.0, 780.0]
ROUND_2 = [0.0, 0.0, 600.0, 780.0]
#: The vendor drawings stand on the floor at these page heights (the lower end of their 35").
FLOOR_R1, FLOOR_R2 = 760.0, 700.0


def _label(
    id_: str,
    side: str,
    inches: str,
    at: tuple[float, float],
    way: str = "v",
    *,
    line: tuple[float, float] | None = None,
    **more: Any,
) -> dict[str, Any]:
    """A label at `at`; `line`: its dimension line's ends along its axis (a height: top and
    bottom page y)."""
    x, y = at
    found: dict[str, Any] = {
        "id": id_,
        "text": more.pop("text", f'{inches}"'),
        "inches": inches,
        "box": [x - 6, y - 10, x + 6, y + 10],
        "side": side,
        "orientation": way,
        "route": more.pop("route", "text"),
        **more,
    }
    if line is not None:
        a, b = line
        found["dim_line"] = [[x + 8, a], [x + 8, b]] if way == "v" else [[a, y + 8], [b, y + 8]]
    return found


def _words(texts: list[tuple[str, tuple[float, float]]], side: str) -> list[dict[str, Any]]:
    return [
        {"text": text, "box": [x - 20, y - 5, x + 20, y + 5], "side": side}
        for text, (x, y) in texts
    ]


#: The vendor's own words on each drawing (a text layer code read).
VENDOR_WORDS_R1 = [("WD-01", (300, 400)), ("STONE TOP", (300, 450)), ("FRONT VIEW", (300, 740))]
VENDOR_WORDS_R2 = [("WD-01", (300, 100)), ("STONE TOP", (300, 150)), ("FRONT VIEW", (300, 680))]


def _architect_labels() -> list[dict[str, Any]]:
    return [
        _label("D2", "architect", "16", (120, 250), "h", text="1' - 4\"", line=(100, 140)),
        _label("D3", "architect", "15", (80, 200), "v", text="1' - 3\"", line=(170, 230)),
        _label("D4", "architect", "30", (60, 150), "v", text="2' - 6\"", line=(80, 230)),
    ]


def _vendor_r1(*extra: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _label("D11", "vendor", "35", (40, 600), line=(500, FLOOR_R1)),
        _label("D12", "vendor", "4", (60, 750), line=(740, FLOOR_R1)),
        _label("D13", "vendor", "52", (300, 340), "h", line=(50, 550)),
        _label("D14", "vendor", "24", (500, 600), line=(560, 640)),
        *extra,
    ]


def _vendor_r2(*extra: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _label("D1", "vendor", "35", (40, 500), line=(400, FLOOR_R2)),
        _label("D2", "vendor", "4", (60, 690), line=(680, FLOOR_R2)),
        _label("D3", "vendor", "52", (300, 40), "h", line=(50, 550)),
        *extra,
    ]


def _page_map() -> dict[str, Any]:
    return {
        "pages": [
            {
                "file": FILE,
                "page": 4,
                "area": "Coffee bar",
                "halves": [
                    {"role": "architect", "box": ARCHITECT_HALF, "round": None},
                    {"role": "vendor", "box": VENDOR_HALF, "round": 1},
                ],
            },
            {
                "file": FILE,
                "page": 5,
                "area": "Coffee bar",
                "halves": [{"role": "vendor", "box": ROUND_2, "round": 2}],
            },
        ]
    }


def _ctx(
    *,
    r1: list[dict[str, Any]] | None = None,
    r2: list[dict[str, Any]] | None = None,
    words_r1: list[tuple[str, tuple[float, float]]] | None = None,
    words_r2: list[tuple[str, tuple[float, float]]] | None = None,
    architect_words: list[tuple[str, tuple[float, float]]] | None = None,
    pictures: dict[int, float] | None = None,
    unopened: frozenset[int] | None = frozenset(),
    coloured: bool | None = False,
    notes: list[dict[str, Any]] | None = None,
    architect: list[dict[str, Any]] | None = None,
) -> EscalationContext:
    def under(file: str, page: int, box: tuple[float, float, float, float]) -> list[dict[str, Any]]:
        if coloured is None:
            return []
        return [{"text": "1' - 4\"", "box": list(box), "coloured": coloured}]

    shares = {4: 0.0, 5: 0.0, **(pictures or {})}
    return EscalationContext(
        main_file=FILE,
        labels={
            (FILE, 4): [*(architect or _architect_labels()), *(r1 or _vendor_r1())],
            (FILE, 5): r2 if r2 is not None else _vendor_r2(),
        },
        page_map=_page_map(),
        phrases={
            (FILE, 4): _words(architect_words or [("COFFEE BAR", (300, 280))], "architect")
            + _words(VENDOR_WORDS_R1 if words_r1 is None else words_r1, "vendor"),
            (FILE, 5): _words(VENDOR_WORDS_R2 if words_r2 is None else words_r2, "vendor"),
        },
        page_texts={(FILE, 5): "WD-01 STONE TOP FRONT VIEW"},
        pictures={(FILE, page): share for page, share in shares.items()},
        annotations=notes or [],
        unopened=unopened,
        under=under,
    )


def _missing(**changes: Any) -> dict[str, Any]:
    """An AI's "the vendor does not show it": the outlet's place from the left end, with its
    height from the floor in the architect's words."""
    finding: dict[str, Any] = {
        "id": "F7",
        "page": 4,
        "item": "Coffee bar: outlet in the base cabinet",
        "category": "cabinet",
        "architect": "1' - 4\" (CL from left end); 1' - 3\" (AFF)",
        "vendor": "not shown on the vendor",
        "status": "check",
        "keep_reason": "other",
        "source": "architect",
        "created_by": "helper:3",
        "trust_level": "to_check",
        "witnesses": [],
        "checks": {"printed_architect": "match", "printed_vendor": "not_a_number"},
        "inches": {"architect": None, "vendor": None},
        "arch_box": {"page": 4, "x0": 114, "y0": 240, "x1": 126, "y1": 260, "file": FILE},
        "vendor_box": None,
        "dims": {
            "architect": {
                "id": "D2",
                "page": 4,
                "file": FILE,
                "text": "1' - 4\"",
                "inches": "16",
                "side": "architect",
                "route": "text",
                "box": [114, 240, 126, 260],
            }
        },
    }
    finding.update(changes)
    return finding


def _run(*findings: dict[str, Any], ctx: EscalationContext | None = None) -> dict[str, Any]:
    result = escalate(list(findings), ctx or _ctx())
    out = {str(f["id"]): f for f in result.changed}
    out["absorbed"] = result.absorbed
    return out


def _not_red(finding: dict[str, Any], words: str) -> None:
    assert proof.proof_group(finding) == "not_compared", finding.get("why")
    assert finding["vendor_search"]["found"] == "none"
    assert words in str(finding["vendor_search"].get("not_red")), finding["vendor_search"]
    reason = proof.proof_of(finding)["reason"]
    assert reason is not None and "not a red: " in reason


# ---- the red -------------------------------------------------------------------------------------


def test_an_outlet_on_the_millwork_the_vendor_prints_nowhere_is_red() -> None:
    finding = _run(_missing())["F7"]
    assert proof.proof_group(finding) == "clouded" and finding["status"] == "different"
    assert finding["why"] == (
        "Missing on the vendor's drawing: architect shows 1' - 4\" and 1' - 3\" "
        "(outlet, architect page 4)."
    )
    assert finding["vendor"] == "not shown on the vendor's drawings"
    found = proof.proof_of(finding)
    assert found["proof"] == (
        "Code searched 7 vendor labels on pages 4 and 5 (every vendor drawing and round of the "
        "area): none prints 1' - 4\" or 1' - 3\""
    )
    assert found["should_be"].startswith('16"')  # the architect's own value, nothing more
    assert finding["witnesses"] == ["text"] and finding["trust_level"] == "likely"
    assert finding["escalation"]["outcome"] == "wrong"
    # The trust rule keeps the witness when the checks are worked out again.
    assert chat_trust._text_made(finding) == "agrees"
    assert chat_trust._text_made({**finding, "vendor": '24"'}) is None


def test_blocking_needs_no_height_and_a_copy_of_the_same_label_folds_into_the_red() -> None:
    blocking = _missing(
        id="F8",
        item="Coffee bar: blocking under the top",
        architect="2' - 6\"",
        dims={
            "architect": {
                **_missing()["dims"]["architect"],
                "id": "D4",
                "text": "2' - 6\"",
                "inches": "30",
            }
        },
    )
    copy = _missing(id="F9", item="Coffee bar: blocking", architect="2' - 6\"")
    copy["dims"] = blocking["dims"]
    out = _run(blocking, copy)
    assert proof.proof_group(out["F8"]) == "clouded"
    assert "architect shows 2' - 6\" (blocking, architect page 4)" in out["F8"]["why"]
    assert out["absorbed"] == {"F9": "Same item as F8 (page 4), which carries it: a proven Wrong."}
    assert [entry["finding_id"] for entry in out["F8"]["also_on"]] == ["F9"]


# ---- millwork or a room feature ------------------------------------------------------------------


def test_the_word_lists_decide_the_kind() -> None:
    assert kind_of(["Bar: duplex outlet"]) == ("outlet", None)
    assert kind_of(["Bar: GFCI"]) == ("outlet", None)
    assert kind_of(["Bar: sink cut-out"]) == ("cut-out", None)
    assert kind_of(["Bar: floor to ceiling"]) == (None, "ceiling")
    assert kind_of(["Bar: TV outlet"]) == ("outlet", "tv")
    assert kind_of(["Bar: overall width"]) == (None, None)


def test_room_features_and_items_with_no_millwork_word_are_never_red() -> None:
    for item, words in (
        ("Coffee bar: TV outlet", "a room feature (tv)"),
        ("Coffee bar: outlet beside the mirror", "a room feature (mirror)"),
        ("Coffee bar: floor to light switch", "a room feature (light)"),
        ("Coffee bar: floor to artwork centre", "a room feature (artwork)"),
        ("Coffee bar: shower valve", "a room feature (shower)"),
        ("Coffee bar: overall width", "code finds no millwork word"),
    ):
        _not_red(_run(_missing(item=item))["F7"], words)
    # The architect's own words beside the dimension count too.
    ctx = _ctx(architect_words=[("CEILING LINE", (130, 270))])
    _not_red(_run(_missing(), ctx=ctx)["F7"], "a room feature (ceiling)")


def test_an_outlet_above_the_millwork_or_with_no_height_is_not_red() -> None:
    # The architect's words give no height from the floor: code cannot tell where it sits.
    _not_red(
        _run(_missing(architect="1' - 4\" (CL from left end)"))["F7"],
        "cannot tell whether it sits on the millwork",
    )
    # 5'-0" above the floor, over a 35" cabinet: a wall outlet.
    high = [*_architect_labels()[:2], _label("D5", "architect", "60", (80, 100), text="5' - 0\"")]
    ctx = _ctx(architect=high)
    _not_red(
        _run(_missing(architect="1' - 4\" (CL); 5' - 0\" (AFF)"), ctx=ctx)["F7"],
        'it sits 60" above the floor, above the vendor\'s millwork (its tallest height 35")',
    )


# ---- every vendor drawing read -------------------------------------------------------------------


def test_an_unread_vendor_drawing_blocks_the_red() -> None:
    # Round 2 has two labels only: no label layer to prove an absence with.
    few = [_label("D1", "vendor", "35", (40, 500)), _label("D2", "vendor", "4", (60, 690))]
    _not_red(_run(_missing(), ctx=_ctx(r2=few))["F7"], "page 5 round 2 has no label and text layer")
    # Round 2 has labels but no words: pen strokes, or a scan.
    _not_red(_run(_missing(), ctx=_ctx(words_r2=[]))["F7"], "page 5 round 2 has no label and text")
    _not_red(
        _run(_missing(), ctx=_ctx(pictures={5: 0.4}))["F7"],
        "page 5 round 2 holds a pasted picture",
    )
    _not_red(
        _run(_missing(), ctx=_ctx(unopened=frozenset({5})))["F7"],
        "page 5 round 2 was not opened in this check",
    )


# ---- the vendor shows it another way -------------------------------------------------------------


def test_an_item_the_vendor_dimensions_differently_is_not_missing() -> None:
    # A height from the floor 1 3/4" off the architect's, in round 2: the same outlet, maybe.
    near = _label("D7", "vendor", "16.75", (500, 650), text='16 3/4"', line=(640, FLOOR_R2))
    _not_red(
        _run(_missing(), ctx=_ctx(r2=_vendor_r2(near)))["F7"],
        'the vendor prints 16 3/4" (p5.D7) the same way round, within 3" of 1\' - 3"',
    )
    # Another finding compares the same architect label with a vendor value.
    pair = {
        "id": "F3",
        "page": 5,
        "item": "Coffee bar: outlet from the left end",
        "architect": "1' - 4\"",
        "vendor": '17"',
        "status": "different",
        "dims": {"architect": _missing()["dims"]["architect"], "vendor": {"id": "D9", "page": 5}},
    }
    _not_red(_run(_missing(), pair)["F7"], "F3 compares it with the vendor's 17\"")
    # The vendor's drawing names the item: it shows it, or says who provides it.
    named = [*VENDOR_WORDS_R2, ("Electrical outlets", (450, 300)), ("by others", (450, 310))]
    _not_red(
        _run(_missing(), ctx=_ctx(words_r2=named))["F7"],
        "the vendor's drawing names it: 'Electrical outlets by others' (page 5 round 2)",
    )
    # The vendor's side of the reading carries a number.
    _not_red(
        _run(_missing(vendor='not shown on the vendor (prints 17")'))["F7"],
        "the vendor's side names a value",
    )
    # The value is printed in a word on the vendor's drawing (not a label).
    printed = [*VENDOR_WORDS_R1, ("OUTLET AT 1'-4\" O.C.", (300, 500))]
    _not_red(_run(_missing(), ctx=_ctx(words_r1=printed))["F7"], "1' - 4\": the vendor's page 4")


def test_a_plan_depth_near_the_height_does_not_block_it() -> None:
    # A plan view's depth (its line ends far from the floor line) is not a height from the floor.
    depth = _label("D8", "vendor", "15.5", (500, 200), text='15 1/2"', line=(100, 180))
    finding = _run(_missing(), ctx=_ctx(r2=_vendor_r2(depth)))["F7"]
    assert proof.proof_group(finding) == "clouded"


# ---- the architect's value -----------------------------------------------------------------------


def test_the_architect_value_must_be_code_read_text_on_the_architect_drawing() -> None:
    drawn = [{**_architect_labels()[0], "route": "drawn"}, *_architect_labels()[1:]]
    _not_red(_run(_missing(), ctx=_ctx(architect=drawn))["F7"], "not printed text")
    note = [{"page": 4, "kind": "FreeText", "file": FILE, "box": [100, 230, 140, 270]}]
    _not_red(_run(_missing(), ctx=_ctx(notes=note))["F7"], "inside one of GV's notes")
    _not_red(_run(_missing(), ctx=_ctx(coloured=True))["F7"], "in GV's colour")
    _not_red(_run(_missing(), ctx=_ctx(coloured=None))["F7"], "could not look at the architect")
    # A label the page map puts on the vendor's drawing is never the architect's value.
    moved = [{**_architect_labels()[0], "box": [114, 400, 126, 420]}, *_architect_labels()[1:]]
    dims = {"architect": {**_missing()["dims"]["architect"], "box": [114, 400, 126, 420]}}
    _not_red(
        _run(_missing(dims=dims), ctx=_ctx(architect=moved))["F7"],
        "the value is on the vendor's drawing",
    )
