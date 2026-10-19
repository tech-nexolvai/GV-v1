"""The simple full check's compare call (`simple_check/compare.py`, `prompts.py`), $0: pictures cut
from a synthetic PDF, a scripted provider (answers, the repair turn, retries, refusals, the first
call warming the cache), and the official SDK over a mock transport. Made-up values only."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest
from PIL import Image, ImageDraw

from app.review.assistant.chat_provider import AnthropicSdkProvider, ProviderChoice, ProviderFailure
from app.review.assistant.simple_check import compare
from app.review.assistant.simple_check.compare import (
    DOWN_REASON,
    MAX_IMAGE_TOKENS,
    MIN_LEGIBLE_DPI,
    OVERVIEW_TOKENS,
    CompareSettings,
    PictureSource,
    SentPicture,
    compare_all,
    compare_item,
    estimate_cost_usd,
    image_tokens,
    plan_parts,
    request_content,
    resized_size,
    to_result,
)
from app.review.assistant.simple_check.prompts import (
    EXAMPLE_REPLY,
    SYSTEM_PROMPT,
    item_text,
    system_prompt,
)
from app.review.assistant.simple_check.schema import (
    REPLY_SCHEMA,
    Drawing,
    Label,
    Picture,
    Reply,
    WorkItem,
)

SYSTEM = system_prompt("- CT-1: top width = cabinets + fillers (made up)", "All products")

# ---- a made-up work item ------------------------------------------------------------------------


def _label(page: int, n: int, side: str, text: str = '24"') -> Label:
    return Label(
        ref=f"p{page}.D{n}",
        page=page,
        id=f"D{n}",
        text=text,
        inches="24",
        box=(40.0 * n, 40.0, 40.0 * n + 20, 50.0),
        side=side,
    )


def _drawing(ref: str, page: int, role: str, labels: Sequence[Label], box: Any = None) -> Drawing:
    box = box or (0.0, 0.0, 400.0, 300.0)
    return Drawing(
        ref=ref,
        file="set.pdf",
        page=page,
        half=0,
        role=role,  # type: ignore[arg-type]
        box=box,
        crop_box=box,
        labels=tuple(labels),
    )


def _item(hard: tuple[str, ...] = (), item_id: str = "p7.v1") -> WorkItem:
    return WorkItem(
        id=item_id,
        file="set.pdf",
        page=7,
        area="2. Vanity",
        vendor=_drawing(item_id, 7, "vendor", [_label(7, n, "vendor") for n in (3, 4, 5)]),
        architect=(_drawing("p6.a1", 6, "architect", [_label(6, 1, "architect")]),),
        hard=hard,
    )


def _pictures(item: WorkItem) -> list[SentPicture]:
    png = Image.new("RGB", (8, 6), "white")
    import io

    buffer = io.BytesIO()
    png.save(buffer, format="PNG")
    data = buffer.getvalue()
    return [
        SentPicture(
            Picture(
                number=1,
                drawing=item.vendor.ref,
                role="vendor",
                file="set.pdf",
                page=7,
                box=(0.0, 0.0, 400.0, 300.0),
                width=800,
                height=600,
                dpi=144,
                tokens=image_tokens(800, 600),
                part="whole",
                labels=item.vendor_refs,
            ),
            data,
        ),
        SentPicture(
            Picture(
                number=2,
                drawing="p6.a1",
                role="architect",
                file="set.pdf",
                page=6,
                box=(100.0, 50.0, 500.0, 350.0),
                width=800,
                height=600,
                dpi=144,
                tokens=image_tokens(800, 600),
                part="whole",
                labels=("p6.D1",),
            ),
            data,
        ),
    ]


def _answer(*, missing: bool = False, item_id: str = "p7.v1") -> dict[str, Any]:
    pairs = [
        {
            "vendor_label": "p7.D3",
            "architect_label": "D1",
            "measures": "overall width",
            "vendor_from": "wall_face (left)",
            "vendor_to": "wall_face (right)",
            "architect_from": "wall_face (left)",
            "architect_to": "wall_face (right)",
            "verdict": "different",
            "note": "",
        },
        {
            "vendor_label": "D4",
            "architect_label": None,
            "measures": "toe kick",
            "vendor_from": "floor",
            "vendor_to": "other (toe kick top)",
            "architect_from": "",
            "architect_to": "",
            "verdict": "no_architect_value",
            "note": "",
        },
    ]
    return {
        "work_item": item_id,
        "pairs": pairs,
        "other_items": [
            {
                "kind": "dimension_without_label",
                "what": "shelf height (hand-lettered)",
                "vendor": '60"',
                "architect": "5'-0\"",
                "vendor_image": 1,
                "vendor_box_px": [80, 60, 160, 120],
                "architect_image": 2,
                "architect_box_px": [0, 0, 400, 300],
                "verdict": "same",
                "millwork": True,
            }
        ],
        "unaccounted_labels": [] if missing else ["p7.D5"],
    }


# ---- a scripted provider ------------------------------------------------------------------------


def _events(
    text: str,
    *,
    stop: str = "end_turn",
    model: str = "claude-sonnet-5-5",
    fallback: bool = False,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": 9000,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 1,
                },
            },
        }
    ]
    index = 0
    if fallback:
        events.append(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "fallback", "to": {"model": "claude-opus-4-8"}},
            }
        )
        events.append({"type": "content_block_stop", "index": index})
        index += 1
    events += [
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": "Compared the two."},
        },
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "signature_delta", "signature": "sig-1"},
        },
        {"type": "content_block_stop", "index": index},
        {
            "type": "content_block_start",
            "index": index + 1,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": index + 1,
            "delta": {"type": "text_delta", "text": text},
        },
        {"type": "content_block_stop", "index": index + 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop},
            "usage": {"output_tokens": 3000},
        },
        {"type": "message_stop"},
    ]
    return events


class _Stream:
    def __init__(self, events: list[dict[str, Any]], log: list[str], tag: str) -> None:
        self.events, self.log, self.tag = events, log, tag

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for n, event in enumerate(self.events):
            if n == 0:
                self.log.append(f"event:{self.tag}")
            yield event

    def close(self) -> None:
        pass


class _Provider:
    """Answers each call with the next script entry: a list of events or a `ProviderFailure`."""

    name = "anthropic"

    def __init__(self, script: Sequence[Any]) -> None:
        self.script = list(script)
        self.calls: list[Mapping[str, Any]] = []
        self.log: list[str] = []
        self.lock = threading.Lock()

    def open_stream(self, params: Mapping[str, Any]) -> _Stream:
        with self.lock:
            self.calls.append(params)
            text = json.dumps(params["messages"][0]["content"][-1])
            tag = text.split("Work item ")[1].split(":")[0] if "Work item " in text else "?"
            self.log.append(f"open:{tag}")
            step = self.script.pop(0) if self.script else _events(json.dumps(_answer()))
        if isinstance(step, Exception):
            raise step
        return _Stream(step, self.log, tag)


def _run(provider: _Provider, item: WorkItem | None = None, **more: Any) -> Any:
    slept: list[float] = []
    outcome = compare_item(
        item or _item(),
        provider=provider,
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(retries=2),
        sleep=slept.append,
        **more,
    )
    return outcome, slept


# ---- pure helpers ---------------------------------------------------------------------------------


def test_resized_size_follows_anthropics_rule() -> None:
    assert resized_size(3840, 2160) == (2576, 1449)
    assert resized_size(2000, 1500) == (2000, 1500)
    assert image_tokens(1000, 1000) == 1296
    w, h = resized_size(2400, 2400)
    assert image_tokens(w, h) <= MAX_IMAGE_TOKENS


def test_a_small_drawing_goes_whole_and_a_big_one_in_sharp_tiles() -> None:
    whole = plan_parts((0.0, 0.0, 500.0, 370.0))
    assert [p.part for p in whole] == ["whole"] and whole[0].dpi >= MIN_LEGIBLE_DPI
    big = plan_parts((10.0, 20.0, 1130.0, 690.0))
    assert big[0].part == "overview"
    tiles = big[1:]
    assert len(tiles) >= 2 and all(t.dpi >= MIN_LEGIBLE_DPI for t in tiles)
    # The tiles cover the drawing, overlapping.
    assert min(t.box[0] for t in tiles) == 10.0 and max(t.box[2] for t in tiles) == 1130.0
    assert min(t.box[1] for t in tiles) == 20.0 and max(t.box[3] for t in tiles) == 690.0
    width = int(1120 * big[0].dpi / 72) + 2, int(670 * big[0].dpi / 72) + 2
    assert image_tokens(*width) <= OVERVIEW_TOKENS
    # Too big for any tiling to reach the mark: the sharpest arrangement, not the fewest tiles.
    huge = plan_parts((0.0, 0.0, 3000.0, 2000.0))[1:]
    assert len(huge) == compare.MAX_TILES
    assert all(t.dpi == max(t.dpi for t in huge) for t in huge)


def test_the_prompt_is_short_and_states_the_rules() -> None:
    assert len(SYSTEM_PROMPT) < 7000
    for words in (
        "architect's drawings are the truth",
        "EVERY vendor label",
        "different_points",
        "millwork is true",
        "dimension_without_label",
        "Never type a value",
    ):
        assert words in SYSTEM_PROMPT
    # The worked example is itself a valid answer.
    Reply.model_validate(EXAMPLE_REPLY)
    assert SYSTEM.startswith(SYSTEM_PROMPT) and "CT-1" in SYSTEM
    text = item_text(_item(), [p.picture for p in _pictures(_item())])
    assert "p7.D3" in text and "p6.D1" in text and "Work item p7.v1" in text


def test_the_reply_is_mapped_to_the_contracts_form() -> None:
    item = _item()
    reply = Reply.model_validate(_answer())
    result, problems = to_result(reply, item, [p.picture for p in _pictures(item)])
    assert problems == []
    assert result is not None
    assert [p.vendor_label for p in result.pairs] == ["p7.D3", "p7.D4"]
    assert result.pairs[0].architect_label == "p6.D1"
    other = result.other_items[0]
    assert other.vendor_box == (40.0, 30.0, 80.0, 60.0)
    assert other.architect_box == (100.0, 50.0, 300.0, 200.0)
    assert other.architect_page == 6


def test_a_box_on_the_wrong_picture_or_outside_it_is_a_problem() -> None:
    item = _item()
    document = _answer()
    document["other_items"][0]["vendor_image"] = 2
    document["other_items"][0]["architect_box_px"] = [0, 0, 900, 300]
    _result, problems = to_result(
        Reply.model_validate(document), item, [p.picture for p in _pictures(item)]
    )
    joined = "\n".join(problems)
    assert "image 2 is the architect's drawing, not the vendor's" in joined
    assert "is outside image 2" in joined


def test_cost_estimate() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 0}
    assert estimate_cost_usd("claude-sonnet-5-5", usage) == pytest.approx(3.0)
    assert estimate_cost_usd("claude-opus-5-5", usage) == pytest.approx(6.0)


# ---- one item -----------------------------------------------------------------------------------


def test_one_call_with_the_form_the_scope_and_the_pictures() -> None:
    provider = _Provider([_events(json.dumps(_answer()))])
    outcome, _slept = _run(provider)
    assert outcome.status == "ok" and outcome.attempts == 1 and not outcome.repaired
    assert outcome.result.work_item == "p7.v1"
    assert outcome.cost_usd > 0 and outcome.usage["output_tokens"] == 3000
    params = provider.calls[0]
    assert params["model"] == "claude-sonnet-5-5"
    assert params["output_config"]["effort"] == "high"
    assert params["output_config"]["format"] == {"type": "json_schema", "schema": REPLY_SCHEMA}
    assert params["thinking"]["type"] == "adaptive"
    assert "tools" not in params and "tool_choice" not in params
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    content = params["messages"][0]["content"]
    assert not any("cache_control" in block for block in content)
    images = [block for block in content if block["type"] == "image"]
    assert len(images) == 2
    assert all(i["transformations"] == {"oversized_image": "error"} for i in images)
    assert content[0]["text"].startswith("Image 1: the vendor drawing p7.v1")


def test_a_hard_item_goes_to_the_stronger_model() -> None:
    provider = _Provider([_events(json.dumps(_answer()), model="claude-opus-5-5")])
    outcome, _slept = _run(provider, _item(hard=("hand lettering",)))
    assert provider.calls[0]["model"] == "claude-opus-5-5"
    assert outcome.asked_model == "claude-opus-5-5" and outcome.status == "ok"


def test_an_answer_that_leaves_a_label_out_gets_one_repair_turn() -> None:
    provider = _Provider(
        [_events(json.dumps(_answer(missing=True))), _events(json.dumps(_answer()))]
    )
    outcome, _slept = _run(provider)
    assert outcome.status == "ok" and outcome.repaired and outcome.attempts == 2
    second = provider.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    # The thinking is passed back unchanged, then the answer, then code's problems.
    assert second[1]["content"][0] == {
        "type": "thinking",
        "thinking": "Compared the two.",
        "signature": "sig-1",
    }
    assert "p7.D5" in second[2]["content"][0]["text"]
    assert outcome.usage["output_tokens"] == 6000


def test_still_wrong_after_the_repair_turn_is_failed_never_dropped() -> None:
    provider = _Provider(
        [_events(json.dumps(_answer(missing=True))), _events(json.dumps(_answer(missing=True)))]
    )
    outcome, _slept = _run(provider)
    assert outcome.status == "failed" and outcome.result is None
    assert "after one repair turn" in (outcome.reason or "")
    assert any("p7.D5" in p for p in outcome.problems)
    assert outcome.partial is not None and outcome.partial.work_item == "p7.v1"


def test_words_instead_of_the_form_and_a_cut_answer_are_repaired() -> None:
    provider = _Provider(
        [_events("I could not finish.", stop="max_tokens"), _events(json.dumps(_answer()))]
    )
    outcome, _slept = _run(provider)
    assert outcome.status == "ok" and outcome.repaired
    provider = _Provider([_events("No JSON here."), _events("Still none.")])
    outcome, _slept = _run(provider)
    assert outcome.status == "failed" and "not the JSON form" in outcome.problems[0]


def test_transient_failures_are_asked_again_with_waits() -> None:
    provider = _Provider(
        [
            ProviderFailure("lost", status=None, retryable=True),
            ProviderFailure("busy", status=429, retryable=False),
            _events(json.dumps(_answer())),
        ]
    )
    outcome, slept = _run(provider)
    assert outcome.status == "ok" and outcome.attempts == 3
    assert len(slept) == 2 and slept[1] > slept[0]


def test_a_refused_request_or_a_refusal_fails_with_the_reason() -> None:
    provider = _Provider([ProviderFailure("refused (error 400)", status=400, retryable=False)])
    outcome, slept = _run(provider)
    assert outcome.status == "failed" and "error 400" in (outcome.reason or "") and not slept
    provider = _Provider([_events("", stop="refusal")])
    outcome, _slept = _run(provider)
    assert outcome.status == "failed" and "declined" in (outcome.reason or "")


def test_a_fallback_answer_is_repaired_from_the_start() -> None:
    provider = _Provider(
        [
            _events(json.dumps(_answer(missing=True)), fallback=True),
            _events(json.dumps(_answer())),
        ]
    )
    outcome, _slept = _run(provider)
    assert outcome.status == "ok" and outcome.model == "claude-sonnet-5-5"
    second = provider.calls[1]["messages"]
    assert len(second) == 1 and "An earlier answer was" in second[0]["content"][-1]["text"]


def test_pictures_that_cannot_be_drawn_fail_the_item() -> None:
    def broken(_item: WorkItem) -> list[SentPicture]:
        raise ValueError("there is no page 7")

    outcome = compare_item(
        _item(), provider=_Provider([]), source=broken, system=SYSTEM, settings=CompareSettings()
    )
    assert outcome.status == "failed" and "no page 7" in (outcome.reason or "")


# ---- every item ---------------------------------------------------------------------------------


def test_every_item_in_order_the_first_call_warms_the_cache() -> None:
    items = [_item(item_id=f"p7.v{n}") for n in (1, 2, 3, 4)]
    provider = _Provider([_events(json.dumps(_answer(item_id=i.id))) for i in items])
    # Each scripted answer is taken in call order; answer every item with its own id.
    provider.script = []
    answers = {i.id: _events(json.dumps(_answer(item_id=i.id))) for i in items}

    def open_stream(params: Mapping[str, Any]) -> _Stream:
        text = json.dumps(params["messages"][0]["content"][-1])
        tag = text.split("Work item ")[1].split(":")[0]
        with provider.lock:
            provider.log.append(f"open:{tag}")
        return _Stream(answers[tag], provider.log, tag)

    provider.open_stream = open_stream  # type: ignore[method-assign]
    seen: list[str] = []
    outcomes = compare_all(
        items,
        provider=provider,
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(parallel=4),
        on_outcome=lambda o: seen.append(o.work_item),
    )
    assert [o.work_item for o in outcomes] == [i.id for i in items]
    assert all(o.status == "ok" for o in outcomes)
    assert sorted(seen) == sorted(i.id for i in items)
    assert provider.log[:2] == ["open:p7.v1", "event:p7.v1"]


def test_a_stopped_run_lists_every_item_left() -> None:
    stop = threading.Event()
    stop.set()
    outcomes = compare_all(
        [_item(), _item(item_id="p7.v2")],
        provider=_Provider([]),
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(),
        stop=stop,
    )
    assert [o.status for o in outcomes] == ["failed", "failed"]
    assert all("stopped" in (o.reason or "") for o in outcomes)


def test_a_service_that_does_not_answer_stops_being_asked() -> None:
    """The app test (2026-10-19): with the AI service down, every drawing was tried again and
    again (6 minutes for 39). After three drawings in a row fail every try, the rest fail at once
    with the reason; a refused request (one drawing's own problem) does not count."""
    items = [_item(item_id=f"p7.v{n}") for n in range(1, 7)]
    provider = _Provider([ProviderFailure("connection refused", status=None, retryable=True)] * 9)
    outcomes = compare_all(
        items,
        provider=provider,
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(parallel=1, retries=2),
        sleep=lambda _s: None,
        warm_wait_s=0.0,
    )
    assert [o.status for o in outcomes] == ["failed"] * 6
    assert all("model call failed" in (o.reason or "") for o in outcomes[:3])
    assert all((o.reason or "") == DOWN_REASON for o in outcomes[3:])
    assert len(provider.calls) == 9  # three drawings, three tries each; none for the rest
    refused = _Provider([ProviderFailure("refused (error 400)", status=400, retryable=False)] * 4)
    outcomes = compare_all(
        items[:4],
        provider=refused,
        source=_pictures,
        system=SYSTEM,
        settings=CompareSettings(parallel=1, retries=2),
        sleep=lambda _s: None,
        warm_wait_s=0.0,
    )
    assert len(refused.calls) == 4 and DOWN_REASON not in [o.reason for o in outcomes]


# ---- pictures from a PDF ------------------------------------------------------------------------


def _pdf(path: Path) -> None:
    """A 1000 x 500 pt page at 72 dpi: a vendor drawing with two printed numbers."""
    page = Image.new("RGB", (1000, 500), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle((40, 40, 460, 460), outline="black", width=2)
    draw.text((100, 100), '24"', fill="black")
    draw.rectangle((540, 40, 960, 460), outline="black", width=2)
    page.save(path, "PDF", resolution=72.0)


def test_pictures_are_cut_sharp_with_codes_marks(tmp_path: Path) -> None:
    uploads, pack = tmp_path / "uploads", tmp_path / "pack"
    uploads.mkdir()
    pack.mkdir()
    _pdf(uploads / "set.pdf")
    label = Label(
        ref="p1.D1",
        page=1,
        id="D1",
        text='24"',
        inches="24",
        box=(95.0, 95.0, 125.0, 112.0),
        side="vendor",
    )
    vendor = _drawing("p1.v1", 1, "vendor", [label], box=(40.0, 40.0, 460.0, 460.0))
    architect = _drawing("p1.a1", 1, "architect", [], box=(540.0, 40.0, 960.0, 460.0))
    item = WorkItem(id="p1.v1", file="set.pdf", page=1, vendor=vendor, architect=(architect,))
    source = PictureSource(pack, uploads)
    sent = source.pictures(item)
    assert [s.picture.drawing for s in sent] == ["p1.v1", "p1.a1"]
    for one in sent:
        picture = one.picture
        assert picture.tokens <= MAX_IMAGE_TOKENS and max(picture.width, picture.height) <= 2576
        assert picture.dpi >= MIN_LEGIBLE_DPI
        with Image.open(__import__("io").BytesIO(one.png)) as opened:
            assert opened.size == (picture.width, picture.height)
    assert sent[0].picture.labels == ("p1.D1",)
    # Code's blue tag is drawn near the label.
    with Image.open(__import__("io").BytesIO(sent[0].png)) as opened:
        rgb = opened.convert("RGB")
        scale = sent[0].picture.width / (460 - 40)
        x, y = int((95 - 40) * scale), int((95 - 40) * scale)
        region = [rgb.getpixel((x + dx, y - dy)) for dx in range(30) for dy in range(2, 30)]
        assert any(b > 180 and r < 60 for r, _g, b in region)
    content = request_content(item, sent)
    assert sum(1 for block in content if block["type"] == "image") == 2


# ---- the official SDK, over a mock transport --------------------------------------------------------


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def test_the_sdk_sends_the_form_and_reads_the_streamed_answer() -> None:
    import anthropic

    requests: list[httpx2.Request] = []

    def transport(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(_events(json.dumps(_answer()))),
        )

    provider = AnthropicSdkProvider(
        ProviderChoice("anthropic", "sk-ant-test-not-real"),
        timeout_seconds=30,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(transport)),
    )
    outcome = compare_item(
        _item(), provider=provider, source=_pictures, system=SYSTEM, settings=CompareSettings()
    )
    assert outcome.status == "ok", outcome.problems
    body = json.loads(requests[0].content)
    assert body["model"] == "claude-sonnet-5-5" and body["stream"] is True
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "high"
    assert body["messages"][0]["content"][1]["transformations"] == {"oversized_image": "error"}
    assert "fallbacks" in body


def test_settings_from_the_app_settings() -> None:
    from app.config import Settings

    settings = Settings(
        database_url="postgresql+psycopg://x/y",
        assistant_simple_parallel=3,
        _env_file=None,  # type: ignore[call-arg]
    )
    chosen = CompareSettings.from_settings(settings)
    assert chosen.parallel == 3 and chosen.model == "claude-sonnet-5-5"
    model, effort = chosen.for_item(_item(hard=("dense",)))
    assert model.id == "claude-opus-5-5" and effort == "high"
    assert compare.WARM_WAIT_S > 0
