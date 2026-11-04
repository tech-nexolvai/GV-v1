"""Reading the unread pen-stroke numbers with two makers and the drawing's scale (Step 2).

Verification for `app/review/assistant/stroke_reads.py`. Fake pages and fake readers only: no
paid call, no PDF. Every value is invented; no client value ("68" against "89" is the generic
upside-down example). The views are drawn at 1/4" per point.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.review.assistant import stroke_reads as sr
from app.runs.rates import ModelRate
from extraction.ink import InkAt, InkClass
from extraction.slot_reader.seal import ReaderAnswer

SONNET = "anthropic.claude-sonnet-5-5"
OPUS = "anthropic.claude-opus-5-5"
QWEN = "qwen.qwen3-vl-235b-a22b"
KIMI = "us.moonshotai.kimi-k3"
SCALE = Fraction(1, 4)
FILE = "Vendor drawings.pdf"
VENDOR_INK = InkAt(InkClass.VENDOR, "")

LABEL_KEYS = {
    "id",
    "file",
    "page",
    "side",
    "box",
    "reasons",
    "turns_tried",
    "answers",
    "state",
    "why",
    "turn",
    "text",
    "inches",
}


def png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("L", (width, height), 255).save(out, format="PNG")
    return out.getvalue()


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as opened:
        return opened.size


def spot(
    box: list[float], reason: str = "other", *also: str, side: str = "architect"
) -> dict[str, Any]:
    return {"kind": "strokes", "side": side, "reason": reason, "also": list(also), "box": box}


def write_pack(
    folder: Path, spots: list[dict[str, Any]], *, page: int = 3, version: int = 26
) -> Path:
    coverage = {
        "schema": "gv-reading-coverage/1",
        "pages": [
            {
                "file": FILE,
                "page": page,
                "not_read": [
                    {"kind": "typed", "side": "vendor", "reason": "in_note", "box": [1, 1, 2, 2]},
                    *spots,
                ],
            }
        ],
    }
    (folder / "reading_coverage.json").write_text(json.dumps(coverage))
    (folder / "status.json").write_text(json.dumps({"state": "ready", "version": version}))
    return folder


class FakePages:
    """Each spot's turns, line (drawn inches at SCALE), scale, neighbours and picture size."""

    def __init__(
        self,
        *,
        turns: Mapping[str, tuple[int, ...]] | None = None,
        drawn: Mapping[str, int | None] | None = None,
        scaled: bool = True,
        others: Sequence[tuple[float, float, float, float]] = (),
        ink: InkAt | None = VENDOR_INK,
    ) -> None:
        self._turns = dict(turns or {})
        self._drawn = dict(drawn or {})
        self._scaled = scaled
        self._others = list(others)
        self._ink = ink
        self.crops: dict[str, tuple[float, float, float, float]] = {}

    def turns(self, spot: sr.Spot) -> tuple[int, ...] | None:
        return self._turns.get(spot.id)

    def line(self, spot: sr.Spot) -> sr.LineFound | None:
        inches = self._drawn.get(spot.id)
        if inches is None:
            return None
        length = float(Fraction(inches) / SCALE)
        return sr.LineFound("v", spot.box[0] - 3, 100.0, 100.0 + length)

    def scale(self, spot: sr.Spot, line: sr.LineFound) -> Fraction | None:
        return SCALE if self._scaled else None

    def others(self, spot: sr.Spot) -> Sequence[tuple[float, float, float, float]]:
        return self._others

    def page_size(self, spot: sr.Spot) -> tuple[float, float] | None:
        return (1000.0, 800.0)

    def picture(self, spot: sr.Spot, crop: tuple[float, float, float, float]) -> bytes | None:
        self.crops[spot.id] = crop
        return png(max(1, round(crop[2] - crop[0])), max(1, round(crop[3] - crop[1])))

    def ink(self, spot: sr.Spot, crop: tuple[float, float, float, float]) -> InkAt | None:
        return self._ink


def answer(model: str, text: str) -> ReaderAnswer:
    return ReaderAnswer(
        model_id=model, text=text, readable=True, no_dimension=False, stacked=False, combined=False
    )


class FakeReader:
    """Two makers; `texts` gives each crop key's text (`p3.S1@90`), the same for both readers."""

    def __init__(
        self,
        texts: Mapping[str, str],
        models: tuple[str, str] = (SONNET, QWEN),
        *,
        fail: bool = False,
    ) -> None:
        self.texts = dict(texts)
        self.models = models
        self.fail = fail
        self.batches: list[list[sr.CropAsk]] = []
        self.budgets: list[sr.ReadBudget] = []

    @property
    def readers(self) -> tuple[sr.ReaderInfo, ...]:
        from extraction.slot_reader.seal import _maker

        return tuple(sr.ReaderInfo(m, _maker(m)) for m in self.models)

    def read(self, asks: Sequence[sr.CropAsk], budget: sr.ReadBudget) -> sr.ReadResult:
        self.batches.append(list(asks))
        self.budgets.append(budget)
        if self.fail:
            raise RuntimeError("the account refused")
        found: dict[tuple[str, str], ReaderAnswer | None] = {}
        for ask in asks:
            for model in self.models:
                text = self.texts.get(ask.key)
                found[(ask.key, model)] = None if text is None else answer(model, text)
        return sr.ReadResult(found, cost_micros=1000 * len(asks))

    @property
    def keys(self) -> list[str]:
        return [ask.key for batch in self.batches for ask in batch]


def labels_of(run: sr.StrokeReadsRun) -> dict[str, dict[str, Any]]:
    return {label["id"]: label for label in run.document["labels"]}


# ---- the setting ---------------------------------------------------------------------------------


def test_the_setting_is_off_unless_it_names_a_mode() -> None:
    assert sr.mode_from_environment({}) == "off"
    assert sr.mode_from_environment({sr.MODE_ENV: " Paired "}) == "paired"
    assert sr.mode_from_environment({sr.MODE_ENV: "all"}) == "all"
    assert sr.mode_from_environment({sr.MODE_ENV: "yes"}) == "off"


def test_the_cap_is_two_dollars_unless_set_and_never_unknown() -> None:
    assert sr.cap_from_environment({}) == Decimal(2)
    assert sr.cap_from_environment({sr.CAP_ENV: "0.75"}) == Decimal("0.75")
    for bad in ("lots", "-1", "NaN", "Infinity"):
        assert sr.cap_from_environment({sr.CAP_ENV: bad}) == Decimal(0)


# ---- what is read --------------------------------------------------------------------------------


def test_spots_come_from_the_coverage_numbered_per_page_strokes_only(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([10, 10, 20, 15]), spot([30, 10, 34, 30], "mm_row", "sideways")])
    coverage = json.loads((tmp_path / "reading_coverage.json").read_text())
    found = sr.spots_of(coverage)
    assert [(s.id, s.reasons, s.sideways) for s in found] == [
        ("p3.S1", ("other",), False),
        ("p3.S2", ("mm_row", "sideways"), True),
    ]


def test_a_scope_holds_its_pages_and_within_a_page_its_views() -> None:
    inside = sr.Spot("p3.S1", FILE, 3, "architect", (10, 10, 20, 15), ())
    outside = sr.Spot("p3.S2", FILE, 3, "architect", (400, 400, 410, 405), ())
    other_page = sr.Spot("p4.S1", FILE, 4, "architect", (10, 10, 20, 15), ())
    assert sr.Scope().holds(other_page)
    paged = sr.Scope(pages=frozenset({(FILE, 3)}))
    assert paged.holds(inside) and paged.holds(outside) and not paged.holds(other_page)
    viewed = sr.Scope(views={(FILE, 3): ((0.0, 0.0, 100.0, 100.0),)})
    assert viewed.holds(inside) and not viewed.holds(outside) and viewed.holds(other_page)


def test_inches_are_written_as_an_exact_fraction() -> None:
    assert sr.inches_text(Fraction(69, 2)) == "69/2"
    assert sr.inches_text(Fraction(68)) == "68"
    assert sr.inches_text(None) is None


# ---- reading -------------------------------------------------------------------------------------


def test_an_upright_number_on_its_drawn_length_is_sealed_witnessed(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])])
    reader = FakeReader({"p3.S1@0": '24"'})
    run = sr.ensure_stroke_reads(
        tmp_path, None, reader, Decimal("1.5"), pages=FakePages(drawn={"p3.S1": 24})
    )
    label = labels_of(run)["p3.S1"]
    assert set(label) == LABEL_KEYS
    assert (label["state"], label["turn"], label["text"], label["inches"]) == (
        "sealed_witnessed",
        0,
        '24"',
        "24",
    )
    assert label["turns_tried"] == [0]
    assert label["answers"] == [
        {"model": SONNET, "turn": 0, "text": '24"'},
        {"model": QWEN, "turn": 0, "text": '24"'},
    ]
    assert reader.budgets[0].cap_usd == Decimal("1.5")
    assert run.cost_micros == 1000


def test_the_file_has_exactly_the_agreed_schema(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])], version=26)
    sr.ensure_stroke_reads(
        tmp_path,
        None,
        FakeReader({"p3.S1@0": '24"'}),
        Decimal(1),
        pages=FakePages(drawn={"p3.S1": 24}),
    )
    document = json.loads((tmp_path / sr.FILE).read_text())
    assert set(document) == {"schema", "pack_version", "readers", "labels"}
    assert document["schema"] == "gv-stroke-reads/1"
    assert document["pack_version"] == 26
    assert document["readers"] == [
        {"model": SONNET, "maker": "vendor:anthropic"},
        {"model": QWEN, "maker": "vendor:qwen"},
    ]
    assert set(document["labels"][0]) == LABEL_KEYS
    assert sr.load(tmp_path) == document


def test_68_drawn_top_to_bottom_is_read_again_turned_and_sealed_at_270(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 108, 130], "sideways")])
    reader = FakeReader({"p3.S1@90": '89"', "p3.S1@270": '68"'})
    pages = FakePages(turns={"p3.S1": (90,)}, drawn={"p3.S1": 68})
    run = sr.ensure_stroke_reads(tmp_path, None, reader, Decimal(1), pages=pages)
    label = labels_of(run)["p3.S1"]
    assert (label["state"], label["turn"], label["inches"]) == ("sealed_witnessed", 270, "68")
    assert label["turns_tried"] == [90, 270]
    assert reader.keys == ["p3.S1@90", "p3.S1@270"]  # the flip only once the first did not fit
    # Each copy is turned upright: a crop taller than wide comes out wider than tall.
    width, height = size_of(reader.batches[0][0].png)
    assert width > height


def test_a_sideways_number_that_fits_at_its_first_turn_is_not_read_again(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 108, 130], "sideways")])
    reader = FakeReader({"p3.S1@270": '68"'})
    pages = FakePages(turns={"p3.S1": (270,)}, drawn={"p3.S1": 68})
    run = sr.ensure_stroke_reads(tmp_path, None, reader, Decimal(1), pages=pages)
    assert labels_of(run)["p3.S1"]["state"] == "sealed_witnessed"
    assert reader.keys == ["p3.S1@270"]


def test_a_sideways_number_code_cannot_place_is_read_both_ways(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 108, 130], "sideways")])
    reader = FakeReader({"p3.S1@90": '40"', "p3.S1@270": '42"'})
    run = sr.ensure_stroke_reads(
        tmp_path, None, reader, Decimal(1), pages=FakePages(drawn={"p3.S1": 41})
    )
    label = labels_of(run)["p3.S1"]
    assert (label["state"], label["why"]) == ("held", "both orientations fit")
    assert sorted(reader.keys) == ["p3.S1@270", "p3.S1@90"]


def test_a_sideways_number_with_no_scale_is_never_read(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 108, 130], "sideways")])
    reader = FakeReader({"p3.S1@90": '68"', "p3.S1@270": '68"'})
    for pages in (FakePages(drawn={"p3.S1": 68}, scaled=False), FakePages(drawn={})):
        run = sr.ensure_stroke_reads(tmp_path, None, reader, Decimal(1), pages=pages)
        label = labels_of(run)["p3.S1"]
        assert (label["state"], label["why"], label["turns_tried"]) == (
            "unread",
            "no scale for a sideways label",
            [],
        )
    assert reader.keys == []


def test_an_upright_number_with_no_scale_is_sealed_unwitnessed(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])])
    run = sr.ensure_stroke_reads(
        tmp_path, None, FakeReader({"p3.S1@0": '24"'}), Decimal(1), pages=FakePages()
    )
    label = labels_of(run)["p3.S1"]
    assert (label["state"], label["inches"]) == ("sealed_unwitnessed", "24")


def test_a_stacked_fraction_or_gv_sticker_never_seals(tmp_path: Path) -> None:
    write_pack(
        tmp_path,
        [spot([100, 100, 130, 110], "stacked_fraction"), spot([300, 100, 330, 110], "gv_sticker")],
    )
    reader = FakeReader({"p3.S1@0": '24 1/2"', "p3.S2@0": '24"'})
    run = sr.ensure_stroke_reads(
        tmp_path,
        None,
        reader,
        Decimal(1),
        pages=FakePages(drawn={"p3.S1": 24, "p3.S2": 24}),
    )
    found = labels_of(run)
    assert found["p3.S1"]["state"] == "unread" and found["p3.S1"]["why"] == "stacked fraction"
    assert found["p3.S2"]["state"] == "unread"
    assert "GV's sticker" in found["p3.S2"]["why"]


def test_unknown_ink_holds_the_reading(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])])
    run = sr.ensure_stroke_reads(
        tmp_path,
        None,
        FakeReader({"p3.S1@0": '24"'}),
        Decimal(1),
        pages=FakePages(drawn={"p3.S1": 24}, ink=None),
    )
    assert labels_of(run)["p3.S1"]["state"] == "unread"


def test_the_crop_is_padded_and_clipped_short_of_a_neighbour(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])])
    pages = FakePages(others=[(132, 100, 150, 110)])
    sr.ensure_stroke_reads(tmp_path, None, FakeReader({}), Decimal(1), pages=pages)
    x0, y0, x1, y1 = pages.crops["p3.S1"]
    assert x0 == pytest.approx(96) and y0 == pytest.approx(96) and y1 == pytest.approx(114)
    assert x1 == pytest.approx(131)  # one point short of the neighbour (E2's clip gap)


def test_the_crop_reaches_down_to_its_own_line(tmp_path: Path) -> None:
    """The fake line runs up the page 3 pt left of the ink (x = 97): the crop pads past it."""
    write_pack(tmp_path, [spot([100, 100, 130, 110])])
    pages = FakePages(drawn={"p3.S1": 24})
    sr.ensure_stroke_reads(tmp_path, None, FakeReader({}), Decimal(1), pages=pages)
    assert pages.crops["p3.S1"][0] == pytest.approx(93)


def test_a_box_another_overlaps_is_crowded_and_never_seals(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])])
    run = sr.ensure_stroke_reads(
        tmp_path,
        None,
        FakeReader({"p3.S1@0": '24"'}),
        Decimal(1),
        pages=FakePages(drawn={"p3.S1": 24}, others=[(120, 105, 140, 115)]),
    )
    label = labels_of(run)["p3.S1"]
    assert label["state"] == "unread" and "overlaps" in label["why"]


def test_only_the_scope_is_read(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])], page=3)
    reader = FakeReader({"p3.S1@0": '24"'})
    run = sr.ensure_stroke_reads(
        tmp_path,
        sr.Scope(pages=frozenset({(FILE, 9)})),
        reader,
        Decimal(1),
        pages=FakePages(drawn={"p3.S1": 24}),
    )
    assert run.document["labels"] == [] and reader.keys == []


def test_a_reading_that_stops_keeps_every_label_unread_with_why(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])])
    run = sr.ensure_stroke_reads(
        tmp_path,
        None,
        FakeReader({}, fail=True),
        Decimal(1),
        pages=FakePages(drawn={"p3.S1": 24}),
    )
    label = labels_of(run)["p3.S1"]
    assert (label["state"], label["why"]) == ("unread", "not read: RuntimeError")
    assert run.stopped == "RuntimeError"


def test_one_maker_readers_are_refused(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])])
    with pytest.raises(ValueError):
        sr.ensure_stroke_reads(
            tmp_path, None, FakeReader({}, (OPUS, SONNET)), Decimal(1), pages=FakePages()
        )


def test_turned_copies_turn_clockwise() -> None:
    picture = Image.new("L", (2, 3), 255)
    picture.putpixel((0, 0), 0)  # top left
    out = io.BytesIO()
    picture.save(out, format="PNG")
    with Image.open(io.BytesIO(sr.turned(out.getvalue(), 90))) as turned:
        assert turned.size == (3, 2)
        assert turned.getpixel((2, 0)) == 0  # top left goes to top right, turned clockwise
    with Image.open(io.BytesIO(sr.turned(out.getvalue(), 270))) as turned:
        assert turned.getpixel((0, 1)) == 0  # ... and to bottom left the other way
    assert sr.turned(out.getvalue(), 0) == out.getvalue()


# ---- production pieces with fake clients ---------------------------------------------------------


class Rates:
    def rate_for(self, model_id: str) -> ModelRate | None:
        if model_id not in {SONNET, QWEN, KIMI}:
            return None
        return ModelRate(Decimal("0.003"), Decimal("0.015"))


class FakeClient:
    def __init__(self, name: str, seen: list[tuple[str, str]], text: str) -> None:
        self.name, self.seen, self.text = name, seen, text

    def converse(self, **request: Any) -> Mapping[str, Any]:
        self.seen.append((self.name, request["modelId"]))
        body = {
            "text": self.text,
            "stacked": False,
            "combined": False,
            "readable": True,
            "no_dimension": False,
        }
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": json.dumps(body)}]}},
            "usage": {"inputTokens": 1000, "outputTokens": 100},
        }


class Provider:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def for_current_thread(self) -> FakeClient:
        return self.client


def test_each_reader_goes_to_its_own_route() -> None:
    seen: list[tuple[str, str]] = []
    routed = sr._RoutedClients(
        Provider(FakeClient("claude", seen, '1"')), Provider(FakeClient("bedrock", seen, '1"'))
    ).for_current_thread()
    routed.converse(modelId=SONNET)
    routed.converse(modelId=QWEN)
    assert seen == [("claude", SONNET), ("bedrock", QWEN)]
    with pytest.raises(ValueError):
        sr._RoutedClients(
            None, Provider(FakeClient("bedrock", seen, '1"'))
        ).for_current_thread().converse(modelId=SONNET)


def test_the_production_reader_reuses_the_slot_readers_parallel_reading() -> None:
    seen: list[tuple[str, str]] = []
    clients = sr._RoutedClients(None, Provider(FakeClient("bedrock", seen, '24"')))
    reader = sr.SlotPairReader(
        readers=(KIMI, QWEN),
        clients=clients,
        rates=Rates(),
        calls_per_minute={KIMI: 6000, QWEN: 6000},
        max_concurrent_calls=2,
        retry_backoff_seconds=0.001,
    )
    assert [r.maker for r in reader.readers] == ["vendor:moonshot", "vendor:qwen"]
    budget = sr.ReadBudget(Decimal(5))
    result = reader.read([sr.CropAsk("p3.S1@0", 2, png(40, 20))], budget)
    assert {key: a.text for key, a in result.answers.items() if a} == {
        ("p3.S1@0", KIMI): '24"',
        ("p3.S1@0", QWEN): '24"',
    }
    assert sorted(model for _name, model in seen) == [QWEN, KIMI]
    # 1,000 in and 100 out at $0.003 / $0.015 per thousand: $0.0045 a call, two calls.
    assert result.cost_micros == 9000
    assert budget.guard is not None and budget.stop is not None


def test_the_production_reader_refuses_one_maker_or_unpriced_or_unpaced_readers() -> None:
    clients = sr._RoutedClients(None, None)
    with pytest.raises(ValueError):
        sr.SlotPairReader(
            readers=(OPUS, SONNET), clients=clients, rates=Rates(), calls_per_minute={}
        )
    with pytest.raises(ValueError):
        sr.SlotPairReader(
            readers=(SONNET, "mistral.unknown"),
            clients=clients,
            rates=Rates(),
            calls_per_minute={SONNET: 1, "mistral.unknown": 1},
        )
    with pytest.raises(ValueError):
        sr.SlotPairReader(
            readers=(SONNET, QWEN), clients=clients, rates=Rates(), calls_per_minute={SONNET: 1}
        )


def test_the_spend_cap_holds_every_call_back() -> None:
    seen: list[tuple[str, str]] = []
    reader = sr.SlotPairReader(
        readers=(KIMI, QWEN),
        clients=sr._RoutedClients(None, Provider(FakeClient("bedrock", seen, '24"'))),
        rates=Rates(),
        calls_per_minute={KIMI: 6000, QWEN: 6000},
        retry_backoff_seconds=0.001,
    )
    result = reader.read([sr.CropAsk("p3.S1@0", 2, png(40, 20))], sr.ReadBudget(Decimal(0)))
    assert seen == [] and all(a is None for a in result.answers.values())
    assert result.stopped == "budget"


# ---- the full check's hook ------------------------------------------------------------------------


def test_off_by_default_the_full_check_builds_no_reader_and_writes_nothing(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])])
    made: list[int] = []

    def factory() -> FakeReader:
        made.append(1)
        return FakeReader({"p3.S1@0": '24"'})

    for environ in ({}, {sr.MODE_ENV: "off"}, {sr.MODE_ENV: "nonsense"}):
        found = sr.full_check_reads(
            pack=tmp_path,
            uploads=None,
            pages=None,
            room_micros=10_000_000,
            reader_factory=factory,
            environ=environ,
        )
        assert found is None
    assert made == [] and not (tmp_path / sr.FILE).exists()


def test_no_money_or_no_readers_reads_nothing(tmp_path: Path) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])])

    def broken() -> FakeReader:
        raise ValueError("no key")

    made: list[int] = []

    def factory() -> FakeReader:
        made.append(1)
        return FakeReader({})

    on = {sr.MODE_ENV: "all"}
    assert (
        sr.full_check_reads(
            pack=tmp_path,
            uploads=None,
            pages=None,
            room_micros=0,
            reader_factory=factory,
            environ=on,
        )
        is None
    )
    assert (
        sr.full_check_reads(
            pack=tmp_path,
            uploads=None,
            pages=None,
            room_micros=10**6,
            reader_factory=factory,
            environ={**on, sr.CAP_ENV: "0"},
        )
        is None
    )
    assert made == []
    assert (
        sr.full_check_reads(
            pack=tmp_path,
            uploads=None,
            pages=None,
            room_micros=10**6,
            reader_factory=broken,
            environ=on,
        )
        is None
    )
    assert not (tmp_path / sr.FILE).exists()


def test_on_the_cap_is_the_smaller_of_the_setting_and_the_room_and_paired_reads_its_pages(
    tmp_path: Path,
) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110])], page=3)
    reader = FakeReader({"p3.S1@0": '24"'})
    pages = FakePages(drawn={"p3.S1": 24})
    found = sr.full_check_reads(
        pack=tmp_path,
        uploads=None,
        pages=frozenset({(FILE, 3)}),
        room_micros=750_000,
        reader_factory=lambda: reader,
        environ={sr.MODE_ENV: "paired", sr.CAP_ENV: "2"},
        geometry=pages,
    )
    assert found is not None and found.counts == {"sealed_witnessed": 1}
    assert reader.budgets[0].cap_usd == Decimal("0.75")
    reader = FakeReader({"p3.S1@0": '24"'})
    found = sr.full_check_reads(
        pack=tmp_path,
        uploads=None,
        pages=frozenset({(FILE, 9)}),
        room_micros=10**7,
        reader_factory=lambda: reader,
        environ={sr.MODE_ENV: "paired", sr.CAP_ENV: "0.5"},
        geometry=pages,
    )
    assert found is not None and found.document["labels"] == [] and reader.keys == []


def test_the_settings_defaults_are_the_modules() -> None:
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["assistant_stroke_reads"].default == "off"
    assert fields["assistant_stroke_reads_cap_usd"].default == sr.DEFAULT_CAP_USD
    assert fields["assistant_stroke_readers"].default == ""
    assert sr.MODE_ENV == "GV_ASSISTANT_STROKE_READS"
    assert sr.CAP_ENV == "GV_ASSISTANT_STROKE_READS_CAP_USD"
    assert sr.READERS_ENV == "GV_ASSISTANT_STROKE_READERS"


def _roster_engine(pack: Path, charged: list[tuple[int, list[Any]]]) -> Any:
    from contextlib import contextmanager
    from types import SimpleNamespace

    @contextmanager
    def phase(name: str) -> Any:
        yield

    return SimpleNamespace(
        tools=SimpleNamespace(workspace=SimpleNamespace(set_pack=pack, uploads=pack)),
        book=SimpleNamespace(main_file=FILE),
        _cross=None,
        timings=SimpleNamespace(phase=phase),
        charge_outside=lambda cost, calls: charged.append((cost, list(calls))),
    )


def test_the_roster_hook_does_nothing_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default off: the hook returns before it looks at the engine, so no reader is built, no
    call is made, nothing is charged and nothing is written."""
    from types import SimpleNamespace

    from app.review.assistant import full_check_roster

    def never(**_given: Any) -> None:
        raise AssertionError("the stroke reads ran while off")

    monkeypatch.setattr(sr, "full_check_reads", never)
    run = full_check_roster._Run(engine=SimpleNamespace())  # type: ignore[arg-type]
    run._stroke_reads()
    assert run.stroke_block == {} and run.spent == {}


def test_the_roster_hook_reads_the_paired_pages_in_the_gap_closers_room_and_charges_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from app.review.assistant import full_check_roster

    monkeypatch.setenv(sr.MODE_ENV, "paired")
    asked: list[dict[str, Any]] = []
    attempt = SimpleNamespace(
        model_id=QWEN, prompt_id="slot-crop-v1", input_tokens=900, output_tokens=40
    )

    def fake(**given: Any) -> sr.StrokeReadsRun:
        asked.append(given)
        document = {"labels": [{"state": "sealed_witnessed"}, {"state": "unread"}]}
        return sr.StrokeReadsRun(document, 4_500, (attempt,), None)

    monkeypatch.setattr(sr, "full_check_reads", fake)
    monkeypatch.setattr(full_check_roster._Run, "_room", lambda self, phase: {"gaps": 333}[phase])
    charged: list[tuple[int, list[Any]]] = []
    run = full_check_roster._Run(engine=_roster_engine(tmp_path, charged))
    run.plan = SimpleNamespace(price=5_000_000)  # type: ignore[assignment]
    run.vendor_pages = frozenset({3, 4})
    run._stroke_reads()
    (given,) = asked
    assert given["room_micros"] == 333 and given["pack"] == tmp_path
    assert given["pages"] == frozenset({(FILE, 3), (FILE, 4)})
    assert charged == [(4_500, [(QWEN, "slot-crop-v1", 900, 40)])]
    assert run.spent == {"stroke_reads": 4_500}
    assert run.stroke_block["sealed_witnessed"] == 1 and run.stroke_block["calls"] == 1


def test_the_paired_pages_are_the_cross_file_pairs_when_there_are_some() -> None:
    from types import SimpleNamespace

    from app.review.assistant import full_check_roster

    cross = SimpleNamespace(file="Vendor.pdf", pairs={5: (("Architect.pdf", 12),)})
    engine = SimpleNamespace(_cross=cross, book=SimpleNamespace(main_file="Vendor.pdf"))
    run = full_check_roster._Run(engine=engine)  # type: ignore[arg-type]
    assert run._paired_pages() == frozenset({("Vendor.pdf", 5), ("Architect.pdf", 12)})


def test_the_engine_counts_outside_spend_like_its_own_calls() -> None:
    """`ChatEngine.charge_outside`: the money goes into `spent` (which every cap reads), onto the
    current phase's timing, and each call onto the Usage page."""
    import threading
    from types import SimpleNamespace

    from app.review.assistant.chat_engine import ChatEngine

    recorded: list[dict[str, Any]] = []
    timed: list[int] = []
    emitted: list[tuple[str, Any]] = []
    fake = SimpleNamespace(
        _lock=threading.Lock(),
        _spent=1_000,
        timings=SimpleNamespace(charge=timed.append),
        ledger=SimpleNamespace(record=lambda **row: recorded.append(row)),
        emit=lambda kind, data: emitted.append((kind, data)),
    )
    ChatEngine.charge_outside(fake, 4_500, [(QWEN, "slot-crop-v1", 900, 40)])  # type: ignore[arg-type]
    assert fake._spent == 5_500 and timed == [4_500]
    assert [(r["model_key"], r["input_tokens"], r["output_tokens"]) for r in recorded] == [
        (QWEN, 900, 40)
    ]
    assert emitted and emitted[0][0] == "usage"
    ChatEngine.charge_outside(fake, -5)  # type: ignore[arg-type]
    assert fake._spent == 5_500


class LongLine(FakePages):
    """Every spot's line drawn 24 1/2" long at SCALE."""

    def line(self, spot: sr.Spot) -> sr.LineFound | None:
        return sr.LineFound("h", 115.0, 100.0, 100.0 + float(Fraction(49, 2) / SCALE))


def test_witnessed_only_where_the_drawn_length_agrees_as_decision_b_counts_it(
    tmp_path: Path,
) -> None:
    """23" drawn 24 1/2" long is inside the misread band (6.5%) but past scale_fit's tolerance
    (0.69" here): never a second reading, so sealed but unwitnessed, said why."""
    write_pack(tmp_path, [spot([100, 100, 130, 110])])
    pages = LongLine()
    run = sr.ensure_stroke_reads(
        tmp_path, None, FakeReader({"p3.S1@0": '23"'}), Decimal(1), pages=pages
    )
    label = labels_of(run)["p3.S1"]
    assert (label["state"], label["why"], label["inches"]) == (
        "sealed_unwitnessed",
        sr.NOT_CLOSE,
        "23",
    )
    assert sr.drawn_agrees(Fraction(24), Fraction(24) / SCALE, SCALE)
    assert sr.drawn_agrees(Fraction(24), Fraction(49, 2) / SCALE, SCALE)  # 0.5" off: inside
    assert not sr.drawn_agrees(Fraction(23), Fraction(49, 2) / SCALE, SCALE)


def test_a_dry_run_on_a_saved_pack_plans_without_a_reader_and_writes_only_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_pack(tmp_path, [spot([100, 100, 130, 110]), spot([200, 100, 208, 130], "sideways")])
    monkeypatch.setattr(sr, "PdfPackPages", lambda *a: FakePages(drawn={"p3.S1": 24}))

    def no_reader(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a dry run makes no reader")

    monkeypatch.setattr(sr, "reader_from_environment", no_reader)
    out = tmp_path / "out.json"
    assert sr.main([str(tmp_path), "--dry-run", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "copies that would be read (first turns): 1; spent $0" in printed
    assert json.loads(out.read_text())["schema"] == sr.SCHEMA
    assert not (tmp_path / sr.FILE).exists()
