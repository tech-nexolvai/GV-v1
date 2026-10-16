"""The set pack, the zoom tool, parallel tools and the helper footer (#1497). No network, no paid call.

The drawing set here is synthetic: a combined sheet built in the test (pasted drawings as `/Stamp`
annotations, GV's typed headings and round captions as 24 pt FreeText, a red GV note), never a
client file.
"""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from app.config import Settings
from app.review.assistant import set_pack
from app.review.assistant.chat_drawing import crop_png
from app.review.assistant.chat_engine import ChatEngine, EngineSettings
from app.review.assistant.chat_request import MODELS
from app.review.assistant.chat_tools import (
    HELPER_FOOTER_MARK,
    MAX_ZOOM_CROPS,
    ChatTools,
    helper_footer,
    named_pages,
    new_step,
    seen_log,
)
from app.review.assistant.sandbox import SET_PACK, ChatWorkspace
from app.runs.rates import load_model_rates
from tests.review.assistant.chat_fakes import (
    SONNET,
    FakeLedger,
    FakeSandbox,
    ScriptedProvider,
    reply,
    text,
    tool,
)

RATES = load_model_rates(Path("deploy/model_rates.us-east-1.json"))

# ---- a synthetic combined set ----------------------------------------------------------------------

WIDTH, HEIGHT = 1000.0, 700.0


def _form(writer: PdfWriter, body: bytes, width: float, height: float) -> Any:
    stream = DecodedStreamObject()
    stream.set_data(body)
    stream.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(
                [FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)]
            ),
        }
    )
    return writer._add_object(stream)


def _annotation(
    writer: PdfWriter,
    page: Any,
    kind: str,
    rect: Sequence[float],
    *,
    text_: str | None = None,
    size: int = 24,
    colour: bytes = b"0 0 0",
) -> None:
    x0, y0, x1, y1 = rect
    body = colour + b" rg 0 0 " + f"{x1 - x0} {y1 - y0}".encode() + b" re f"
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject(f"/{kind}"),
            NameObject("/Rect"): ArrayObject([FloatObject(v) for v in rect]),
            NameObject("/AP"): DictionaryObject(
                {NameObject("/N"): _form(writer, body, x1 - x0, y1 - y0)}
            ),
            NameObject("/F"): NumberObject(4),
        }
    )
    if text_ is not None:
        annotation[NameObject("/Contents")] = TextStringObject(text_)
        annotation[NameObject("/DA")] = TextStringObject(f"0 0 0 rg /Helv {size} Tf")
        annotation[NameObject("/T")] = TextStringObject("Reviewer A")
    reference = writer._add_object(annotation)
    if "/Annots" not in page:
        page[NameObject("/Annots")] = ArrayObject()
    page["/Annots"].append(reference)


def combined_set() -> bytes:
    """Page 1: a divider with a 36 pt section title. Page 2: the architect's drawing (heading
    above it) over the vendor's (heading above it, first-round caption under it), the area title,
    and a red GV note inside the vendor's drawing. Page 3: the vendor's second round beside a brand
    prototype, both captioned below."""
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(WIDTH, HEIGHT)
    one, two, three = writer.pages
    _annotation(writer, one, "FreeText", (300, 320, 700, 380), text_="1. KITCHEN WALL", size=36)
    # PDF space (y up): the architect's drawing in the upper half, the vendor's in the lower half.
    _annotation(writer, two, "FreeText", (20, 660, 220, 690), text_="2. Vanity", size=24)
    _annotation(writer, two, "FreeText", (400, 640, 640, 668), text_="ID SET ELEVATION")
    _annotation(writer, two, "Stamp", (300, 400, 700, 630), colour=b"0.2 0.2 0.2")
    _annotation(writer, two, "FreeText", (380, 340, 700, 368), text_="VENDOR'S SHOP DRAWING")
    _annotation(writer, two, "Stamp", (300, 60, 700, 330), colour=b"0.2 0.2 0.2")
    _annotation(writer, two, "FreeText", (400, 20, 650, 48), text_="First set of comments")
    _annotation(writer, two, "FreeText", (450, 150, 520, 170), text_='38"', size=8, colour=b"1 0 0")
    _annotation(writer, three, "Stamp", (50, 200, 450, 600), colour=b"0.2 0.2 0.2")
    _annotation(writer, three, "FreeText", (100, 160, 400, 188), text_="BRAND PROTOTYPE")
    _annotation(writer, three, "Stamp", (550, 200, 950, 600), colour=b"0.2 0.2 0.2")
    _annotation(writer, three, "FreeText", (600, 160, 900, 188), text_="VENDOR SHOP DRAWING")
    _annotation(writer, three, "FreeText", (600, 120, 900, 148), text_="Second set of comments")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


LISTING = [{"name": "combined.pdf", "sha256": "a" * 64, "pages": 3, "kind": "the vendor's"}]


@pytest.fixture
def uploads(tmp_path: Path) -> Path:
    folder = tmp_path / "uploads"
    folder.mkdir()
    (folder / "combined.pdf").write_bytes(combined_set())
    return folder


@pytest.fixture
def pack(tmp_path: Path, uploads: Path) -> Path:
    folder = set_pack.pack_folder(tmp_path / "chats", LISTING)
    set_pack.ensure_pack(folder, uploads, LISTING, background=False)
    return folder


# ---- the set pack ----------------------------------------------------------------------------------


def test_the_pack_holds_the_page_map_annotations_pictures_and_contact_sheets(pack: Path) -> None:
    status = json.loads((pack / "status.json").read_text())
    assert status["state"] == "ready"
    assert status["done"] == ["drawings_only", "page_map", "pages", "contact_sheets"]
    assert sorted(p.name for p in (pack / "pages" / "combined").iterdir()) == [
        "p-001.png",
        "p-002.png",
        "p-003.png",
    ]
    with Image.open(pack / "pages" / "combined" / "p-001.png") as page:
        width, height = page.size
        assert abs(width - WIDTH * 100 / 72) <= 1 and abs(height - HEIGHT * 100 / 72) <= 1
    assert [p.name for p in (pack / "contact").iterdir()] == ["combined-p001-003.png"]
    assert (pack / "README.txt").read_text().startswith("The set pack")

    annotations = json.loads((pack / "annotations.json").read_text())
    heading = next(a for a in annotations if a["text"] == "ID SET ELEVATION")
    # Shown boxes: points, origin top-left.
    assert heading == {
        "file": "combined.pdf",
        "page": 2,
        "kind": "FreeText",
        "author": "Reviewer A",
        "text": "ID SET ELEVATION",
        "font_pt": 24.0,
        "box": [400.0, 32.0, 640.0, 60.0],
    }


def test_the_page_map_reads_roles_per_drawing_rounds_and_areas(pack: Path) -> None:
    pages = json.loads((pack / "page_map.json").read_text())["pages"]
    first, second, third = pages
    assert first["role"] == "general" and first["section"] == "1. KITCHEN WALL"
    assert first["halves"] == []

    assert second["role"] == "architect+vendor"
    assert second["area"] == "2. Vanity" and second["area_from_page"] is None
    assert [(h["role"], h["heading"], h["round"]) for h in second["halves"]] == [
        ("architect", "ID SET ELEVATION", None),
        ("vendor", "VENDOR'S SHOP DRAWING", 1),
    ]
    assert second["halves"][0]["box"] == [300.0, 70.0, 700.0, 300.0]
    assert second["rounds"] == [1]

    # Captions below their drawings; the area carries on from page 2.
    assert third["role"] == "brand_prototype+vendor"
    assert [(h["role"], h["round"]) for h in third["halves"]] == [
        ("brand_prototype", None),
        ("vendor", 2),
    ]
    assert third["area"] == "2. Vanity" and third["area_from_page"] == 2


def test_the_drawings_only_copy_keeps_only_the_pasted_drawings(pack: Path) -> None:
    reader = PdfReader(str(pack / "combined_drawings_only.pdf"))
    kinds = [
        [a.get_object()["/Subtype"] for a in page.get("/Annots") or []] for page in reader.pages
    ]
    assert kinds == [[], ["/Stamp", "/Stamp"], ["/Stamp", "/Stamp"]]


def test_the_pack_is_made_once_per_upload_and_reused_across_chats(
    tmp_path: Path, uploads: Path, pack: Path
) -> None:
    again = set_pack.pack_folder(tmp_path / "chats", [dict(item) for item in LISTING])
    assert again == pack
    before = (pack / "page_map.json").stat().st_mtime_ns
    assert set_pack.ensure_pack(again, uploads, LISTING) is None  # ready: nothing to do
    assert (pack / "page_map.json").stat().st_mtime_ns == before
    other = [{**LISTING[0], "sha256": "b" * 64}]
    assert set_pack.pack_folder(tmp_path / "chats", other) != pack


def test_the_pack_is_built_in_the_background(tmp_path: Path, uploads: Path) -> None:
    folder = set_pack.pack_folder(tmp_path / "chats", LISTING)
    thread = set_pack.ensure_pack(folder, uploads, LISTING)
    assert thread is not None
    assert set_pack.ensure_pack(folder, uploads, LISTING) in (thread, None)  # never twice
    thread.join(timeout=60)
    assert json.loads((folder / "status.json").read_text())["state"] == "ready"


# ---- zoom ------------------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path, uploads: Path, pack: Path) -> ChatWorkspace:
    opened = ChatWorkspace.open(tmp_path / "chat", set_pack=pack)
    (opened.uploads / "combined.pdf").write_bytes((uploads / "combined.pdf").read_bytes())
    return opened


def _zoom_tools(workspace: ChatWorkspace) -> ChatTools:
    return ChatTools(workspace, FakeSandbox(workspace), uploads=LISTING)


def _zoom(tools: ChatTools, raw: Mapping[str, Any], by: str = "lead") -> Any:
    return tools.zoom(raw, new_step("z", "zoom", raw), by)


def _red(png: bytes) -> int:
    with Image.open(io.BytesIO(png)) as picture:
        raw = picture.convert("RGB").tobytes()
    pixels = zip(raw[0::3], raw[1::3], raw[2::3], strict=True)
    return sum(1 for r, g, b in pixels if r > 200 and g < 80 and b < 80)


def test_zoom_returns_one_labelled_picture_per_crop_and_logs_what_was_seen(
    workspace: ChatWorkspace,
) -> None:
    tools = _zoom_tools(workspace)
    raw = {
        "crops": [
            {"page": 2, "box": {"x0": 420, "y0": 520, "x1": 560, "y1": 560}, "label": "width"},
            {"page": 3, "box": {"x0": 0, "y0": 0, "x1": WIDTH, "y1": HEIGHT}},
        ],
        "description": "Read the widths",
    }

    result = _zoom(tools, raw, "helper:2")

    assert not result.is_error
    kinds = [block["type"] for block in result.content]
    assert kinds == ["text", "image", "text", "image"]
    assert result.content[0]["text"].startswith("Crop 1 (width): page 2 of combined.pdf")
    assert "300 dpi" in result.content[0]["text"]  # a small box: as sharp as fits
    first, whole = tools.seen
    assert first["by"] == "helper:2" and first["dpi"] == 300 and first["page"] == 2
    assert first["box"] == {"x0": 420.0, "y0": 520.0, "x1": 560.0, "y1": 560.0}
    picture = workspace.pictures / f"{first['picture_id']}.png"
    with Image.open(picture) as shown:
        width, height = shown.size
        assert abs(width - 140 * 300 / 72) <= 2 and abs(height - 40 * 300 / 72) <= 2
    # A whole page fits the model's limit, at a lower dpi.
    assert whole["dpi"] < 200
    assert max(result.content[3]["source"]["width"], result.content[3]["source"]["height"]) <= 2576
    assert result.step["tool"] == "view" and result.step["picture_id"] == first["picture_id"]
    assert result.step["label"] == "Read the widths"
    assert seen_log([{"type": "step", "step": result.step}]) == tools.seen


def test_zoom_hides_gvs_markup(workspace: ChatWorkspace, pack: Path) -> None:
    tools = _zoom_tools(workspace)
    box = {"x0": 440, "y0": 520, "x1": 530, "y1": 560}  # the red GV note on page 2
    result = _zoom(tools, {"crops": [{"page": 2, "box": box}]})
    zoomed = (workspace.pictures / f"{tools.seen[0]['picture_id']}.png").read_bytes()
    original = crop_png(workspace.uploads / "combined.pdf", 2, (440, 520, 530, 560))
    assert not result.is_error
    assert original is not None and _red(original) > 50
    assert _red(zoomed) == 0


def test_zoom_limits_and_plain_reasons(workspace: ChatWorkspace) -> None:
    tools = _zoom_tools(workspace)
    box = {"x0": 0, "y0": 0, "x1": 50, "y1": 50}
    too_many = {"crops": [{"page": 1, "box": box}] * (MAX_ZOOM_CROPS + 1)}
    assert "1 to 4" in _zoom(tools, too_many).content[0]["text"]

    wrong_file = {"crops": [{"page": 1, "box": box, "file": "other.pdf"}]}
    refused = _zoom(tools, wrong_file)
    assert refused.is_error
    assert "uploads are at /mnt/user-data/uploads: combined.pdf" in refused.content[0]["text"]

    partly = _zoom(
        tools,
        {
            "crops": [
                {"page": 9, "box": box},
                {"page": 1, "box": {"x0": 5000, "y0": 5000, "x1": 5100, "y1": 5100}},
                {"page": 1, "box": box},
            ]
        },
    )
    assert not partly.is_error  # one crop was drawn
    words = [block["text"] for block in partly.content if block["type"] == "text"]
    assert words[0] == "Crop 1: not drawn: there is no page 9 (the file has 3)."
    assert words[1].startswith("Crop 2: not drawn: the box is not on page 1")
    assert len(tools.seen) == 1


def test_zoom_makes_the_drawings_only_copy_before_the_pack_is_ready(
    tmp_path: Path, uploads: Path
) -> None:
    workspace = ChatWorkspace.open(tmp_path / "chat")  # no pack at all
    (workspace.uploads / "combined.pdf").write_bytes((uploads / "combined.pdf").read_bytes())
    tools = _zoom_tools(workspace)
    result = _zoom(
        tools, {"crops": [{"page": 2, "box": {"x0": 300, "y0": 70, "x1": 700, "y1": 300}}]}
    )
    assert not result.is_error
    assert (workspace.root / "set-local" / "combined_drawings_only.pdf").is_file()
    # The set folder the sandbox sees says it is not ready.
    assert workspace.resolve(f"{SET_PACK}/status.json").read_text().startswith('{"state"')


# ---- fix lines -------------------------------------------------------------------------------------


def test_a_wrong_path_says_where_the_files_are(workspace: ChatWorkspace) -> None:
    tools = _zoom_tools(workspace)
    raw = {"path": "/mnt/uploads/combined.pdf", "description": "x"}
    refused = tools.view(raw, new_step("v", "view", raw))
    assert refused.is_error
    words = refused.content[0]["text"]
    assert "The uploads are at /mnt/user-data/uploads: combined.pdf" in words
    assert "the set pack" in words and "/mnt/user-data/set/" in words

    failing = ChatTools(
        workspace,
        FakeSandbox(workspace, lambda c, _w: ("ls: /mnt/uploads: No such file or directory", 2)),
    )
    raw = {"command": "ls /mnt/uploads", "description": "x"}
    told = failing.bash(raw, new_step("b", "bash_tool", raw))
    assert (
        "[Fix: The uploads are at /mnt/user-data/uploads: combined.pdf" in told.content[0]["text"]
    )


# ---- the helper footer -----------------------------------------------------------------------------


def test_named_pages_reads_the_pages_a_brief_names() -> None:
    assert named_pages("Compare pages 3-5 and 9, then p. 12; sheets 14 to 15") == [
        3,
        4,
        5,
        9,
        12,
        14,
        15,
    ]
    assert named_pages("Compare the vanity tops.") == []


def test_the_footer_gives_the_pages_from_the_page_map(workspace: ChatWorkspace) -> None:
    footer = helper_footer("Compare pages 2-3 with the architect.", workspace)
    assert footer.startswith(HELPER_FOOTER_MARK)
    assert "- Your pages: 2-3. Work only on them." in footer
    assert "- page 2 (combined.pdf): architect+vendor, 2. Vanity; drawings: architect at" in footer
    assert "vendor round 1 at" in footer
    assert "The set pack is ready at /mnt/user-data/set/" in footer
    assert "zoom" in footer and "about 12 tool calls" in footer
    assert "at most 300 words" in footer and "did not reach" in footer
    # No pages named: no page lines, the rest stays.
    plain = helper_footer("Count the sinks.", workspace)
    assert "Your pages" not in plain and "about 12 tool calls" in plain


# ---- parallel tools --------------------------------------------------------------------------------


class _Engine:
    def __init__(
        self,
        tmp_path: Path,
        provider: ScriptedProvider,
        handler: Any,
        max_parallel_agents: int = 6,
    ) -> None:
        self.workspace = ChatWorkspace.open(tmp_path / "chat")
        self.sandbox = FakeSandbox(self.workspace, handler)
        self.events: list[tuple[str, Any]] = []
        cancel = threading.Event()
        provider.cancel = cancel
        self.engine = ChatEngine(
            settings=EngineSettings(
                model=MODELS["claude-opus-5-5"],
                effort="high",
                thinking=True,
                lead_max_turns=10,
                helper_max_turns=10,
                message_cap_micros=5_000_000,
                package_cap_micros=40_000_000,
                rates=lambda _key: RATES,
                max_parallel_agents=max_parallel_agents,
            ),
            provider=provider,  # type: ignore[arg-type]
            tools=ChatTools(workspace=self.workspace, sandbox=self.sandbox, page_count=3),
            ledger=FakeLedger(),
            emit=lambda event, data: self.events.append((event, data)),
            history=(),
            user_content=[{"type": "text", "text": "Go"}],
            package_spent=lambda: 0,
            read_picture=lambda pid: (self.workspace.pictures / f"{pid}.png").read_bytes(),
            cancel=cancel,
            sleep=lambda _seconds: False,
        )


def _slow_first() -> tuple[Any, list[str]]:
    """`slow` waits until `fast` has finished: it can only finish when the two run together."""
    finished: list[str] = []
    fast_done = threading.Event()

    def handler(command: str, _w: ChatWorkspace) -> tuple[str, int]:
        if command == "slow":
            assert fast_done.wait(5), "the two commands did not run at the same time"
            time.sleep(0.05)
        finished.append(command)
        if command == "fast":
            fast_done.set()
        return f"out {command}", 0

    return handler, finished


def test_the_leads_read_only_tools_run_together_and_reply_in_order(tmp_path: Path) -> None:
    handler, finished = _slow_first()
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("a", "bash_tool", {"command": "slow", "description": "Slow"}),
                tool("b", "bash_tool", {"command": "fast", "description": "Fast"}),
                tool("c", "create_file", {"path": "n.txt", "file_text": "x", "description": "N"}),
                stop="tool_use",
            ),
            reply(text("Done.")),
        ]
    )
    harness = _Engine(tmp_path, provider, handler)

    outcome = harness.engine.run()

    assert finished == ["fast", "slow"]
    # One user message with every result, in the order asked.
    results = outcome.api_content[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]
    assert results[0]["content"][0]["text"].endswith("out slow")
    # The steps show in the order asked too.
    starts = [data["step"]["id"] for event, data in harness.events if event == "step_start"]
    assert starts == ["a", "b", "c"]


def test_a_helpers_read_only_tools_run_together_and_reply_in_order(tmp_path: Path) -> None:
    handler, finished = _slow_first()
    task = "Compare pages 2-3"
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("ag", "Agent", {"description": "Pages 2-3", "prompt": task}),
                stop="tool_use",
            ),
            reply(text("Done.")),
        ],
        helpers={
            task: [
                reply(
                    tool("h1", "bash_tool", {"command": "slow", "description": "Slow"}),
                    tool("h2", "bash_tool", {"command": "fast", "description": "Fast"}),
                    stop="tool_use",
                    model=SONNET,
                ),
                reply(text("Pages 2-3: nothing differs."), model=SONNET),
            ]
        },
    )
    harness = _Engine(tmp_path, provider, handler)

    harness.engine.run()

    assert finished == ["fast", "slow"]
    second = json.loads(provider.helper_calls[task][1])
    results = second["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["h1", "h2"]
    # The app's footer is on the helper's brief.
    brief = json.loads(provider.helper_calls[task][0])["messages"][0]["content"][0]["text"]
    assert brief.startswith(task + HELPER_FOOTER_MARK) and "Your pages: 2-3" in brief


def test_parallel_helpers_are_capped_by_the_setting(tmp_path: Path) -> None:
    harness = _Engine(tmp_path, ScriptedProvider(lead=[]), None, max_parallel_agents=20)
    assert harness.engine._max_agents == 8
    assert Settings.model_fields["assistant_max_parallel_agents"].default == 6
    with pytest.raises(ValueError):
        Settings(database_url="postgresql+psycopg://x@h/db", assistant_max_parallel_agents=9)
