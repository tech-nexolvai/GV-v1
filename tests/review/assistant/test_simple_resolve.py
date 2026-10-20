"""The resolve loop (`simple_check/resolve.py`, #1735): each route it fills, what code checks of the
AI's evidence, the settings, the conventions library, the model call's form and cost hold, saved
rounds replayed at $0, and the loop inside a real chat engine's simple full check.

The wall (every known false red stays out of red) is `test_simple_loop_wall.py`. Made-up drawings
and values only; no PDF reading, no model call, no money."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.review.assistant.simple_check import resolve
from app.review.assistant.simple_check.run import SimpleOptions
from app.review.assistant.simple_check.verify import (
    BlindAnswer,
    Item,
    Pack,
    Region,
    Verified,
    verify_answer,
    with_blind,
)
from tests.review.assistant.test_simple_loop_wall import (
    FIXTURES,
    PACK,
    Scripted,
    _verified,
)

FILE = "set.pdf"
ARCH = [0.0, 0.0, 500.0, 400.0]
VEND = [0.0, 420.0, 500.0, 800.0]
WHOLE = [0.0, 0.0, 500.0, 800.0]
BLACK = (120, 0.0)
ON = resolve.ResolveSettings(mode="on")
WALLS = {
    "vendor_from": "wall_face (left)",
    "vendor_to": "wall_face (right)",
    "architect_from": "wall_face (left)",
    "architect_to": "wall_face (right)",
}


def _label(
    lid: str,
    text: str,
    inches: str,
    box: Sequence[float],
    side: str,
    o: str = "h",
    line: Sequence[Sequence[float]] | None = None,
    **more: Any,
) -> dict[str, Any]:
    extension = None
    if line is not None:
        (x0, y0), (x1, y1) = line
        extension = [[[x0, y0 - 40], [x0, y0]], [[x1, y1 - 40], [x1, y1]]]
    return {
        "id": lid,
        "text": text,
        "inches": inches,
        "box": list(box),
        "side": side,
        "orientation": o,
        "route": more.pop("route", "text"),
        "mm": None,
        "units": None,
        "dim_line": [list(p) for p in line] if line is not None else None,
        "extension": extension,
        "span_pt": None,
        "scale_ok": True,
        **more,
    }


def _mini() -> Pack:
    """Page 1: the architect's elevation (a 44" overall width, and a string of parts 20 + 24 = 44
    touching end to end) over the vendor's round 1 (40"); page 2: the vendor's round 2 (42")."""
    page1 = [
        _label("D1", "3' - 8\"", "44", (100, 100, 130, 110), "architect"),
        _label(
            "D2", "1' - 8\"", "20", (110, 300, 130, 310), "architect", "h", ((100, 315), (200, 315))
        ),
        _label(
            "D3", "2' - 0\"", "24", (230, 300, 260, 310), "architect", "h", ((200, 315), (320, 315))
        ),
        _label("D4", '40"', "40", (100, 500, 130, 510), "vendor", "h", ((100, 515), (300, 515))),
        _label("D5", '18"', "18", (20, 600, 30, 630), "vendor", "v"),
    ]
    page2 = [
        _label("D1", '42"', "42", (100, 100, 130, 110), "vendor"),
        _label("D2", '42"', "42", (20, 100, 30, 130), "vendor", "v"),
    ]
    words = [
        {"text": "BASE CABINET", "box": [20, 700, 90, 710], "side": "vendor"},
        {"text": "END PANEL", "box": [120, 700, 200, 710], "side": "vendor"},
        {"text": "SHELF", "box": [220, 700, 300, 710], "side": "vendor"},
    ]
    return Pack.from_documents(
        main_file=FILE,
        dims={
            "files": [FILE],
            "pages": [
                {"file": FILE, "page": 1, "labels": page1, "phrases": words},
                {"file": FILE, "page": 2, "labels": page2, "phrases": []},
            ],
        },
        page_map={
            "pages": [
                {
                    "file": FILE,
                    "page": 1,
                    "area": "1. Kitchen",
                    "area_from_page": None,
                    "halves": [
                        {"box": ARCH, "role": "architect", "round": None},
                        {"box": VEND, "role": "vendor", "round": 1},
                    ],
                },
                {
                    "file": FILE,
                    "page": 2,
                    "area": "1. Kitchen",
                    "area_from_page": 1,
                    "halves": [
                        {"box": WHOLE, "role": "vendor", "round": 2},
                    ],
                },
            ]
        },
        pairs={"pairs": []},
        page_texts={"pages": []},
    )


MINI = _mini()
ROUND_1 = Item("p1.v1", Region(FILE, 1, tuple(VEND), 1), (Region(FILE, 1, tuple(ARCH)),))  # type: ignore[arg-type]


NEWEST = Item("p2.v2", Region(FILE, 2, tuple(WHOLE), 2), (Region(FILE, 1, tuple(ARCH)),))  # type: ignore[arg-type]


def _pair(v: str, a: str | None, verdict: str = "different", **ends: str) -> dict[str, Any]:
    return {
        "vendor_label": v,
        "architect_label": a,
        "measures": "overall counter width",
        **(ends or WALLS),
        "verdict": verdict,
        "note": "",
    }


def _one(
    raw: Mapping[str, Any],
    *,
    pack: Pack = MINI,
    item: Item = ROUND_1,
    pair: bool = True,
    read: Any = None,
) -> Verified:
    result = {
        "work_item": item.id,
        "pairs": [dict(raw)] if pair else [],
        "other_items": [] if pair else [dict(raw)],
        "unaccounted_labels": [],
    }
    found = verify_answer(result, item, pack, ink=lambda _l: BLACK, read=read)
    mine = [v for v in found.items if (v.kind == "pair") == pair]
    assert len(mine) == 1
    return mine[0]


def _run(
    v: Verified,
    raw: Mapping[str, Any],
    proposals: list[resolve.Proposal],
    *,
    pack: Pack = MINI,
    item: Item = ROUND_1,
    settings: resolve.ResolveSettings = ON,
    **more: Any,
) -> resolve.Resolved:
    return resolve.resolve_one(
        v,
        raw,
        item,
        pack,
        proposer=Scripted(list(proposals)),
        settings=settings,
        ink=lambda _l: BLACK,
        **more,
    )


# ---- the routes -------------------------------------------------------------------------------


def test_an_older_round_label_is_moved_to_the_newest_rounds_same_dimension() -> None:
    raw = _pair("p1.D4", "p1.D1")
    v = _one(raw)
    assert v.outcome == "not_compared" and "round 1" in v.reason
    entry = resolve.eligible(v, raw, ROUND_1, MINI)
    assert isinstance(entry, resolve.Failure) and entry.route == "round"
    # The newest round's 42" against the architect's 44": a new pair, verified, for a blind look.
    done = _run(
        v,
        raw,
        [resolve.Proposal(kind="newer_round", vendor_label="p2.D1", verdict="different", **WALLS)],
    )
    assert done.verified.outcome == "candidate", done.verified.reason
    assert done.verified.key == v.key and done.verified.vendor is not None
    assert (
        done.verified.vendor["page"] == 2
        and done.item.vendor.page == 2
        and done.item.vendor.round == 2
    )
    assert done.verified.resolve and done.verified.resolve[0]["kind"] == "newer_round"
    # A label that runs the other way is not one of code's candidates.
    other = _run(
        v,
        raw,
        [resolve.Proposal(kind="newer_round", vendor_label="p2.D2", verdict="different", **WALLS)],
    )
    assert other.verified.outcome == "not_compared"
    assert "not one of code's candidates" in other.rounds[0].why


def test_an_equal_part_of_the_vendors_span_is_code_proved() -> None:
    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    sink = {
        "vendor_from": "wall_face (left)",
        "vendor_to": "centre_line (sink)",
        "architect_from": "wall_face (left)",
        "architect_to": "centre_line (sink)",
    }
    done = resolve.resolve_one(
        v,
        raw,
        item,
        PACK,
        proposer=Scripted(
            [resolve.Proposal(kind="eq_part", vendor_label="p1.D13", verdict="different", **sink)]
        ),
        settings=ON,
        ink=lambda _l: BLACK,
    )
    assert done.verified.outcome == "candidate", done.verified.reason
    assert done.verified.vendor is not None and done.verified.vendor["id"] == "D13"
    assert done.rounds[0].facts["eq_part"] == "p1.D13"
    # Not an equal part of that span: refused.
    other = resolve.resolve_one(
        v,
        raw,
        item,
        PACK,
        proposer=Scripted(
            [resolve.Proposal(kind="eq_part", vendor_label="p1.D7", verdict="different", **sink)]
        ),
        settings=ON,
        ink=lambda _l: BLACK,
    )
    assert other.verified.outcome == "not_compared"
    assert "not one of code's equal parts" in other.rounds[0].why


def _unlabelled(
    vendor: str, architect: str, vbox: Sequence[float], abox: Sequence[float]
) -> dict[str, Any]:
    return {
        "kind": "dimension_without_label",
        "what": "overall counter width",
        "vendor": vendor,
        "architect": architect,
        "vendor_box": list(vbox),
        "architect_box": list(abox),
        "architect_page": 1,
        "verdict": "different",
        "millwork": True,
    }


def test_a_value_without_a_label_is_attached_to_codes_label_at_its_spot() -> None:
    item = Item("p2.v2", Region(FILE, 2, tuple(WHOLE), 2), (Region(FILE, 1, tuple(ARCH)),))  # type: ignore[arg-type]
    raw = _unlabelled('42"', "3' - 8\"", (100, 100, 130, 110), (100, 100, 130, 110))
    v = _one(raw, item=item, pair=False)
    assert v.outcome == "not_compared" and v.reason_code == "unreadable"
    entry = resolve.eligible(v, raw, item, MINI)
    assert isinstance(entry, resolve.Failure) and entry.route == "read"
    good = resolve.Proposal(
        kind="attach", vendor_label="p2.D1", architect_label="p1.D1", verdict="different", **WALLS
    )
    done = _run(v, raw, [good], item=item)
    assert done.verified.outcome == "candidate" and done.verified.blind == "pair"
    # A label that does not print the value quoted at the spot quoted: the pair is fixed.
    wrong = resolve.Proposal(
        kind="attach", vendor_label="p2.D2", architect_label="p1.D1", verdict="different", **WALLS
    )
    assert _run(v, raw, [wrong], item=item).verified.outcome == "not_compared"
    elsewhere = resolve.Proposal(
        kind="attach", vendor_label="p2.D1", architect_label="p1.D2", verdict="different", **WALLS
    )
    assert _run(v, raw, [elsewhere], item=item).verified.outcome == "not_compared"


def test_an_architect_string_counts_only_as_one_proved_chain_and_the_blind_look_must_pick_it() -> (
    None
):
    raw = _unlabelled('42"', '44" (sum of the string)', (100, 100, 130, 110), (95, 295, 325, 320))
    v = _one(raw, item=NEWEST, pair=False)

    def chain_of(*labels: str) -> resolve.Proposal:
        return resolve.Proposal(
            kind="architect_chain",
            vendor_label="p2.D1",
            labels=labels,
            verdict="different",
            **WALLS,
        )

    done = _run(v, raw, [chain_of("p1.D2", "p1.D3")], item=NEWEST)
    assert done.verified.outcome == "candidate", done.verified.reason
    assert done.verified.blind == "chain"
    assert done.verified.architect is not None
    assert done.verified.architect["chain"] == ["p1.D2", "p1.D3"]
    agree = BlindAnswer("D2+D3", "same_thing", *WALLS.values(), "yes")
    assert with_blind(done.verified, agree).outcome == "red"
    part = BlindAnswer("D3", "same_thing", *WALLS.values(), "yes")
    assert with_blind(done.verified, part).outcome == "not_compared"
    # One part alone is not a chain; the architect's overall is not a part of it.
    assert _run(v, raw, [chain_of("p1.D2")], item=NEWEST).verified.outcome == "not_compared"
    done = _run(v, raw, [chain_of("p1.D1", "p1.D3")], item=NEWEST)
    assert done.verified.outcome == "not_compared"


def test_the_read_number_tool_reads_both_spots_and_its_answer_stands() -> None:
    """The loop's hook is the real tool (`read_number.NumberReader`, #1746) through
    `resolve.reader_at`: asked at each spot, by the file's own page; counted only when it says
    read and reads the values quoted (the pair is fixed); its reason said when it cannot."""
    from app.review.assistant.read_number import Reading

    item = Item("p2.v2", Region(FILE, 2, tuple(WHOLE), 2), (Region(FILE, 1, tuple(ARCH)),))  # type: ignore[arg-type]
    raw = _unlabelled('42"', "3' - 8\"", (100, 100, 130, 110), (100, 100, 130, 110))
    v = _one(raw, item=item, pair=False)
    asked: list[Any] = []

    def tool(value_of: Any) -> Any:
        def number(request: Any) -> Reading:
            asked.append(request)
            return value_of(request)

        return resolve.reader_at(number, MINI)

    def read(request: Any) -> Reading:
        value = "42" if request.page == 2 else "44"
        return Reading("read", value, f'{value}"', agreed=("ocr", "shape"), code="ocr_shape")

    done = _run(v, raw, [], item=item, read_number=tool(read))
    assert done.verified.outcome == "candidate" and done.verified.blind == "values"
    assert [(r.file, r.page, r.side) for r in asked] == [
        (FILE, 2, "vendor"),
        (FILE, 1, "architect"),
    ]
    assert done.rounds[0].number == 0

    def unread(request: Any) -> Reading:
        return Reading("unread", reason="the readers disagree", code="readers_disagree")

    done = _run(v, raw, [resolve.Proposal()], item=item, read_number=tool(unread))
    assert done.verified.outcome == "not_compared"
    assert "the readers disagree" in done.rounds[0].why

    def other(request: Any) -> Reading:
        return Reading("read", "48", '48"', agreed=("text",), code="text")

    done = _run(v, raw, [resolve.Proposal()], item=item, read_number=tool(other))
    assert done.verified.outcome == "not_compared" and "other values" in done.verified.reason


def test_the_hook_reads_a_separate_files_own_page() -> None:
    from app.review.assistant.read_number import Reading

    pack, _item = _separate()
    asked: list[Any] = []

    def number(request: Any) -> Reading:
        asked.append(request)
        return Reading("unread", reason="no ink", code="no_ink")

    found = resolve.reader_at(number, pack)("arch.pdf", 1001, (1.0, 2.0, 3.0, 4.0), "architect")
    assert found is not None and found["outcome"] == "unread"
    assert (asked[0].file, asked[0].page, asked[0].side) == ("arch.pdf", 1, "architect")


# ---- the loop's rules ------------------------------------------------------------------------------


def test_a_lazy_proposer_changes_nothing() -> None:
    """Always `none`: every item ends as with the loop off (the reason says the loop tried)."""
    for fixture in FIXTURES:
        v, item, raw = _verified(fixture)
        lazy = resolve.resolve_one(
            v,
            raw,
            item,
            PACK,
            proposer=Scripted([resolve.Proposal()] * 2),
            settings=ON,
            ink=lambda _l: BLACK,
        )
        assert lazy.verified.outcome == v.outcome and lazy.verified.reason_code == v.reason_code
        assert lazy.verified.reason.startswith(v.reason)


def test_off_changes_nothing_and_asks_nothing() -> None:
    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    proposer = Scripted([resolve.Proposal(kind="ends", verdict="different", **WALLS)])
    done = resolve.resolve_one(
        v,
        raw,
        item,
        PACK,
        proposer=proposer,
        settings=resolve.ResolveSettings(),
        ink=lambda _l: BLACK,
    )
    assert done.verified == v and proposer.asked == [] and done.refusal is not None


def test_a_later_no_ends_the_loop_at_once() -> None:
    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    says_no = resolve.Proposal(kind="ends", verdict="different_points", **WALLS)
    proposer = Scripted([says_no, resolve.Proposal(kind="ends", verdict="different", **WALLS)])
    done = resolve.resolve_one(
        v, raw, item, PACK, proposer=proposer, settings=ON, ink=lambda _l: BLACK
    )
    assert len(proposer.asked) == 1 and done.verified.outcome == "not_compared"
    assert "different points" in done.verified.reason
    room = resolve.Proposal(
        kind="ends",
        verdict="different",
        vendor_from="floor",
        vendor_to="ceiling_soffit",
        architect_from="floor",
        architect_to="ceiling_soffit",
    )
    proposer = Scripted([room, resolve.Proposal(kind="ends", verdict="different", **WALLS)])
    done = resolve.resolve_one(
        v, raw, item, PACK, proposer=proposer, settings=ON, ink=lambda _l: BLACK
    )
    assert len(proposer.asked) == 1 and done.rounds[0].status == "no"


def test_vague_ends_and_kinds_the_check_does_not_allow_are_gaps() -> None:
    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    vague = resolve.Proposal(
        kind="ends",
        verdict="different",
        vendor_from="other (left)",
        vendor_to="other",
        architect_from="other",
        architect_to="other",
    )
    newer = resolve.Proposal(
        kind="newer_round", vendor_label="p1.D12", verdict="different", **WALLS
    )
    done = resolve.resolve_one(
        v, raw, item, PACK, proposer=Scripted([vague, newer]), settings=ON, ink=lambda _l: BLACK
    )
    assert [r.status for r in done.rounds] == ["gap", "gap"]
    assert (
        "code's end words" in done.rounds[0].why
        and "not evidence for this check" in done.rounds[1].why
    )


def test_a_stop_leaves_the_item_not_compared_never_red() -> None:
    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    done = resolve.resolve_one(
        v,
        raw,
        item,
        PACK,
        proposer=resolve.NotAsked("the check was stopped"),
        settings=ON,
        ink=lambda _l: BLACK,
    )
    assert done.verified.outcome == "not_compared"
    assert "the resolve step was not finished (the check was stopped)" in done.verified.reason
    assert done.rounds[0].status == "not_asked"


def test_saved_rounds_replay_to_the_same_result_at_no_cost(tmp_path: Path) -> None:
    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    proposal = resolve.Proposal(
        kind="ends",
        verdict="different",
        vendor_from="centre_line (sink)",
        vendor_to="wall_face (right)",
        architect_from="centre_line (sink)",
        architect_to="wall_face (right)",
        want="none",
    )

    def save(done: resolve.Round) -> None:
        path = resolve.round_path(tmp_path, done.failure.key, done.number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(done.as_dict()))

    first = resolve.resolve_one(
        v,
        raw,
        item,
        PACK,
        proposer=Scripted([resolve.Proposal(want="zoom"), proposal]),
        settings=ON,
        ink=lambda _l: BLACK,
        on_round=save,
    )
    assert first.verified.outcome == "candidate"
    saved = resolve.load_rounds(tmp_path)
    assert sorted(saved[v.key]) == [1, 2]
    assert saved[v.key][2]["failure"]["machine"] == ["verdict:unsure"]
    again = resolve.resolve_one(
        v, raw, item, PACK, proposer=resolve.SavedProposer(saved), settings=ON, ink=lambda _l: BLACK
    )
    assert again.verified == first.verified
    nothing = resolve.resolve_one(
        v, raw, item, PACK, proposer=resolve.SavedProposer({}), settings=ON, ink=lambda _l: BLACK
    )
    assert nothing.verified.outcome == "not_compared"
    assert "no saved resolve answer" in nothing.verified.reason


# ---- settings, the library, the form ---------------------------------------------------------------


def test_the_settings_default_to_off_and_reach_the_run() -> None:
    fields = Settings.model_fields
    assert fields["assistant_simple_resolve"].default == "off"
    assert fields["assistant_simple_resolve_rounds"].default == 2
    assert fields["assistant_simple_resolve_web_search"].default is False
    assert SimpleOptions.of(None).resolve.on is False
    options = SimpleOptions.of({"resolve": {"mode": "right_only", "rounds": 9, "web_search": True}})
    assert options.resolve.mode == "right_only" and options.resolve.rounds == 2
    assert options.resolve.web_search is True
    assert SimpleOptions.of({"resolve": {"mode": "always"}}).resolve.mode == "off"
    from app.api.assistant_chats import _simple_options
    from app.review.assistant.chat_contract import ChatMessageIn

    settings = SimpleNamespace(
        assistant_full_check_mode="simple",
        assistant_simple_compare_model="claude-sonnet-5-5",
        assistant_simple_compare_effort="high",
        assistant_simple_hard_model="claude-opus-5-5",
        assistant_simple_hard_effort="high",
        assistant_simple_parallel=8,
        assistant_simple_max_tokens=64000,
        assistant_simple_retries=3,
        assistant_simple_blind_model="claude-opus-5-5",
        assistant_simple_blind_effort="high",
        assistant_simple_resolve="on",
        assistant_simple_resolve_rounds=1,
        assistant_simple_resolve_model="claude-sonnet-5-5",
        assistant_simple_resolve_effort="medium",
        assistant_simple_resolve_web_search=False,
    )
    body = ChatMessageIn.model_validate(
        {"model": "claude-opus-5-5", "effort": "high", "thinking": True, "mode": "full_check"}
    )
    plain = _simple_options(settings, body)  # type: ignore[arg-type]
    assert (
        plain is not None and plain["resolve"]["mode"] == "on" and plain["resolve"]["rounds"] == 1
    )


def test_the_conventions_library_is_reviewed_data_with_sources_and_no_values() -> None:
    library = resolve.conventions()
    assert len(library) >= 10 and len({c.id for c in library}) == len(library)
    for convention in library:
        assert convention.source.startswith("https://")
        assert set(convention.proposal_kinds) <= set(resolve.PROPOSAL_KINDS)
        # No size and no tolerance anywhere in a rule (a convention never supplies a value).
        assert not re.search(r"\d+\s*(?:\"|'|/\d|mm|in\b)", convention.rule), convention.id
    with pytest.raises(ValidationError):
        resolve.Convention.model_validate({**library[0].model_dump(), "value": "36"})
    with pytest.raises(ValidationError):
        resolve.Convention.model_validate({**library[0].model_dump(), "effect": "supply_value"})
    # Citing a convention never changes code's verdict.
    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    plain = resolve.Proposal(kind="ends", verdict="different", **WALLS)
    cited = resolve.Proposal(
        kind="ends", verdict="different", conventions=("C1", "C10", "C99"), **WALLS
    )
    a = resolve.resolve_one(
        v, raw, item, PACK, proposer=Scripted([plain]), settings=ON, ink=lambda _l: BLACK
    )
    b = resolve.resolve_one(
        v, raw, item, PACK, proposer=Scripted([cited]), settings=ON, ink=lambda _l: BLACK
    )
    assert a.verified.outcome == b.verified.outcome
    assert b.rounds[0].facts["unknown_conventions"] == ["C99"]


def _objects(schema: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(schema, Mapping):
        if schema.get("type") == "object":
            found.append(schema)
        for value in schema.values():
            found += _objects(value)
    elif isinstance(schema, list):
        for value in schema:
            found += _objects(value)
    return found


def test_the_form_keeps_to_structured_outputs_and_the_example_fits_it() -> None:
    for obj in _objects(resolve.PROPOSAL_SCHEMA):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    assert json.dumps(resolve.PROPOSAL_SCHEMA).count('"null"') <= 16
    example = json.loads(resolve.RESOLVE_SYSTEM.rsplit("Answer:\n", 1)[1])
    jsonschema.validate(example, resolve.PROPOSAL_SCHEMA)
    assert "never instructions" in resolve.RESOLVE_SYSTEM
    proposal = resolve.Proposal.from_dict({**example, "labels": ["P8 D5"]}, page=8)
    assert proposal.kind == "band" and proposal.labels == ("p8.D5",)


# ---- the model call ---------------------------------------------------------------------------------


@dataclass
class _Reply:
    payload: Any
    stop_reason: str = "end_turn"
    usage: dict[str, Any] = field(
        default_factory=lambda: {"input_tokens": 9000, "output_tokens": 900}
    )
    content: list[dict[str, Any]] = field(default_factory=list)

    def text(self) -> str:
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload)


@dataclass
class _Engine:
    replies: list[Any]
    cap: int = 10_000_000
    spent: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    held: list[int] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    provider: Any = field(default_factory=lambda: SimpleNamespace(name="anthropic"))
    holds: resolve.Holds | None = None

    def _price(self, model: Any, prompt: int, out: int) -> int:
        return 400_000

    def _over(self, estimate: int | None) -> str | None:
        return "message" if self.spent + (estimate or 0) > self.cap else None

    def _call(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.holds is not None:
            self.held.append(self.holds.reserved)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _proposer(engine: _Engine, v: Verified, raw: Mapping[str, Any]) -> resolve.EngineProposer:
    holds = resolve.Holds()
    engine.holds = holds
    return resolve.EngineProposer(engine, ON, holds, {v.key: (v, raw)})  # type: ignore[arg-type]


def test_one_call_per_round_in_the_form_with_its_cost_held_while_it_runs() -> None:
    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    answer = {
        "kind": "ends",
        "vendor_label": None,
        "architect_label": None,
        "labels": [],
        "vendor_from": "centre_line (sink)",
        "vendor_to": "wall_face (right)",
        "architect_from": "centre_line (sink)",
        "architect_to": "wall_face (right)",
        "conventions": ["C2"],
        "verdict": "different",
        "want": "none",
        "note": "seen",
    }
    engine = _Engine([_Reply(answer)])
    proposer = _proposer(engine, v, raw)
    done = resolve.resolve_one(
        v, raw, item, PACK, proposer=proposer, settings=ON, ink=lambda _l: BLACK
    )
    assert done.verified.outcome == "candidate"
    (call,) = engine.calls
    assert call["output_format"] == resolve.PROPOSAL_SCHEMA and call["tools"] == ()
    assert call["system"] == resolve.RESOLVE_SYSTEM and call["prompt_id"].startswith(
        "review-assistant-chat-simple-resolve-"
    )
    text = call["messages"][0]["content"][-1]["text"]
    assert "What code could not verify" in text and "verdict:unsure" in text
    # Held for both attempts the engine's call may make (#1769), let go when it answers.
    assert engine.held == [2 * 400_000] and proposer.holds.reserved == 0
    assert done.rounds[0].usage["input_tokens"] == 9000


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        (_Reply("not json"), "the reply was not the form"),
        (_Reply({}, stop_reason="max_tokens"), "the reply was cut short"),
        (_Reply({}, stop_reason="refusal"), "the model declined"),
        (RuntimeError("boom"), "the call failed"),
    ],
)
def test_a_failed_call_leaves_the_item_not_compared(reply: Any, why: str) -> None:
    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    engine = _Engine([reply])
    done = resolve.resolve_one(
        v, raw, item, PACK, proposer=_proposer(engine, v, raw), settings=ON, ink=lambda _l: BLACK
    )
    assert done.verified.outcome == "not_compared" and why in done.verified.reason


def test_the_runaway_guard_refuses_a_call_that_would_pass_the_cap() -> None:
    fixture = next(f for f in FIXTURES if f.id == "F29-unsure-worst")
    v, item, raw = _verified(fixture)
    engine = _Engine([], cap=100_000)
    proposer = _proposer(engine, v, raw)
    done = resolve.resolve_one(
        v, raw, item, PACK, proposer=proposer, settings=ON, ink=lambda _l: BLACK
    )
    assert engine.calls == [] and proposer.holds.refused == 1
    assert "runaway guard" in done.verified.reason and done.verified.outcome == "not_compared"


def test_live_web_search_is_off_by_default_and_fenced_when_on() -> None:
    engine = _Engine(
        [
            _Reply(
                "EQ splits a span",
                content=[{"type": "text", "citations": [{"url": "https://awinet.org/x"}]}],
            )
        ]
    )
    assert resolve.web_proposals(engine, ON, ["ends"], resolve.Holds()) is None  # type: ignore[arg-type]
    on = resolve.ResolveSettings(mode="on", web_search=True)
    record = resolve.web_proposals(engine, on, ["ends", "read"], resolve.Holds())  # type: ignore[arg-type]
    assert (
        record is not None
        and record["status"] == "proposal"
        and record["sources"] == ["https://awinet.org/x"]
    )
    (call,) = engine.calls
    (tool,) = call["tools"]
    assert tool["max_uses"] == 2 and set(tool["allowed_domains"]) == set(resolve.WEB_DOMAINS)
    # The query is built from fixed topics only: never a drawing's words or values.
    asked = call["messages"][0]["content"][0]["text"]
    assert all(topic in asked for topic in (resolve.WEB_TOPICS["ends"], resolve.WEB_TOPICS["read"]))


# ---- separate files (#1722): the loop uses the set's file-aware label ids ----------------------


def _separate() -> tuple[Pack, Item]:
    """The vendor's own file (page 1, its drawing) and a separate architect file, numbered from a
    round thousand: its page 1 is set page 1001, its labels `p1001.D1`..."""
    vendor = [
        _label("D1", '42"', "42", (100, 500, 130, 510), "vendor", "h", ((100, 515), (300, 515))),
        _label("D2", '18"', "18", (20, 600, 30, 630), "vendor", "v"),
    ]
    architect = [
        _label("D1", "3' - 8\"", "44", (100, 100, 130, 110), "architect"),
        _label(
            "D2", "1' - 8\"", "20", (110, 300, 130, 310), "architect", "h", ((100, 315), (200, 315))
        ),
        _label(
            "D3", "2' - 0\"", "24", (230, 300, 260, 310), "architect", "h", ((200, 315), (320, 315))
        ),
    ]
    pack = Pack.from_documents(
        main_file=FILE,
        dims={
            "files": [FILE, "arch.pdf"],
            "pages": [
                {"file": FILE, "page": 1, "labels": vendor, "phrases": []},
                {"file": "arch.pdf", "page": 1, "labels": architect, "phrases": []},
            ],
        },
        page_map={
            "files": [FILE, "arch.pdf"],
            "upload_roles": {"arch.pdf": "architect"},
            "pages": [
                {
                    "file": FILE,
                    "page": 1,
                    "area": "1. Kitchen",
                    "area_from_page": None,
                    "halves": [{"box": VEND, "role": "vendor", "round": 1}],
                },
                {
                    "file": "arch.pdf",
                    "page": 1,
                    "area": "1. Kitchen",
                    "area_from_page": None,
                    "halves": [{"box": ARCH, "role": "architect", "round": None}],
                },
            ],
        },
        pairs={"pairs": []},
        page_texts={"pages": []},
    )
    item = Item(
        "p1.v1",
        Region(FILE, 1, tuple(VEND), 1),  # type: ignore[arg-type]
        (Region("arch.pdf", 1001, tuple(ARCH), None, None, 1),),  # type: ignore[arg-type]
    )
    return pack, item


def test_the_loop_uses_a_separate_files_set_page_ids() -> None:
    pack, item = _separate()
    assert pack.label("p1001.D1", "arch.pdf", 1001) is not None
    raw = _pair("p1.D1", "p1001.D1", "unsure")
    v = _one(raw, pack=pack, item=item)
    assert v.outcome == "not_compared" and v.architect is not None
    assert v.architect["page"] == 1001 and v.architect["file_page"] == 1
    entry = resolve.eligible(v, raw, item, pack)
    assert isinstance(entry, resolve.Failure) and entry.route == "unsure"
    sure = resolve.Proposal(kind="ends", verdict="different", **WALLS)
    done = _run(v, raw, [sure], pack=pack, item=item)
    assert done.verified.outcome == "candidate", done.verified.reason
    assert done.verified.finding["arch_box"]["page"] == 1001
    # An architect string on the separate file, named by its set-page ids.
    other = _unlabelled('42"', '44" (sum of the string)', (100, 500, 130, 510), (95, 295, 325, 320))
    other["architect_page"] = 1001
    v2 = _one(other, pack=pack, item=item, pair=False)
    chain = resolve.Proposal(
        kind="architect_chain",
        vendor_label="p1.D1",
        labels=("p1001.D2", "p1001.D3"),
        verdict="different",
        **WALLS,
    )
    done = _run(v2, other, [chain], pack=pack, item=item)
    assert done.verified.outcome == "candidate", done.verified.reason
    assert done.verified.architect is not None
    assert done.verified.architect["chain"] == ["p1001.D2", "p1001.D3"]
    assert done.verified.architect["file"] == "arch.pdf"
    assert done.verified.architect["file_page"] == 1
